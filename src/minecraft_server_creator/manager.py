from __future__ import annotations

from datetime import datetime, timezone
import os
import re
import secrets
import shutil
import stat
import tarfile
from pathlib import Path
import tempfile
import time
import uuid
from typing import BinaryIO, Callable, Iterator
from urllib.parse import urlsplit

from .config import AppConfig, ConfigStore, data_directory
from .geyser import GeyserInstallPlan, GeyserInstallResult, GeyserInstaller
from .minecraft import MinecraftMetadataClient
from .launch import LaunchConfigurationError, validate_profile_launch
from .loaders import ArclightInstaller, FabricInstaller, ForgeInstaller, LoaderInstallResult, NeoForgeInstaller
from .models import IMPLEMENTED_SERVER_TYPES, PortSettings, ServerProfile, slugify, validate_port
from .network import port_available
from .properties import read_properties, update_properties
from .rcon import (
    DEFAULT_RCON_PORT,
    RconClient,
    RconError,
    inventory_editing_disabled_reason,
    normalize_item_id,
    player_data_disabled_reason,
    validate_player_name,
)
from .runtimes import RuntimeManager
from .systemd import SystemdUserService


class ManagerError(RuntimeError):
    pass


def _format_backup_size(value: float) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _iter_backup_paths(server_dir: Path) -> Iterator[Path]:
    def visit(path: Path) -> Iterator[Path]:
        yield path
        if path.is_symlink() or not path.is_dir():
            return
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            yield from visit(child)

    for child in sorted(server_dir.iterdir(), key=lambda item: item.name):
        if child.name in {"backups", "logs"}:
            continue
        yield from visit(child)


def _measure_backup(server_dir: Path, progress: Callable[[str], None] | None) -> tuple[int, int, int]:
    total_bytes = 0
    file_count = 0
    entry_count = 0
    seen_inodes: set[tuple[int, int]] = set()
    last_report = time.monotonic()
    for path in _iter_backup_paths(server_dir):
        entry_count += 1
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            inode = (info.st_dev, info.st_ino)
            if info.st_ino and info.st_nlink > 1 and inode in seen_inodes:
                continue
            if info.st_ino:
                seen_inodes.add(inode)
            total_bytes += info.st_size
            file_count += 1
        now = time.monotonic()
        if progress and now - last_report >= 1.0:
            relative = path.relative_to(server_dir).as_posix()
            progress(f"Backup: Dateiliste wird ermittelt — {entry_count} Einträge geprüft; zuletzt {relative}")
            last_report = now
    return total_bytes, file_count, entry_count


class _BackupProgressReader:
    def __init__(self, stream: BinaryIO, on_read: Callable[[int], None]) -> None:
        self.stream = stream
        self.on_read = on_read

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        if chunk:
            self.on_read(len(chunk))
        return chunk


def _add_backup_path(
    archive: tarfile.TarFile,
    path: Path,
    arcname: str,
    on_entry: Callable[[str], None],
    on_bytes: Callable[[str, int], None],
) -> None:
    info = archive.gettarinfo(str(path), arcname)
    on_entry(arcname)
    if info is None:
        return
    if info.isreg():
        with path.open("rb") as stream:
            reader = _BackupProgressReader(stream, lambda amount: on_bytes(arcname, amount))
            archive.addfile(info, reader)
        return
    archive.addfile(info)
    if info.isdir():
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _add_backup_path(archive, child, f"{arcname}/{child.name}", on_entry, on_bytes)


