from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tempfile

from .catalog import CatalogError, ContentCatalog, InstallPlan, NoCompatibleVersionError
from .models import ServerProfile

GEYSER_PROJECT_ID = "wKkoqHrH"
FABRIC_API_PROJECT_ID = "P7dR8mSH"


class GeyserError(RuntimeError):
    """Raised when Geyser cannot be safely installed or configured."""


class GeyserBetaOptInRequired(GeyserError):
    """Raised when the only matching Geyser-Fabric build is a beta."""


@dataclass(frozen=True)
class PlannedMod:
    plan: InstallPlan | None
    path: Path
    version: str
    release_channel: str
    already_installed: bool = False


@dataclass(frozen=True)
class GeyserInstallPlan:
    profile_id: str
    server_dir: str
    minecraft_version: str
    bedrock_port: int
    allow_beta: bool
    geyser: PlannedMod
    fabric_api: PlannedMod


@dataclass(frozen=True)
class GeyserInstallResult:
    geyser_path: Path
    geyser_version: str
    geyser_release_channel: str
    geyser_already_installed: bool
    fabric_api_path: Path
    fabric_api_version: str
    fabric_api_already_installed: bool
    configuration_path: Path | None = None


class GeyserInstaller:
    """Install the official Modrinth Geyser-Fabric build and its Fabric API dependency."""

    def __init__(self, catalog: ContentCatalog | None = None) -> None:
        self.catalog = catalog or ContentCatalog()

    @staticmethod
    def _root(profile: ServerProfile) -> Path:
        root = Path(profile.server_dir).expanduser().resolve()
        if not root.is_dir():
            raise GeyserError(f"Serverordner wurde nicht gefunden: {root}")
        return root

    def _mods_dir(self, profile: ServerProfile) -> Path:
        root = self._root(profile)
        mods_dir = root / "mods"
        if mods_dir.is_symlink():
            raise GeyserError("Der Mods-Ordner darf kein symbolischer Link sein.")
        if mods_dir.exists() and not mods_dir.is_dir():
            raise GeyserError("Der Mods-Pfad ist kein Verzeichnis.")
        if mods_dir.exists() and mods_dir.resolve().parent != root:
            raise GeyserError("Der Mods-Ordner liegt nicht sicher im Serverprofil.")
        return mods_dir

    def _managed_mod(
        self,
        profile: ServerProfile,
        project_id: str,
        filename_prefix: str,
    ) -> PlannedMod | None:
        root = self._root(profile)
        mods_dir = self._mods_dir(profile)
        entries = tuple(mods_dir.iterdir()) if mods_dir.is_dir() else ()
        records = [
            record
            for record in self.catalog.installed(profile)
            if record.get("kind") == "mod" and str(record.get("project_id")) == project_id
        ]
        if len(records) > 1:
            raise GeyserError(f"Mehrere katalogverwaltete Dateien für Projekt {project_id} gefunden; prüfe den Mods-Ordner.")

        if records:
            record = records[0]
            if record.get("minecraft_version") != profile.minecraft_version:
                raise GeyserError(
                    f"Die registrierte Moddatei {record.get('path', '?')} passt nicht mehr zu Minecraft "
                    f"{profile.minecraft_version}; prüfe sie vor der Installation."
                )
            relative = record.get("path")
            if not isinstance(relative, str):
                raise GeyserError("Die Katalogliste enthält keinen gültigen Modpfad.")
            relative_path = Path(relative)
            if (
                relative_path.is_absolute()
                or len(relative_path.parts) != 2
                or relative_path.parts[0] != "mods"
                or any(part in {"", ".", ".."} for part in relative_path.parts)
            ):
                raise GeyserError("Die Katalogliste enthält einen unsicheren Modpfad.")
            path = root / relative_path
            if path.is_symlink():
                raise GeyserError(f"Die katalogverwaltete Moddatei darf kein symbolischer Link sein: {path}")
            if not path.is_file() or path.resolve().parent != mods_dir.resolve():
                raise GeyserError(f"Die Katalogliste verweist auf eine fehlende oder unsichere Moddatei: {path}")
            if not path.name.lower().startswith(filename_prefix.lower()):
                raise GeyserError(f"Die katalogverwaltete Datei für Projekt {project_id} hat einen unerwarteten Namen.")
            duplicates = [
                child
                for child in entries
                if child != path
                and child.name.lower().startswith(filename_prefix.lower())
                and (child.suffix.lower() == ".jar" or child.is_symlink())
            ]
            if duplicates:
                names = ", ".join(child.name for child in duplicates)
                raise GeyserError(f"Zusätzliche unverwaltete Moddatei(en) gefunden: {names}.")
            return PlannedMod(
                None,
                path,
                str(record.get("file_version") or "unbekannt"),
                str(record.get("release_channel") or "release"),
                already_installed=True,
            )

        unmanaged = [
            child
            for child in entries
            if child.name.lower().startswith(filename_prefix.lower())
            and (child.suffix.lower() == ".jar" or child.is_symlink())
        ]
        if unmanaged:
            names = ", ".join(child.name for child in unmanaged)
            raise GeyserError(
                f"Unverwaltete passende Moddatei(en) gefunden: {names}. Entferne sie oder registriere sie erst im Katalog, "
                "damit keine doppelte/unkompatible Version installiert wird."
            )
        return None

    @staticmethod
    def _planned_mod(plan: InstallPlan, mods_dir: Path) -> PlannedMod:
        path = mods_dir / plan.file_name
        if path.exists() or path.is_symlink():
            raise GeyserError(f"Die Zieldatei existiert bereits und wird nicht überschrieben: {path}")
        return PlannedMod(plan, path, plan.file_version, plan.release_channel)

    def prepare(self, profile: ServerProfile, *, allow_beta: bool = False) -> GeyserInstallPlan:
        if profile.server_type != "fabric":
            raise GeyserError("Geyser-Fabric lässt sich nur auf einem reinen Fabric-Profil installieren.")
        if profile.ports.bedrock_udp is None:
            raise GeyserError("Für Geyser muss ein Bedrock-Port gesetzt sein; ändere das Profil mit mcsc ports.")
        if profile.ports.bedrock_udp == profile.ports.java_tcp:
            raise GeyserError("Der Geyser-Port nutzt TCP und UDP und darf nicht mit dem Java-TCP-Port kollidieren.")

        mods_dir = self._mods_dir(profile)
        geyser = self._managed_mod(profile, GEYSER_PROJECT_ID, "geyser-fabric")
        if geyser is None:
            try:
                geyser_plan = self.catalog.prepare_install("modrinth", "mod", GEYSER_PROJECT_ID, profile)
            except NoCompatibleVersionError as no_stable_build:
                try:
                    geyser_plan = self.catalog.prepare_install(
                        "modrinth", "mod", GEYSER_PROJECT_ID, profile, include_beta=True
                    )
                except NoCompatibleVersionError as no_beta_build:
                    raise no_beta_build from no_stable_build
                if geyser_plan.release_channel == "beta" and not allow_beta:
                    raise GeyserBetaOptInRequired(
                        "Für diese Fabric-/Minecraft-Version wurde kein stabiler Geyser-Fabric-Build gefunden. "
                        "Der passende offizielle Build ist als Beta markiert; bestätige das ausdrücklich mit --allow-beta."
                    ) from no_stable_build
            if geyser_plan.release_channel == "beta" and not allow_beta:
                raise GeyserBetaOptInRequired(
                    "Der passende Geyser-Fabric-Build ist als Beta markiert; bestätige das ausdrücklich mit --allow-beta."
                )
            geyser = self._planned_mod(geyser_plan, mods_dir)

        fabric_api = self._managed_mod(profile, FABRIC_API_PROJECT_ID, "fabric-api")
        if fabric_api is None:
            api_plan = self.catalog.prepare_install("modrinth", "mod", FABRIC_API_PROJECT_ID, profile)
            if api_plan.release_channel != "release":
                raise GeyserError("Für Fabric API wurde keine stabile Version gefunden; Beta-Abhängigkeiten werden nicht automatisch installiert.")
            fabric_api = self._planned_mod(api_plan, mods_dir)

        return GeyserInstallPlan(
            profile_id=profile.id,
            server_dir=str(self._root(profile)),
            minecraft_version=profile.minecraft_version,
            bedrock_port=int(profile.ports.bedrock_udp),
            allow_beta=allow_beta,
            geyser=geyser,
            fabric_api=fabric_api,
        )

    def install(self, profile: ServerProfile, plan: GeyserInstallPlan) -> GeyserInstallResult:
        if (
            plan.profile_id != profile.id
            or plan.server_dir != str(self._root(profile))
            or plan.minecraft_version != profile.minecraft_version
            or profile.ports.bedrock_udp != plan.bedrock_port
        ):
            raise GeyserError("Die vorbereitete Geyser-Installation passt nicht mehr zu diesem Serverprofil.")

        # Fabric API is installed first so a transient Geyser download error never leaves a
        # Geyser jar that cannot load. The API is harmless on its own if the second download
        # needs to be retried.
        api_path = plan.fabric_api.path
        if plan.fabric_api.plan is not None:
            api_path = self.catalog.install(plan.fabric_api.plan, profile)
        geyser_path = plan.geyser.path
        if plan.geyser.plan is not None:
            geyser_path = self.catalog.install(plan.geyser.plan, profile, allow_beta=plan.allow_beta)

        config_path: Path | None = None
        if self.config_path(profile).is_file():
            config_path = self.configure(profile)
        return GeyserInstallResult(
            geyser_path=geyser_path,
            geyser_version=plan.geyser.version,
            geyser_release_channel=plan.geyser.release_channel,
            geyser_already_installed=plan.geyser.already_installed,
            fabric_api_path=api_path,
            fabric_api_version=plan.fabric_api.version,
            fabric_api_already_installed=plan.fabric_api.already_installed,
            configuration_path=config_path,
        )

    @staticmethod
    def config_path(profile: ServerProfile) -> Path:
        return Path(profile.server_dir).expanduser() / "config" / "Geyser-Fabric" / "config.yml"

    @staticmethod
    def _patch_bedrock_settings(text: str, port: int) -> str:
        lines = text.splitlines(keepends=True)
        section_pattern = re.compile(r"^bedrock\s*:\s*(?:#.*)?(?:\r?\n)?$")
        starts = [index for index, line in enumerate(lines) if section_pattern.match(line)]
        if len(starts) != 1:
            raise GeyserError("Die Geyser-Konfiguration muss genau einen top-level-Abschnitt 'bedrock:' enthalten.")
        start = starts[0]
        if not lines[start].endswith(("\n", "\r")):
            lines[start] += "\n"
        end = len(lines)
        top_level_pattern = re.compile(r"^[A-Za-z0-9_.-]+\s*:\s*(?:#.*)?(?:\r?\n)?$")
        for index in range(start + 1, len(lines)):
            if top_level_pattern.match(lines[index]):
                end = index
                break

        child_indents: list[int] = []
        for line in lines[start + 1 : end]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.match(r"^(\s+)[A-Za-z0-9_-]+\s*:", line)
            if match:
                child_indents.append(len(match.group(1)))
        indent_size = min(child_indents) if child_indents else 2
        if indent_size <= 0:
            raise GeyserError("Der 'bedrock:'-Abschnitt hat eine ungültige Einrückung.")

        updated: dict[str, int] = {}
        child_pattern = re.compile(r"^(\s+)(port|transport)\s*:\s*(.*?)(\r?\n)?$")
        for index in range(start + 1, end):
            match = child_pattern.match(lines[index])
            if not match or len(match.group(1)) != indent_size:
                continue
            key = match.group(2)
            if key in updated:
                raise GeyserError(f"Die Geyser-Konfiguration enthält den Eintrag '{key}' mehrfach.")
            raw_value = match.group(3)
            value, marker, comment = raw_value.partition("#")
            desired = str(port) if key == "port" else "nethernet"
            suffix = f"  #{comment}" if marker else ""
            newline = match.group(4) or "\n"
            lines[index] = f"{match.group(1)}{key}: {desired}{suffix}{newline}"
            updated[key] = index

        insertions: list[str] = []
        if "port" not in updated:
            insertions.append(f"{' ' * indent_size}port: {port}\n")
        if "transport" not in updated:
            insertions.append(f"{' ' * indent_size}transport: nethernet\n")
        if insertions:
            lines[start + 1 : start + 1] = insertions
        return "".join(lines)

    def configure(self, profile: ServerProfile) -> Path:
        if profile.ports.bedrock_udp is None:
            raise GeyserError("Für Geyser muss ein Bedrock-Port gesetzt sein.")
        if profile.ports.bedrock_udp == profile.ports.java_tcp:
            raise GeyserError("Der Geyser-Port benötigt TCP und UDP und darf nicht mit dem Java-TCP-Port kollidieren.")
        path = self.config_path(profile)
        root = self._root(profile)
        config_dir = root / "config"
        geyser_dir = config_dir / "Geyser-Fabric"
        if config_dir.is_symlink() or geyser_dir.is_symlink() or path.is_symlink():
            raise GeyserError("Geyser-Konfigurationsordner und -Datei dürfen keine symbolischen Links sein.")
        if not path.is_file():
            raise GeyserError(
                "Die Geyser-Konfiguration wurde noch nicht erzeugt. Starte den Server einmal, stoppe ihn danach "
                "und führe mcsc geyser configure <server> erneut aus."
            )
        if path.resolve().parent != geyser_dir.resolve() or geyser_dir.resolve().parent != config_dir.resolve():
            raise GeyserError("Die Geyser-Konfiguration liegt nicht sicher innerhalb des Serverprofils.")
        try:
            original = path.read_text(encoding="utf-8")
            updated = self._patch_bedrock_settings(original, int(profile.ports.bedrock_udp))
        except GeyserError:
            raise
        except (OSError, UnicodeError) as exc:
            raise GeyserError(f"Die Geyser-Konfiguration konnte nicht gelesen werden: {exc}") from exc
        if updated == original:
            return path

        temporary: Path | None = None
        mode = stat.S_IMODE(path.stat().st_mode)
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", dir=path.parent, prefix=".geyser-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        except OSError as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise GeyserError(f"Die Geyser-Konfiguration konnte nicht sicher gespeichert werden: {exc}") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)
        return path
