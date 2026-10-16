from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import zipfile

from .minecraft import USER_AGENT, download_file
from .models import ServerProfile

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
CURSEFORGE_GAME_ID = 432
CURSEFORGE_MOD_CLASS = 6
CURSEFORGE_PLUGIN_CLASS = 5
CURSEFORGE_LOADERS = {"forge": 1, "fabric": 4, "neoforge": 6}


class CatalogError(RuntimeError):
    """Raised when a catalogue result cannot be safely searched or installed."""


@dataclass(frozen=True)
class CatalogProject:
    source: str
    kind: str
    project_id: str
    slug: str
    title: str
    summary: str
    downloads: int
    url: str


@dataclass(frozen=True)
class InstallPlan:
    source: str
    kind: str
    project_id: str
    project_slug: str
    project_title: str
    project_url: str
    file_name: str
    file_version: str
    download_url: str
    checksum: str
    algorithm: str
    required_dependencies: tuple[str, ...]
    minecraft_version: str


SERVER_ENVIRONMENTS = frozenset(
    {
        "client_and_server",
        "client_only_server_optional",
        "server_only",
        "server_only_client_optional",
        "dedicated_server_only",
        "client_or_server",
        "client_or_server_prefers_both",
    }
)


def _allowed_profile_loader(profile: ServerProfile, kind: str) -> tuple[str, ...]:
    if kind not in {"mod", "plugin"}:
        raise CatalogError("Der Katalog unterstützt nur Mods und Plugins.")
    if kind == "plugin":
        if profile.server_type != "arclight":
            raise CatalogError("Plugins können hier nur auf Arclight-Profilen installiert werden.")
        # Arclight documents Bukkit/Spigot support, but not Paper's extended API.
        # Only those loaders are included to avoid presenting Paper-only files as compatible.
        return ("bukkit", "spigot")
    if profile.server_type == "vanilla":
        raise CatalogError("Vanilla unterstützt keine Loader-Mods. Wähle Fabric, Forge, NeoForge oder Arclight.")
    loader = profile.loader_family if profile.server_type == "arclight" else profile.server_type
    if loader not in {"fabric", "forge", "neoforge"}:
        raise CatalogError("Für dieses Serverprofil ist kein kompatibler Mod-Loader gespeichert.")
    return (loader,)


def _request_json(url: str, host: str, *, api_key: str | None = None) -> Any:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CatalogError(f"Kataloganfragen dürfen nur über HTTPS an {host} gesendet werden.")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30.0) as response:
            content = response.read(10_000_001)
        if len(content) > 10_000_000:
            raise CatalogError(f"Die Antwort von {host} überschreitet die zulässige Größe.")
        return json.loads(content.decode("utf-8"))
    except CatalogError:
        raise
    except HTTPError as exc:
        if host == "api.curseforge.com" and exc.code in {401, 403}:
            raise CatalogError(
                f"CurseForge hat die Anfrage abgelehnt (HTTP {exc.code}). Prüfe den API-Schlüssel in CURSEFORGE_API_KEY."
            ) from exc
        raise CatalogError(f"Der Katalog {host} antwortete mit HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Katalogdaten konnten nicht von {host} geladen werden: {exc}") from exc


def _safe_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename or len(filename) > 240:
        raise CatalogError("Der Katalog lieferte keinen gültigen Dateinamen.")
    windows_device = filename.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))
    }
    if (
        filename in {".", ".."}
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or any(char in filename for char in '<>:"|?*')
        or filename.endswith((".", " "))
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        or windows_device
        or not filename.lower().endswith(".jar")
    ):
        raise CatalogError("Es werden nur sichere, portable JAR-Dateinamen ohne Pfadbestandteile akzeptiert.")
    return filename


