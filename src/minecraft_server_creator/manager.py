from __future__ import annotations

from datetime import datetime, timezone
import os
import secrets
import shutil
import stat
import tarfile
from pathlib import Path
import tempfile
import uuid
from typing import Callable

from .config import AppConfig, ConfigStore, data_directory
from .minecraft import MinecraftMetadataClient
from .loaders import FabricInstaller
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


class ServerManager:
    def __init__(
        self,
        store: ConfigStore | None = None,
        metadata: MinecraftMetadataClient | None = None,
        runtimes: RuntimeManager | None = None,
        services: SystemdUserService | None = None,
        fabric: FabricInstaller | None = None,
    ) -> None:
        self.store = store or ConfigStore()
        self.metadata = metadata or MinecraftMetadataClient()
        self.runtimes = runtimes or RuntimeManager(data_directory())
        self.services = services or SystemdUserService()
        self.fabric = fabric or FabricInstaller()

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
        ports: PortSettings | None = None,
        memory_mb: int = 4096,
        auto_start_on_boot: bool = False,
    ) -> ServerProfile:
        config = self.load()
        if server_type not in IMPLEMENTED_SERVER_TYPES:
            raise ManagerError(
                f"{server_type} ist bereits in der Planung, aber noch nicht implementiert. "
                "Aktuell sind Vanilla und Fabric verfügbar."
            )
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
            if server_type == "vanilla":
                self.metadata.install_vanilla(minecraft_version, temp_dir)
            elif server_type == "fabric":
                self.fabric.install(minecraft_version, version_info, temp_dir)
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
            temp_dir.rename(final_dir)
            profile = ServerProfile(
                id=profile_id,
                name=name.strip(),
                slug=slug,
                server_type=server_type,
                minecraft_version=minecraft_version,
                java_major=int(java_major),
                server_dir=str(final_dir),
                memory_mb=memory_mb,
                ports=ports,
                auto_start_on_boot=auto_start_on_boot,
            )
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
        server_dir = Path(profile.server_dir)
        if not (server_dir / "server.jar").is_file():
            raise ManagerError("server.jar fehlt. Das Profil muss erneut installiert werden.")
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

    def backup(self, selector: str) -> Path:
        profile = self.get_profile(selector)
        if self.services.is_active(profile):
            raise ManagerError("Für ein konsistentes Backup muss der Server zuerst gestoppt werden.")
        server_dir = Path(profile.server_dir)
        backup_dir = server_dir / "backups"
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f-UTC")
        archive_path = backup_dir / f"{profile.slug}-{stamp}.tar.gz"
        try:
            with tarfile.open(archive_path, "w:gz") as archive:
                for child in server_dir.iterdir():
                    if child.name in {"backups", "logs"}:
                        continue
                    archive.add(child, arcname=child.name)
        except (OSError, tarfile.TarError) as exc:
            archive_path.unlink(missing_ok=True)
            raise ManagerError(f"Backup fehlgeschlagen: {exc}") from exc
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
            bedrock_udp=validate_port(bedrock_udp, "Bedrock-UDP-Port", optional=True),
            voice_udp=validate_port(voice_udp, "Voicechat-UDP-Port", optional=True),
        )