class ServerManager:
    def __init__(
        self,
        store: ConfigStore | None = None,
        metadata: MinecraftMetadataClient | None = None,
        runtimes: RuntimeManager | None = None,
        services: SystemdUserService | None = None,
        fabric: FabricInstaller | None = None,
        forge: ForgeInstaller | None = None,
        neoforge: NeoForgeInstaller | None = None,
        arclight: ArclightInstaller | None = None,
        geyser: GeyserInstaller | None = None,
    ) -> None:
        self.store = store or ConfigStore()
        self.metadata = metadata or MinecraftMetadataClient()
        self.runtimes = runtimes or RuntimeManager(data_directory())
        self.services = services or SystemdUserService()
        self.fabric = fabric or FabricInstaller()
        self.forge = forge or ForgeInstaller()
        self.neoforge = neoforge or NeoForgeInstaller()
        self.arclight = arclight or ArclightInstaller()
        self.geyser = geyser or GeyserInstaller()

    def load(self) -> AppConfig:
        return self.store.load()

    def save(self, config: AppConfig) -> None:
        self.store.save(config)

    def setup(self, servers_root: str | Path, defaults: PortSettings) -> AppConfig:
        config = self.load()
        root = Path(servers_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        config.servers_root = str(root)
        config.port_defaults = defaults
        self.save(config)
        return config

    def create_server(
        self,
        name: str,
        *,
        server_type: str = "vanilla",
        version: str | None = "latest",
        arclight_loader: str | None = None,
        ports: PortSettings | None = None,
        memory_mb: int = 4096,
        auto_start_on_boot: bool = False,
    ) -> ServerProfile:
        config = self.load()
        if server_type not in IMPLEMENTED_SERVER_TYPES:
            raise ManagerError(
                f"{server_type} ist bereits in der Planung, aber noch nicht implementiert. "
                "Aktuell sind Vanilla, Fabric, Forge, NeoForge und Arclight verfügbar."
            )
        if server_type == "arclight":
            if arclight_loader not in ArclightInstaller.FAMILIES:
                raise ManagerError("Arclight benötigt die explizite Basis-Auswahl fabric, forge oder neoforge.")
        elif arclight_loader is not None:
            raise ManagerError("--arclight-loader darf nur für Arclight-Profile gesetzt werden.")
        if not name.strip():
            raise ManagerError("Der Servername darf nicht leer sein.")
        if memory_mb < 1024:
            raise ManagerError("Mindestens 1024 MB RAM müssen zugewiesen werden.")
        ports = ports or config.port_defaults
        slug = slugify(name)
        if any(profile.slug == slug for profile in config.profiles):
            raise ManagerError(f"Ein Serverprofil mit der Kennung '{slug}' existiert bereits.")

        minecraft_version = self.metadata.resolve_version(version)
        version_info = self.metadata.version_info(minecraft_version)
        java_major = version_info.get("javaVersion", {}).get("majorVersion")
        if not java_major:
            raise ManagerError(f"Die erforderliche Java-Version für Minecraft {minecraft_version} ist unbekannt.")
        root = Path(config.servers_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        final_dir = root / slug
        if final_dir.exists():
            raise ManagerError(f"Der Serverordner existiert bereits: {final_dir}")

        profile_id = uuid.uuid4().hex[:12]
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{slug}-", dir=root))
        try:
            for child in ("logs", "mods", "plugins", "backups", "config"):
                (temp_dir / child).mkdir(mode=0o700, parents=True, exist_ok=True)
            loader_install: LoaderInstallResult | None = None
            if server_type == "vanilla":
                self.metadata.install_vanilla(minecraft_version, temp_dir)
            elif server_type == "fabric":
                loader_install = self.fabric.install(minecraft_version, version_info, temp_dir)
            elif server_type in {"forge", "neoforge"}:
                # Forge-family installers execute Java during profile creation, so the required
                # managed runtime is prepared now rather than waiting until the first server start.
                installer_java = self.runtimes.ensure(int(java_major))
                installer = self.forge if server_type == "forge" else self.neoforge
                loader_install = installer.install(minecraft_version, version_info, temp_dir, installer_java)
            elif server_type == "arclight":
                loader_install = self.arclight.install(
                    minecraft_version,
                    version_info,
                    temp_dir,
                    str(arclight_loader),
                )
            (temp_dir / "eula.txt").write_text("eula=false\n", encoding="utf-8")
            update_properties(
                temp_dir / "server.properties",
                {
                    "server-port": ports.java_tcp,
                    "server-ip": "",
                    "motd": name.replace("\n", " ").replace("\r", " "),
                    "max-players": 20,
                    "online-mode": "true",
                    "enable-rcon": "false",
                    "enable-query": "false",
                },
            )
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            profile = ServerProfile(
                id=profile_id,
                name=name.strip(),
                slug=slug,
                server_type=server_type,
                minecraft_version=minecraft_version,
                java_major=int(java_major),
                server_dir=str(temp_dir),
                memory_mb=memory_mb,
                loader_version=loader_install.loader_version if loader_install else None,
                loader_family=loader_install.loader_family if loader_install else None,
                launch_mode=loader_install.launch_mode if loader_install else "jar",
                launch_target=loader_install.launch_target if loader_install else "server.jar",
                ports=ports,
                auto_start_on_boot=auto_start_on_boot,
            )
            try:
                validate_profile_launch(profile)
            except LaunchConfigurationError as exc:
                raise ManagerError(f"Der Loader hat keine gültige Server-Startkonfiguration erstellt: {exc}") from exc
            temp_dir.rename(final_dir)
            profile.server_dir = str(final_dir)
            config.profiles.append(profile)
            try:
                self.save(config)
            except Exception:
                shutil.rmtree(final_dir, ignore_errors=True)
                raise
            return profile
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def get_profile(self, selector: str) -> ServerProfile:
        return self.load().find_profile(selector)

    def _validate_geyser_profile(self, profile: ServerProfile) -> None:
        if self.services.is_active(profile):
            raise ManagerError("Stoppe den Server vor der Installation oder Konfiguration von Geyser.")
        if profile.server_type != "fabric":
            raise ManagerError("Geyser-Fabric lässt sich nur auf einem reinen Fabric-Profil installieren.")
        latest_version = self.metadata.latest_release()
        if profile.minecraft_version != latest_version:
            raise ManagerError(
                f"Geyser-Fabric unterstützt laut offizieller Anleitung nur die neueste stabile Minecraft-Version "
                f"({latest_version}); dieses Profil verwendet {profile.minecraft_version}."
            )
        bedrock_port = profile.ports.bedrock_udp
        if bedrock_port is None:
            raise ManagerError("Für Geyser muss ein Bedrock-Port gesetzt sein; ändere das Profil mit mcsc ports.")
        if bedrock_port == profile.ports.java_tcp:
            raise ManagerError("Der Geyser-Port benötigt TCP und UDP und darf nicht mit dem Java-TCP-Port kollidieren.")

    def prepare_geyser_install(self, selector: str, *, allow_beta: bool = False) -> GeyserInstallPlan:
        profile = self.get_profile(selector)
        self._validate_geyser_profile(profile)
        return self.geyser.prepare(profile, allow_beta=allow_beta)

    def install_geyser(self, selector: str, plan: GeyserInstallPlan) -> GeyserInstallResult:
        profile = self.get_profile(selector)
        self._validate_geyser_profile(profile)
        return self.geyser.install(profile, plan)

    def configure_geyser(self, selector: str) -> Path:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Stoppe den Server vor der Konfiguration von Geyser.")
        if profile.server_type != "fabric":
            raise ManagerError("Geyser-Fabric lässt sich nur auf einem reinen Fabric-Profil konfigurieren.")
        return self.geyser.configure(profile)

    def update_ports(self, selector: str, ports: PortSettings) -> ServerProfile:
        config = self.load()
        profile = config.find_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Stoppe den Server vor dem Ändern der Ports.")
        properties = read_properties(Path(profile.server_dir) / "server.properties")
        if properties.get("enable-rcon", "false").lower() == "true":
            try:
                rcon_port = int(properties.get("rcon.port", ""))
            except ValueError as exc:
                raise ManagerError("Der konfigurierte RCON-Port ist ungültig; ändere zuerst die RCON-Einstellungen.") from exc
            if ports.java_tcp == rcon_port:
                raise ManagerError("Java- und lokaler RCON-Port dürfen nicht identisch sein.")
        profile.ports = ports
        update_properties(Path(profile.server_dir) / "server.properties", {"server-port": ports.java_tcp})
        self.save(config)
        return profile

    def accept_eula(self, selector: str, accepted: bool) -> ServerProfile:
        if not accepted:
            raise ManagerError("Ohne Zustimmung zur Minecraft-EULA wird der Server nicht gestartet.")
        config = self.load()
        profile = config.find_profile(selector)
        eula = Path(profile.server_dir) / "eula.txt"
        eula.write_text("# Minecraft EULA bestätigt durch den Serverbetreiber\neula=true\n", encoding="utf-8")
        profile.eula_accepted = True
        self.save(config)
        return profile

    def active_profiles(self) -> list[ServerProfile]:
        if not self.services.supported():
            return []
        return [profile for profile in self.load().profiles if self.services.is_active(profile)]

    def start(self, selector: str, *, confirm_linger: Callable[[], bool]) -> ServerProfile:
        profile = self.get_profile(selector)
        if profile.server_type not in IMPLEMENTED_SERVER_TYPES:
            raise ManagerError(f"Servertyp '{profile.server_type}' wird noch nicht unterstützt.")
        try:
            validate_profile_launch(profile)
        except LaunchConfigurationError as exc:
            raise ManagerError(f"Serverinstallation ist unvollständig: {exc}") from exc
        if not profile.eula_accepted:
            raise ManagerError("Die Minecraft-EULA ist noch nicht bestätigt. Starte erneut und bestätige sie zuerst.")
        if not self.services.supported():
            raise ManagerError("Der Hintergrundbetrieb benötigt in dieser ersten Version Linux mit systemd.")
        others = [item for item in self.active_profiles() if item.id != profile.id]
        if others:
            names = ", ".join(item.name for item in others)
            raise ManagerError(f"Es läuft bereits ein anderer Server ({names}). Stoppe ihn zuerst.")
        java = self.runtimes.ensure(profile.java_major)
        needs_linger_confirmation = not self.services.linger_enabled()
        self.services.start(profile, java, confirm_linger=confirm_linger() if needs_linger_confirmation else True)
        return profile

    def stop(self, selector: str) -> ServerProfile:
        profile = self.get_profile(selector)
        self.services.stop(profile)
        return profile

    def set_autostart(self, selector: str, enabled: bool) -> ServerProfile:
        config = self.load()
        profile = config.find_profile(selector)
        previous = profile.auto_start_on_boot
        profile.auto_start_on_boot = enabled
        self.save(config)
        try:
            self.services.set_autostart(profile, enabled)
        except Exception:
            profile.auto_start_on_boot = previous
            self.save(config)
            raise
        return profile

    def update_settings(self, selector: str, settings: dict[str, object]) -> ServerProfile:
        allowed = {
            "motd",
            "max-players",
            "difficulty",
            "gamemode",
            "pvp",
            "white-list",
            "view-distance",
            "simulation-distance",
            "spawn-protection",
        }
        unknown = set(settings) - allowed
        if unknown:
            raise ManagerError(f"Diese Servereinstellung darf nicht über das Menü geändert werden: {', '.join(sorted(unknown))}")
        config = self.load()
        profile = config.find_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Geführte Einstellungen können nur bei gestopptem Server geändert werden.")
        normalized: dict[str, object] = {}
        for key, value in settings.items():
            if key == "motd":
                text = str(value).strip()
                if not text or len(text) > 256 or "\n" in text or "\r" in text:
                    raise ManagerError("Die MOTD muss 1 bis 256 Zeichen enthalten und darf keine Zeilenumbrüche enthalten.")
                normalized[key] = text
            elif key == "max-players":
                number = int(value)
                if not 1 <= number <= 1000:
                    raise ManagerError("Maximale Spielerzahl muss zwischen 1 und 1000 liegen.")
                normalized[key] = number
            elif key == "difficulty":
                choice = str(value).lower()
                if choice not in {"peaceful", "easy", "normal", "hard"}:
                    raise ManagerError("Schwierigkeit muss peaceful, easy, normal oder hard sein.")
                normalized[key] = choice
            elif key == "gamemode":
                choice = str(value).lower()
                if choice not in {"survival", "creative", "adventure", "spectator"}:
                    raise ManagerError("Spielmodus muss survival, creative, adventure oder spectator sein.")
                normalized[key] = choice
            elif key in {"pvp", "white-list"}:
                text = str(value).lower()
                if text not in {"true", "false", "yes", "no", "1", "0"}:
                    raise ManagerError(f"{key} muss an/aus sein.")
                normalized[key] = "true" if text in {"true", "yes", "1"} else "false"
            else:
                number = int(value)
                bounds = (2, 32) if key in {"view-distance", "simulation-distance"} else (0, 64)
                if not bounds[0] <= number <= bounds[1]:
                    raise ManagerError(f"{key} muss zwischen {bounds[0]} und {bounds[1]} liegen.")
                normalized[key] = number
        update_properties(Path(profile.server_dir) / "server.properties", normalized)
        return profile

    def configure_resource_pack(
        self,
        selector: str,
        *,
        url: str,
        sha1: str,
        required: bool,
    ) -> Path:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Stoppe den Server vor der Konfiguration des Ressourcenpakets.")

        resource_url = url.strip()
        if not resource_url or any(ord(char) < 32 or ord(char) == 127 for char in resource_url):
            raise ManagerError("Die Ressourcenpaket-URL ist leer oder enthält Steuerzeichen.")
        try:
            parsed_url = urlsplit(resource_url)
            parsed_url.port  # Validate an explicitly supplied port.
        except ValueError as exc:
            raise ManagerError("Die Ressourcenpaket-URL enthält einen ungültigen Port.") from exc
        if (
            parsed_url.scheme.lower() != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.fragment
        ):
            raise ManagerError("Das Ressourcenpaket benötigt eine HTTPS-URL ohne Zugangsdaten oder Fragment.")
        normalized_sha1 = sha1.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", normalized_sha1):
            raise ManagerError("Der SHA-1-Hash des Ressourcenpakets muss aus genau 40 Hexadezimalzeichen bestehen.")
        if not isinstance(required, bool):
            raise ManagerError("Die Ressourcenpaket-Pflicht muss ausdrücklich true oder false sein.")

        server_dir = Path(profile.server_dir).expanduser()
        if server_dir.is_symlink():
            raise ManagerError("Der Serverordner darf für die Ressourcenpaket-Konfiguration kein symbolischer Link sein.")
        try:
            root = server_dir.resolve(strict=True)
            configured_root = Path(self.load().servers_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ManagerError(f"Der Server- oder Profilordner kann nicht sicher aufgelöst werden: {exc}") from exc
        if not root.is_dir() or root.parent != configured_root:
            raise ManagerError("Der Serverordner liegt nicht direkt innerhalb des konfigurierten Profilordners.")
        properties_path = root / "server.properties"
        if properties_path.is_symlink():
            raise ManagerError("server.properties darf für die Ressourcenpaket-Konfiguration kein symbolischer Link sein.")
        if properties_path.exists() and not properties_path.is_file():
            raise ManagerError("server.properties ist keine reguläre Datei.")
        if properties_path.exists() and properties_path.resolve().parent != root:
            raise ManagerError("server.properties liegt nicht sicher innerhalb des Serverprofils.")

        update_properties(
            properties_path,
            {
                "resource-pack": resource_url,
                "resource-pack-sha1": normalized_sha1,
                "require-resource-pack": str(required).lower(),
            },
        )
        return properties_path

    @staticmethod
    def _rcon_secret_path(profile: ServerProfile) -> Path:
        return Path(profile.server_dir) / "config" / "rcon-password"

    @staticmethod
    def _write_rcon_secret(path: Path, password: str) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise ManagerError("Der RCON-Geheimnisordner darf kein symbolischer Link sein.")
        temp_path: Path | None = None
        try:
            if os.name != "nt":
                os.chmod(path.parent, 0o700)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".rcon-", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(password + chr(10))
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except OSError as exc:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            raise ManagerError(f"RCON-Passwort konnte nicht sicher gespeichert werden: {exc}") from exc

    @staticmethod
    def _read_rcon_secret(path: Path) -> str:
        if path.is_symlink():
            raise ManagerError("Das gespeicherte RCON-Passwort darf kein symbolischer Link sein.")
        try:
            info = path.stat()
        except OSError as exc:
            raise ManagerError(f"RCON-Passwortdatei kann nicht gelesen werden: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ManagerError("Die RCON-Passwortdatei ist keine reguläre Datei.")
        try:
            if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
                os.chmod(path, 0o600)
            password = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ManagerError(f"RCON-Passwortdatei kann nicht sicher gelesen werden: {exc}") from exc
        if not password or chr(10) in password or chr(13) in password:
            raise ManagerError("Die RCON-Passwortdatei enthält kein gültiges Passwort.")
        return password

    def rcon_is_enabled(self, selector: str) -> bool:
        profile = self.get_profile(selector)
        properties = read_properties(Path(profile.server_dir) / "server.properties")
        secret = self._rcon_secret_path(profile)
        return (
            properties.get("enable-rcon", "false").lower() == "true"
            and properties.get("rcon.ip") == "127.0.0.1"
            and secret.is_file()
            and not secret.is_symlink()
        )

    def enable_rcon(self, selector: str) -> ServerProfile:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("RCON kann nur bei gestopptem Server eingerichtet werden.")
        if profile.server_type != "vanilla":
            raise ManagerError("RCON-Konfiguration ist für diesen Servertyp noch nicht auf Kompatibilität geprüft.")
        if profile.ports.java_tcp == DEFAULT_RCON_PORT:
            raise ManagerError(
                f"Der Java-Port belegt bereits den lokalen Standard-RCON-Port {DEFAULT_RCON_PORT}; ändere zuerst den Java-Port."
            )
        if not port_available(DEFAULT_RCON_PORT, "tcp", "127.0.0.1"):
            raise ManagerError(f"Der lokale RCON-Port {DEFAULT_RCON_PORT}/TCP ist bereits belegt.")

        secret_path = self._rcon_secret_path(profile)
        previous_secret: str | None = None
        if secret_path.exists():
            previous_secret = self._read_rcon_secret(secret_path)
        password = secrets.token_urlsafe(32)
        properties_path = Path(profile.server_dir) / "server.properties"
        try:
            self._write_rcon_secret(secret_path, password)
            update_properties(
                properties_path,
                {
                    "enable-rcon": "true",
                    "rcon.port": DEFAULT_RCON_PORT,
                    "rcon.ip": "127.0.0.1",
                    "rcon.password": password,
                },
            )
        except Exception:
            if previous_secret is None:
                secret_path.unlink(missing_ok=True)
            else:
                self._write_rcon_secret(secret_path, previous_secret)
            raise
        return profile

    def disable_rcon(self, selector: str) -> ServerProfile:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("RCON kann nur bei gestopptem Server deaktiviert werden.")
        update_properties(
            Path(profile.server_dir) / "server.properties",
            {"enable-rcon": "false", "rcon.password": "", "rcon.ip": "127.0.0.1"},
        )
        secret_path = self._rcon_secret_path(profile)
        if secret_path.is_symlink():
            raise ManagerError("Das gespeicherte RCON-Passwort ist ein symbolischer Link; bitte manuell prüfen.")
        secret_path.unlink(missing_ok=True)
        return profile

    def _rcon_client(self, profile: ServerProfile) -> RconClient:
        if not self.services.is_active(profile):
            raise ManagerError("Der Server muss laufen, um RCON zu verwenden.")
        properties = read_properties(Path(profile.server_dir) / "server.properties")
        if properties.get("enable-rcon", "false").lower() != "true":
            raise ManagerError("RCON ist nicht aktiviert. Stoppe den Server und führe 'mcsc rcon enable' aus.")
        if properties.get("rcon.ip") != "127.0.0.1":
            raise ManagerError("RCON ist nicht an 127.0.0.1 gebunden und wird aus Sicherheitsgründen nicht verwendet.")
        try:
            port = int(properties.get("rcon.port", ""))
        except ValueError as exc:
            raise ManagerError("Der konfigurierte RCON-Port ist ungültig.") from exc
        if not 1 <= port <= 65535 or port == profile.ports.java_tcp:
            raise ManagerError("Der lokale RCON-Port ist ungültig oder kollidiert mit dem Java-Port.")
        password = self._read_rcon_secret(self._rcon_secret_path(profile))
        if properties.get("rcon.password") != password:
            raise ManagerError("RCON-Passwortdatei und server.properties stimmen nicht überein; RCON neu einrichten.")
        try:
            return RconClient("127.0.0.1", port, password)
        except RconError as exc:
            raise ManagerError(str(exc)) from exc

    def rcon_players(self, selector: str) -> str:
        profile = self.get_profile(selector)
        try:
            with self._rcon_client(profile) as client:
                return client.command("list")
        except RconError as exc:
            raise ManagerError(str(exc)) from exc

    def rcon_player_stats(self, selector: str, player: str) -> str:
        profile = self.get_profile(selector)
        reason = player_data_disabled_reason(profile.server_type, profile.minecraft_version)
        if reason:
            raise ManagerError(reason)
        try:
            player = validate_player_name(player)
        except RconError as exc:
            raise ManagerError(str(exc)) from exc
        target = f"@a[name={player},limit=1]"
        commands = (
            ("Gesundheit", f"data get entity {target} Health"),
            ("Hunger", f"data get entity {target} foodLevel"),
            ("Erfahrungslevel", f"data get entity {target} XpLevel"),
            ("Erfahrungspunkte", f"data get entity {target} XpTotal"),
            ("Scoreboard-Werte", f"scoreboard players list {player}"),
        )
        try:
            with self._rcon_client(profile) as client:
                return chr(10).join(f"{label}: {client.command(command)}" for label, command in commands)
        except RconError as exc:
            raise ManagerError(str(exc)) from exc

    def rcon_inventory(self, selector: str, player: str) -> str:
        profile = self.get_profile(selector)
        reason = player_data_disabled_reason(profile.server_type, profile.minecraft_version)
        if reason:
            raise ManagerError(reason)
        try:
            player = validate_player_name(player)
        except RconError as exc:
            raise ManagerError(str(exc)) from exc
        target = f"@a[name={player},limit=1]"
        try:
            with self._rcon_client(profile) as client:
                return client.command(f"data get entity {target} Inventory")
        except RconError as exc:
            raise ManagerError(str(exc)) from exc

    def rcon_set_inventory_item(self, selector: str, player: str, slot: int, item_id: str, count: int = 1) -> str:
        profile = self.get_profile(selector)
        reason = inventory_editing_disabled_reason(profile.server_type, profile.minecraft_version)
        if reason:
            raise ManagerError(reason)
        try:
            player = validate_player_name(player)
            normalized_item = normalize_item_id(item_id)
        except RconError as exc:
            raise ManagerError(str(exc)) from exc
        if isinstance(slot, bool) or not 0 <= int(slot) <= 35:
            raise ManagerError("Der Inventarplatz muss eine Zahl zwischen 0 und 35 sein.")
        if isinstance(count, bool) or not 1 <= int(count) <= 64:
            raise ManagerError("Die Stückzahl muss zwischen 1 und 64 liegen.")
        target = f"@a[name={player},limit=1]"
        slot_number = int(slot)
        command_slot = f"hotbar.{slot_number}" if slot_number <= 8 else f"inventory.{slot_number - 9}"
        command = f"item replace entity {target} {command_slot} with {normalized_item} {int(count)}"
        try:
            with self._rcon_client(profile) as client:
                return client.command(command)
        except RconError as exc:
            raise ManagerError(str(exc)) from exc

    def regenerate_world(
        self,
        selector: str,
        *,
        seed: str | None,
        create_backup: bool,
    ) -> tuple[str, Path | None]:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Stoppe den Server, bevor du die Welt neu generierst.")

        raw_server_dir = Path(profile.server_dir).expanduser()
        if raw_server_dir.is_symlink():
            raise ManagerError("Der Serverordner darf für eine Welt-Neugenerierung kein symbolischer Link sein.")
        server_dir = raw_server_dir.resolve()
        if not server_dir.is_dir():
            raise ManagerError(f"Serverordner wurde nicht gefunden: {server_dir}")
        properties_path = server_dir / "server.properties"
        if properties_path.is_symlink():
            raise ManagerError("server.properties darf für eine Welt-Neugenerierung kein symbolischer Link sein.")

        properties = read_properties(properties_path)
        world_name = properties.get("level-name", "world").strip() or "world"
        world_component = Path(world_name)
        if (
            world_component.is_absolute()
            or len(world_component.parts) != 1
            or world_name in {".", ".."}
            or "/" in world_name
            or "\\" in world_name
        ):
            raise ManagerError("Der level-name ist kein sicherer Ordnername; die Welt wurde nicht verändert.")

        seed_value = ""
        if seed is not None:
            seed_value = str(seed).strip()
            if len(seed_value) > 128 or any(ord(character) < 32 or ord(character) == 127 for character in seed_value):
                raise ManagerError("Ein Welt-Seed darf höchstens 128 Zeichen lang sein und keine Steuerzeichen enthalten.")
            if not seed_value:
                seed_value = ""

        world_dirs = (
            server_dir / world_name,
            server_dir / f"{world_name}_nether",
            server_dir / f"{world_name}_the_end",
        )
        for world_dir in world_dirs:
            if world_dir.is_symlink():
                raise ManagerError(f"Weltordner darf kein symbolischer Link sein: {world_dir}")
            if os.path.ismount(world_dir):
                raise ManagerError(f"Weltordner darf kein separater Mountpoint sein: {world_dir}")
            if world_dir.exists():
                if not world_dir.is_dir() or world_dir.resolve().parent != server_dir:
                    raise ManagerError(f"Weltpfad ist kein sicherer Ordner innerhalb des Serverprofils: {world_dir}")

        backup_path = self.backup(profile.slug) if create_backup else None
        update_properties(properties_path, {"level-seed": seed_value})
        try:
            for world_dir in world_dirs:
                if world_dir.is_dir():
                    shutil.rmtree(world_dir)
        except OSError as exc:
            detail = f"Ein oder mehrere Weltordner konnten nicht entfernt werden: {exc}"
            if backup_path:
                detail += f" Das vorherige Server-Backup liegt unter {backup_path}."
            raise ManagerError(detail) from exc
        return world_name, backup_path

    def backup(self, selector: str, *, progress: Callable[[str], None] | None = None) -> Path:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Für ein konsistentes Backup muss der Server zuerst gestoppt werden.")
        server_dir = Path(profile.server_dir)
        backup_dir = server_dir / "backups"
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(backup_dir, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f-UTC")
        archive_path = backup_dir / f"{profile.slug}-{stamp}.tar.gz"
        archive_created = False
        archive_complete = False

        try:
            if progress:
                progress("Vollständiges Backup: ermittle Umfang des Serverprofils (backups/ und logs/ ausgenommen) …")
            total_bytes, file_count, entry_count = _measure_backup(server_dir, progress)
            if progress:
                progress(
                    f"Vollständiges Backup: {file_count} Dateien, {entry_count} Einträge, "
                    f"{_format_backup_size(total_bytes)} zu lesen; Komprimierung startet."
                )

            completed_bytes = 0
            completed_entries = 0
            current_member = "Vorbereitung"
            started = time.monotonic()
            last_report = started

            def emit_progress(*, force: bool = False) -> None:
                nonlocal last_report
                if progress is None:
                    return
                now = time.monotonic()
                if not force and now - last_report < 1.0:
                    return
                if total_bytes:
                    percentage = min(100.0, completed_bytes * 100.0 / total_bytes)
                elif entry_count:
                    percentage = min(100.0, completed_entries * 100.0 / entry_count)
                else:
                    percentage = 100.0
                elapsed = max(now - started, 0.001)
                rate = completed_bytes / elapsed
                progress(
                    f"Backup-Fortschritt: {percentage:5.1f}% — "
                    f"{_format_backup_size(completed_bytes)} / {_format_backup_size(total_bytes)}, "
                    f"{_format_backup_size(rate)}/s; Einträge {completed_entries}/{entry_count}; "
                    f"aktuell: {current_member}"
                )
                last_report = now

            def on_entry(member: str) -> None:
                nonlocal completed_entries, current_member
                completed_entries += 1
                current_member = member
                emit_progress()

            def on_bytes(member: str, amount: int) -> None:
                nonlocal completed_bytes, current_member
                completed_bytes += amount
                current_member = member
                emit_progress()

            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(archive_path, flags, 0o600)
            archive_created = True
            os.close(descriptor)
            with tarfile.open(archive_path, "w:gz") as archive:
                for child in sorted(server_dir.iterdir(), key=lambda item: item.name):
                    if child.name in {"backups", "logs"}:
                        continue
                    _add_backup_path(
                        archive,
                        child,
                        child.name,
                        on_entry,
                        on_bytes,
                    )
            if os.name != "nt":
                os.chmod(archive_path, 0o600)
            archive_complete = True
            emit_progress(force=True)
            if progress:
                progress(
                    f"Vollständiges Backup abgeschlossen: {archive_path} "
                    f"({_format_backup_size(archive_path.stat().st_size)} komprimiert)."
                )
        except KeyboardInterrupt:
            if archive_created and not archive_complete:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if progress:
                progress("Backup-Abbruch erkannt; eine unvollständige Archivdatei wurde entfernt, sofern vorhanden.")
            raise
        except (OSError, tarfile.TarError) as exc:
            if archive_created and not archive_complete:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ManagerError(f"Backup fehlgeschlagen: {exc}") from exc
        except BaseException:
            if archive_created and not archive_complete:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return archive_path

    def set_memory(self, selector: str, memory_mb: int) -> ServerProfile:
        if memory_mb < 1024:
            raise ManagerError("Mindestens 1024 MB RAM müssen zugewiesen werden.")
        config = self.load()
        profile = config.find_profile(selector)
        profile.memory_mb = memory_mb
        self.save(config)
        # Refresh the unit on the next start; it is deliberately not restarted here.
        return profile

    @staticmethod
    def validate_port_values(java_tcp: int, bedrock_udp: int | None, voice_udp: int | None) -> PortSettings:
        return PortSettings(
            java_tcp=int(validate_port(java_tcp, "Java-TCP-Port")),
            bedrock_udp=validate_port(bedrock_udp, "Bedrock/Geyser-Port", optional=True),
            voice_udp=validate_port(voice_udp, "Voicechat-UDP-Port", optional=True),
        )