def _validate_download_url(source: str, url: Any) -> str:
    if not isinstance(url, str):
        raise CatalogError("Der Katalog lieferte keine Download-URL.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogError("Die Download-URL enthält einen ungültigen Port.") from exc
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CatalogError("Katalogdownloads müssen über eine sichere HTTPS-Adresse erfolgen.")
    if source == "modrinth" and host != "cdn.modrinth.com":
        raise CatalogError("Modrinth-Dateien werden nur von cdn.modrinth.com akzeptiert.")
    if source == "curseforge" and not host.endswith(".forgecdn.net"):
        raise CatalogError("CurseForge-Dateien werden nur von forgecdn.net akzeptiert.")
    if source not in {"modrinth", "curseforge"}:
        raise CatalogError("Unbekannte Downloadquelle.")
    return url


def _checksum(hashes: Any, *, source: str) -> tuple[str, str]:
    if source == "modrinth":
        if isinstance(hashes, dict):
            for algorithm in ("sha512", "sha1"):
                value = hashes.get(algorithm)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]+", value):
                    expected = 128 if algorithm == "sha512" else 40
                    if len(value) == expected:
                        return value.lower(), algorithm
    elif source == "curseforge" and isinstance(hashes, list):
        # CurseForge FileHash algo: 1 = SHA-1; 2 = MD5. Prefer the stronger SHA-1.
        by_algorithm = {1: ("sha1", 40), 2: ("md5", 32)}
        available: dict[str, str] = {}
        for item in hashes:
            if not isinstance(item, dict):
                continue
            spec = by_algorithm.get(item.get("algo"))
            value = item.get("value")
            if spec and isinstance(value, str) and len(value) == spec[1] and re.fullmatch(r"[0-9a-fA-F]+", value):
                available[spec[0]] = value.lower()
        for algorithm in ("sha1", "md5"):
            if algorithm in available:
                return available[algorithm], algorithm
    raise CatalogError("Für diese Datei fehlt eine unterstützte kryptografische Prüfsumme; Download abgebrochen.")


def _query_url(base: str, route: str, params: dict[str, Any]) -> str:
    return f"{base}{route}?{urlencode(params)}"


class ModrinthCatalog:
    def search(self, query: str, kind: str, profile: ServerProfile, limit: int) -> list[CatalogProject]:
        loaders = _allowed_profile_loader(profile, kind)
        facets: list[list[str]] = [
            [f"all_project_types:{kind}"],
            [f"versions:{profile.minecraft_version}"],
            [f"categories:{loader}" for loader in loaders],
        ]
        url = _query_url(
            f"{MODRINTH_API}/search",
            "",
            {
                "query": query,
                "facets": json.dumps(facets, separators=(",", ":")),
                "limit": str(limit),
                "index": "relevance",
            },
        )
        data = _request_json(url, "api.modrinth.com")
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise CatalogError("Modrinth lieferte ein unerwartetes Suchergebnis.")
        projects: list[CatalogProject] = []
        for hit in data["hits"]:
            if not isinstance(hit, dict):
                continue
            project_types = hit.get("all_project_types")
            if not isinstance(project_types, list):
                project_types = [hit.get("project_type")]
            if kind not in project_types and hit.get("project_type") != kind:
                continue
            environments = hit.get("environment")
            if not isinstance(environments, list) or not set(environments).intersection(SERVER_ENVIRONMENTS):
                continue
            project_id = str(hit.get("project_id") or "")
            slug = str(hit.get("slug") or project_id)
            if not project_id:
                continue
            projects.append(
                CatalogProject(
                    "modrinth",
                    kind,
                    project_id,
                    slug,
                    str(hit.get("title") or slug),
                    str(hit.get("description") or ""),
                    int(hit.get("downloads") or 0),
                    f"https://modrinth.com/{kind}/{slug}",
                )
            )
        return projects

    def prepare_install(self, project_id: str, kind: str, profile: ServerProfile) -> InstallPlan:
        loaders = _allowed_profile_loader(profile, kind)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
            raise CatalogError("Die Modrinth-Projekt-ID bzw. der Slug ist ungültig.")
        project = _request_json(f"{MODRINTH_API}/project/{project_id}", "api.modrinth.com")
        if not isinstance(project, dict):
            raise CatalogError(f"Das angegebene Modrinth-Projekt ist kein {kind}.")
        project_type = project.get("project_type")
        # Modrinth's primary project_type may stay "mod" even for projects with plugin
        # releases. The actual compatible loader on the selected version separates the types.
        if project_type not in {"mod", "plugin"}:
            raise CatalogError(f"Das angegebene Modrinth-Projekt ist kein {kind}.")
        params = {
            "loaders": json.dumps(list(loaders), separators=(",", ":")),
            "game_versions": json.dumps([profile.minecraft_version], separators=(",", ":")),
            "include_changelog": "false",
        }
        versions_url = _query_url(f"{MODRINTH_API}/project/{project_id}/version", "", params)
        versions = _request_json(versions_url, "api.modrinth.com")
        if not isinstance(versions, list):
            raise CatalogError("Modrinth lieferte eine ungültige Versionsliste.")
        for version in versions:
            if not isinstance(version, dict) or version.get("version_type") != "release":
                continue
            if version.get("status") not in {None, "listed"}:
                continue
            game_versions = version.get("game_versions")
            if not isinstance(game_versions, list) or profile.minecraft_version not in game_versions:
                continue
            loaders_data = version.get("loaders")
            if not isinstance(loaders_data, list):
                continue
            version_loaders = {str(item).lower() for item in loaders_data}
            if not version_loaders.intersection(loaders):
                continue
            if version.get("environment") not in SERVER_ENVIRONMENTS:
                continue
            files = version.get("files")
            if not isinstance(files, list):
                continue
            candidates = [item for item in files if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".jar")]
            if not candidates:
                continue
            selected = next((item for item in candidates if item.get("primary") is True), candidates[0])
            filename = _safe_filename(selected.get("filename"))
            checksum, algorithm = _checksum(selected.get("hashes"), source="modrinth")
            project_slug = str(project.get("slug") or project_id)
            dependencies_data = version.get("dependencies")
            if not isinstance(dependencies_data, list):
                dependencies_data = []
            dependencies = tuple(
                str(dependency.get("project_id") or dependency.get("file_name") or dependency.get("version_id"))
                for dependency in dependencies_data
                if isinstance(dependency, dict)
                and dependency.get("dependency_type") == "required"
                and (dependency.get("project_id") or dependency.get("file_name") or dependency.get("version_id"))
            )
            return InstallPlan(
                "modrinth",
                kind,
                str(project.get("id") or project_id),
                project_slug,
                str(project.get("title") or project_slug),
                f"https://modrinth.com/{kind}/{project_slug}",
                filename,
                str(version.get("version_number") or version.get("name") or version.get("id") or "release"),
                _validate_download_url("modrinth", selected.get("url")),
                checksum,
                algorithm,
                dependencies,
                profile.minecraft_version,
            )
        raise CatalogError(
            f"Für {project_id} wurde keine stabile, serverfähige {kind}-Version für Minecraft "
            f"{profile.minecraft_version} und Loader {', '.join(loaders)} gefunden."
        )


class CurseForgeCatalog:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @staticmethod
    def _loader_id(profile: ServerProfile, kind: str) -> int | None:
        if kind == "plugin":
            return None
        loaders = _allowed_profile_loader(profile, kind)
        return CURSEFORGE_LOADERS[loaders[0]]

    def search(self, query: str, kind: str, profile: ServerProfile, limit: int) -> list[CatalogProject]:
        loader_id = self._loader_id(profile, kind)
        params: dict[str, Any] = {
            "gameId": CURSEFORGE_GAME_ID,
            "classId": CURSEFORGE_MOD_CLASS if kind == "mod" else CURSEFORGE_PLUGIN_CLASS,
            "gameVersion": profile.minecraft_version,
            "searchFilter": query,
            "sortField": 2,
            "sortOrder": "desc",
            "pageSize": limit,
            "index": 0,
        }
        if loader_id is not None:
            params["modLoaderType"] = loader_id
        url = _query_url(f"{CURSEFORGE_API}/mods/search", "", params)
        data = _request_json(url, "api.curseforge.com", api_key=self.api_key)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise CatalogError("CurseForge lieferte ein unerwartetes Suchergebnis.")
        projects: list[CatalogProject] = []
        expected_class = CURSEFORGE_MOD_CLASS if kind == "mod" else CURSEFORGE_PLUGIN_CLASS
        for hit in data["data"]:
            if not isinstance(hit, dict) or hit.get("classId") != expected_class:
                continue
            project_id = str(hit.get("id") or "")
            slug = str(hit.get("slug") or project_id)
            if not project_id:
                continue
            links = hit.get("links") if isinstance(hit.get("links"), dict) else {}
            projects.append(
                CatalogProject(
                    "curseforge",
                    kind,
                    project_id,
                    slug,
                    str(hit.get("name") or slug),
                    str(hit.get("summary") or ""),
                    int(hit.get("downloadCount") or 0),
                    str(links.get("websiteUrl") or f"https://www.curseforge.com/minecraft/{'mc-mods' if kind == 'mod' else 'bukkit-plugins'}/{slug}"),
                )
            )
        return projects

    def prepare_install(self, project_id: str, kind: str, profile: ServerProfile) -> InstallPlan:
        if not re.fullmatch(r"[0-9]{1,20}", project_id):
            raise CatalogError("CurseForge-Projekt-IDs müssen numerisch sein.")
        loader_id = self._loader_id(profile, kind)
        mod_id = int(project_id)
        project_result = _request_json(f"{CURSEFORGE_API}/mods/{mod_id}", "api.curseforge.com", api_key=self.api_key)
        project = project_result.get("data") if isinstance(project_result, dict) else None
        expected_class = CURSEFORGE_MOD_CLASS if kind == "mod" else CURSEFORGE_PLUGIN_CLASS
        if not isinstance(project, dict) or project.get("classId") != expected_class:
            raise CatalogError(f"Das angegebene CurseForge-Projekt ist kein {kind}.")
        params: dict[str, Any] = {"gameVersion": profile.minecraft_version, "pageSize": 50, "index": 0}
        if loader_id is not None:
            params["modLoaderType"] = loader_id
        files_url = _query_url(f"{CURSEFORGE_API}/mods/{mod_id}/files", "", params)
        file_result = _request_json(files_url, "api.curseforge.com", api_key=self.api_key)
        files = file_result.get("data") if isinstance(file_result, dict) else None
        if not isinstance(files, list):
            raise CatalogError("CurseForge lieferte eine ungültige Dateiliste.")
        stable_files = sorted(
            (
                item for item in files
                if isinstance(item, dict)
                and item.get("releaseType") == 1
                and item.get("isAvailable", True)
                and isinstance(item.get("gameVersions"), list)
                and profile.minecraft_version in item["gameVersions"]
                and str(item.get("fileName", "")).lower().endswith(".jar")
            ),
            key=lambda item: str(item.get("fileDate") or ""),
            reverse=True,
        )
        if not stable_files:
            raise CatalogError(
                f"Für CurseForge-Projekt {project_id} wurde keine stabile JAR-Datei für Minecraft "
                f"{profile.minecraft_version} und den gewählten Loader gefunden."
            )
        selected = stable_files[0]
        filename = _safe_filename(selected.get("fileName"))
        download_url = selected.get("downloadUrl")
        if not download_url:
            file_id = selected.get("id")
            if not file_id:
                raise CatalogError("CurseForge lieferte weder Download-URL noch Datei-ID.")
            resolved_url = _request_json(
                f"{CURSEFORGE_API}/mods/{mod_id}/files/{int(file_id)}/download-url",
                "api.curseforge.com",
                api_key=self.api_key,
            )
            download_url = resolved_url.get("data") if isinstance(resolved_url, dict) else resolved_url
        checksum, algorithm = _checksum(selected.get("hashes"), source="curseforge")
        dependencies_data = selected.get("dependencies")
        if not isinstance(dependencies_data, list):
            dependencies_data = []
        dependencies = tuple(
            str(item.get("modId"))
            for item in dependencies_data
            if isinstance(item, dict)
            and item.get("relationType") == 3
            and item.get("modId") is not None
        )
        slug = str(project.get("slug") or project_id)
        project_links = project.get("links") if isinstance(project.get("links"), dict) else {}
        project_url = str(project_links.get("websiteUrl") or f"https://www.curseforge.com/minecraft/{'mc-mods' if kind == 'mod' else 'bukkit-plugins'}/{slug}")
        return InstallPlan(
            "curseforge",
            kind,
            str(project.get("id") or project_id),
            slug,
            str(project.get("name") or slug),
            project_url,
            filename,
            str(selected.get("displayName") or selected.get("fileName") or selected.get("id") or "release"),
            _validate_download_url("curseforge", download_url),
            checksum,
            algorithm,
            dependencies,
            profile.minecraft_version,
        )


class ContentCatalog:
    """Search Modrinth/CurseForge and safely install compatible JAR files."""

    def __init__(self, *, curseforge_api_key: str | None = None) -> None:
        self.modrinth = ModrinthCatalog()
        self.curseforge_api_key = curseforge_api_key if curseforge_api_key is not None else os.environ.get("CURSEFORGE_API_KEY")

    def _provider(self, source: str) -> Any:
        if source == "modrinth":
            return self.modrinth
        if source == "curseforge":
            if not self.curseforge_api_key:
                raise CatalogError(
                    "CurseForge benötigt einen API-Schlüssel. Setze CURSEFORGE_API_KEY in der Shell; "
                    "der Schlüssel wird nicht in der Serverkonfiguration gespeichert."
                )
            return CurseForgeCatalog(self.curseforge_api_key)
        raise CatalogError("Quelle muss 'modrinth' oder 'curseforge' sein.")

    def search(self, source: str, kind: str, query: str, profile: ServerProfile, *, limit: int = 10) -> list[CatalogProject]:
        if not query.strip():
            raise CatalogError("Gib einen Suchbegriff für den Katalog an.")
        if not 1 <= limit <= 50:
            raise CatalogError("Die Ergebniszahl muss zwischen 1 und 50 liegen.")
        return self._provider(source).search(query.strip(), kind, profile, limit)

    def prepare_install(self, source: str, kind: str, project_id: str, profile: ServerProfile) -> InstallPlan:
        return self._provider(source).prepare_install(project_id.strip(), kind, profile)

    def install(self, plan: InstallPlan, profile: ServerProfile) -> Path:
        if plan.minecraft_version != profile.minecraft_version:
            raise CatalogError("Die Installationsauswahl passt nicht mehr zur Minecraft-Version dieses Profils.")
        _allowed_profile_loader(profile, plan.kind)
        if plan.source not in {"modrinth", "curseforge"}:
            raise CatalogError("Unbekannte Katalogquelle in der Installationsauswahl.")
        filename = _safe_filename(plan.file_name)
        download_url = _validate_download_url(plan.source, plan.download_url)
        supported_algorithms = {"sha512", "sha1"} if plan.source == "modrinth" else {"sha1", "md5"}
        if plan.algorithm not in supported_algorithms:
            raise CatalogError("Die Installationsauswahl enthält einen nicht unterstützten Prüfsummenalgorithmus.")
        digest, algorithm = _checksum(
            {plan.algorithm: plan.checksum} if plan.source == "modrinth" else [
                {"algo": 1 if plan.algorithm == "sha1" else 2, "value": plan.checksum}
            ],
            source=plan.source,
        )
        root = Path(profile.server_dir).resolve()
        folder_name = "mods" if plan.kind == "mod" else "plugins"
        target_dir = root / folder_name
        if target_dir.is_symlink():
            raise CatalogError(f"Der Zielordner {folder_name} darf kein symbolischer Link sein.")
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target_dir.resolve().parent != root:
            raise CatalogError(f"Der Zielordner {folder_name} liegt nicht sicher innerhalb des Serverprofils.")
        target = target_dir / filename
        if target.exists() or target.is_symlink():
            raise CatalogError(f"Die Datei existiert bereits und wird nicht überschrieben: {target}")
        config_dir = root / "config"
        if config_dir.is_symlink():
            raise CatalogError("Der Konfigurationsordner des Profils darf kein symbolischer Link sein.")
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if config_dir.resolve().parent != root:
            raise CatalogError("Der Konfigurationsordner des Profils ist kein sicherer lokaler Ordner.")
        manifest_path = config_dir / "catalog-installed.json"
        if manifest_path.is_symlink():
            raise CatalogError("Die Katalog-Installationsliste darf kein symbolischer Link sein.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"schema_version": 1, "files": []}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Die Katalog-Installationsliste ist beschädigt: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
            raise CatalogError("Die Katalog-Installationsliste hat ein unbekanntes Format.")
        if any(item.get("path") == f"{folder_name}/{filename}" for item in manifest["files"] if isinstance(item, dict)):
            raise CatalogError(f"{folder_name}/{filename} ist bereits in der Katalog-Installationsliste eingetragen.")
        try:
            download_file(download_url, target, checksum=digest, algorithm=algorithm, timeout=180.0)
            if not zipfile.is_zipfile(target):
                raise CatalogError("Die heruntergeladene Datei ist kein gültiges JAR/ZIP-Archiv.")
            record = {
                "source": plan.source,
                "kind": plan.kind,
                "project_id": plan.project_id,
                "project_slug": plan.project_slug,
                "project_title": plan.project_title,
                "project_url": plan.project_url,
                "file_name": filename,
                "file_version": plan.file_version,
                "path": f"{folder_name}/{filename}",
                "minecraft_version": profile.minecraft_version,
                "loader": profile.loader_family if profile.server_type == "arclight" else profile.server_type,
                "checksum": digest,
                "algorithm": algorithm,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "required_dependencies": list(plan.required_dependencies),
            }
            manifest["files"].append(record)
            self._write_manifest(manifest_path, manifest)
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return target

    @staticmethod
    def _write_manifest(path: Path, data: dict[str, Any]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".catalog-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def installed(profile: ServerProfile) -> list[dict[str, Any]]:
        manifest_path = Path(profile.server_dir) / "config" / "catalog-installed.json"
        if not manifest_path.exists():
            return []
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Die Katalog-Installationsliste kann nicht gelesen werden: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("files"), list):
            raise CatalogError("Die Katalog-Installationsliste hat ein unbekanntes Format.")
        return [item for item in data["files"] if isinstance(item, dict)]
