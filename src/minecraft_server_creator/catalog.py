from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import zipfile
import zlib

from .minecraft import USER_AGENT, download_file
from .models import ServerProfile
from .properties import read_properties

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
CURSEFORGE_GAME_ID = 432
CURSEFORGE_MOD_CLASS = 6
CURSEFORGE_PLUGIN_CLASS = 5
CURSEFORGE_LOADERS = {"forge": 1, "fabric": 4, "neoforge": 6}
CURSEFORGE_PROJECT_PATHS = {"mod": "mc-mods", "plugin": "bukkit-plugins", "datapack": "data-packs"}


class CatalogError(RuntimeError):
    """Raised when a catalogue result cannot be safely searched or installed."""


class NoCompatibleVersionError(CatalogError):
    """Raised when a project has no version for the selected game and loader."""


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
    release_channel: str = "release"


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
    if kind not in {"mod", "plugin", "datapack"}:
        raise CatalogError("Der Katalog unterstützt nur Mods, Plugins und Datapacks.")
    if kind == "datapack":
        return ("datapack",)
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


def _safe_filename(filename: Any, *, extension: str = ".jar") -> str:
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
        or not filename.lower().endswith(extension.lower())
    ):
        raise CatalogError(f"Es werden nur sichere, portable Dateinamen mit Endung {extension} ohne Pfadbestandteile akzeptiert.")
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


def _validate_datapack_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                metadata = archive.getinfo("pack.mcmeta")
            except KeyError as exc:
                raise CatalogError("Das Datapack-ZIP braucht pack.mcmeta direkt im Stammverzeichnis.") from exc
            if metadata.file_size > 1_000_000:
                raise CatalogError("pack.mcmeta überschreitet die zulässige Größe.")
            with archive.open(metadata) as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise CatalogError("pack.mcmeta überschreitet die zulässige Größe.")
            document = json.loads(raw.decode("utf-8"))
    except CatalogError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        RuntimeError,
        EOFError,
        zlib.error,
        NotImplementedError,
    ) as exc:
        raise CatalogError(f"Das Datapack-ZIP oder pack.mcmeta ist ungültig: {exc}") from exc
    pack = document.get("pack") if isinstance(document, dict) else None
    if not isinstance(pack, dict):
        raise CatalogError("pack.mcmeta enthält keinen gültigen pack-Abschnitt.")
    pack_format = pack.get("pack_format")
    valid_format = isinstance(pack_format, int) and not isinstance(pack_format, bool) and pack_format > 0
    for field in ("min_format", "max_format"):
        value = pack.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            valid_format = True
        elif isinstance(value, list) and value and all(
            isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in value
        ) and any(part > 0 for part in value):
            valid_format = True
    if not valid_format:
        raise CatalogError("pack.mcmeta enthält keinen erkennbaren Datapack-Format-Eintrag.")


def _datapack_world_name(profile: ServerProfile) -> str:
    properties = read_properties(Path(profile.server_dir) / "server.properties")
    world_name = properties.get("level-name", "world").strip()
    device = world_name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))
    }
    if (
        not world_name
        or len(world_name) > 120
        or world_name in {".", ".."}
        or Path(world_name).name != world_name
        or "/" in world_name
        or "\\" in world_name
        or any(char in world_name for char in '<>:"|?*')
        or any(ord(char) < 32 or ord(char) == 127 for char in world_name)
        or world_name.endswith((".", " "))
        or device
    ):
        raise CatalogError("level-name muss ein sicherer einzelner Weltordnername ohne Pfadbestandteile sein.")
    return world_name


def _datapack_target(profile: ServerProfile) -> tuple[str, Path]:
    root = Path(profile.server_dir).resolve()
    world_name = _datapack_world_name(profile)
    world_dir = root / world_name
    if world_dir.is_symlink():
        raise CatalogError("Der Weltordner darf kein symbolischer Link sein.")
    world_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if world_dir.resolve().parent != root:
        raise CatalogError("Der Weltordner liegt nicht sicher innerhalb des Serverprofils.")
    datapacks_dir = world_dir / "datapacks"
    if datapacks_dir.is_symlink():
        raise CatalogError("Der Datapack-Ordner darf kein symbolischer Link sein.")
    datapacks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if datapacks_dir.resolve().parent != world_dir:
        raise CatalogError("Der Datapack-Ordner liegt nicht sicher innerhalb der Welt.")
    return f"{world_name}/datapacks", datapacks_dir


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
            if kind != "datapack":
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

    def prepare_install(
        self,
        project_id: str,
        kind: str,
        profile: ServerProfile,
        *,
        include_beta: bool = False,
        version_id: str | None = None,
    ) -> InstallPlan:
        loaders = _allowed_profile_loader(profile, kind)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
            raise CatalogError("Die Modrinth-Projekt-ID bzw. der Slug ist ungültig.")
        if version_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", version_id):
            raise CatalogError("Die Modrinth-Versions-ID ist ungültig.")
        project = _request_json(f"{MODRINTH_API}/project/{project_id}", "api.modrinth.com")
        if not isinstance(project, dict):
            raise CatalogError(f"Das angegebene Modrinth-Projekt ist kein {kind}.")
        project_type = project.get("project_type")
        # Modrinth may report plugins (and some datapack projects) through the
        # primary "mod" project type. The selected version's loader is authoritative.
        allowed_project_types = {"mod", "plugin"} if kind == "plugin" else {"mod", "datapack"}
        if project_type not in allowed_project_types:
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
        accepted_channels = {"release", "beta"} if include_beta else {"release"}
        for version in versions:
            if not isinstance(version, dict) or version.get("version_type") not in accepted_channels:
                continue
            if version_id is not None and version.get("id") != version_id:
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
            if kind != "datapack" and version.get("environment") not in SERVER_ENVIRONMENTS:
                continue
            files = version.get("files")
            if not isinstance(files, list):
                continue
            extension = ".zip" if kind == "datapack" else ".jar"
            candidates = [
                item for item in files
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(extension)
            ]
            if not candidates:
                continue
            selected = next((item for item in candidates if item.get("primary") is True), candidates[0])
            filename = _safe_filename(selected.get("filename"), extension=extension)
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
                str(version.get("version_type")),
            )
        channel_message = "stabil oder Beta" if include_beta else "stabil"
        version_message = f" mit Versions-ID {version_id}" if version_id is not None else ""
        raise NoCompatibleVersionError(
            f"Für {project_id}{version_message} wurde keine passende serverfähige {kind}-Version "
            f"(Kanal: {channel_message}) für Minecraft {profile.minecraft_version} "
            f"und Loader {', '.join(loaders)} gefunden."
        )


class CurseForgeCatalog:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @staticmethod
    def _loader_id(profile: ServerProfile, kind: str) -> int | None:
        if kind in {"plugin", "datapack"}:
            _allowed_profile_loader(profile, kind)
            return None
        loaders = _allowed_profile_loader(profile, kind)
        return CURSEFORGE_LOADERS[loaders[0]]

    def _datapack_scope(self) -> tuple[int, int | None]:
        result = _request_json(
            _query_url(f"{CURSEFORGE_API}/categories", "", {"gameId": CURSEFORGE_GAME_ID}),
            "api.curseforge.com",
            api_key=self.api_key,
        )
        categories = result.get("data") if isinstance(result, dict) else None
        if not isinstance(categories, list):
            raise CatalogError("CurseForge lieferte keine gültige Kategorienliste für Datapacks.")

        def is_datapack_category(category: dict[str, Any]) -> bool:
            names = (category.get("name"), category.get("slug"))
            normalized = {
                re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
                for value in names
                if isinstance(value, str)
            }
            return bool(normalized.intersection({"data-pack", "data-packs", "datapack", "datapacks"}))

        matches = [item for item in categories if isinstance(item, dict) and is_datapack_category(item)]
        if not matches:
            raise CatalogError("CurseForge meldet keine Data Packs-Kategorie; die Suche wurde sicherheitshalber abgebrochen.")
        matches.sort(key=lambda item: not bool(item.get("isClass")))
        category = matches[0]
        category_id = category.get("id")
        if not isinstance(category_id, int) or category_id <= 0:
            raise CatalogError("CurseForge lieferte eine ungültige ID für die Data Packs-Kategorie.")
        if category.get("isClass") is True:
            return category_id, None
        class_id = category.get("classId")
        if not isinstance(class_id, int) or class_id <= 0:
            raise CatalogError("CurseForge lieferte keine übergeordnete Klasse für die Data Packs-Kategorie.")
        return class_id, category_id

    @staticmethod
    def _matches_datapack_category(project: dict[str, Any], class_id: int, category_id: int | None) -> bool:
        if project.get("classId") != class_id:
            return False
        if category_id is None:
            return True
        categories = project.get("categories")
        return isinstance(categories, list) and any(
            isinstance(item, dict) and item.get("id") == category_id for item in categories
        )

    def search(self, query: str, kind: str, profile: ServerProfile, limit: int) -> list[CatalogProject]:
        loader_id = self._loader_id(profile, kind)
        datapack_scope = self._datapack_scope() if kind == "datapack" else None
        expected_class = datapack_scope[0] if datapack_scope else (
            CURSEFORGE_MOD_CLASS if kind == "mod" else CURSEFORGE_PLUGIN_CLASS
        )
        params: dict[str, Any] = {
            "gameId": CURSEFORGE_GAME_ID,
            "classId": expected_class,
            "gameVersion": profile.minecraft_version,
            "searchFilter": query,
            "sortField": 2,
            "sortOrder": "desc",
            "pageSize": limit,
            "index": 0,
        }
        if datapack_scope is not None and datapack_scope[1] is not None:
            params["categoryId"] = datapack_scope[1]
        if loader_id is not None:
            params["modLoaderType"] = loader_id
        url = _query_url(f"{CURSEFORGE_API}/mods/search", "", params)
        data = _request_json(url, "api.curseforge.com", api_key=self.api_key)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise CatalogError("CurseForge lieferte ein unerwartetes Suchergebnis.")
        projects: list[CatalogProject] = []
        for hit in data["data"]:
            if not isinstance(hit, dict) or hit.get("classId") != expected_class:
                continue
            if datapack_scope is not None and not self._matches_datapack_category(hit, *datapack_scope):
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
                    str(links.get("websiteUrl") or f"https://www.curseforge.com/minecraft/{CURSEFORGE_PROJECT_PATHS[kind]}/{slug}"),
                )
            )
        return projects

    def prepare_install(self, project_id: str, kind: str, profile: ServerProfile) -> InstallPlan:
        if not re.fullmatch(r"[0-9]{1,20}", project_id):
            raise CatalogError("CurseForge-Projekt-IDs müssen numerisch sein.")
        loader_id = self._loader_id(profile, kind)
        datapack_scope = self._datapack_scope() if kind == "datapack" else None
        expected_class = datapack_scope[0] if datapack_scope else (
            CURSEFORGE_MOD_CLASS if kind == "mod" else CURSEFORGE_PLUGIN_CLASS
        )
        mod_id = int(project_id)
        project_result = _request_json(f"{CURSEFORGE_API}/mods/{mod_id}", "api.curseforge.com", api_key=self.api_key)
        project = project_result.get("data") if isinstance(project_result, dict) else None
        if not isinstance(project, dict) or project.get("classId") != expected_class:
            raise CatalogError(f"Das angegebene CurseForge-Projekt ist kein {kind}.")
        if datapack_scope is not None and not self._matches_datapack_category(project, *datapack_scope):
            raise CatalogError(f"Das angegebene CurseForge-Projekt gehört nicht zur Data Packs-Kategorie.")
        params: dict[str, Any] = {"gameVersion": profile.minecraft_version, "pageSize": 50, "index": 0}
        if loader_id is not None:
            params["modLoaderType"] = loader_id
        files_url = _query_url(f"{CURSEFORGE_API}/mods/{mod_id}/files", "", params)
        file_result = _request_json(files_url, "api.curseforge.com", api_key=self.api_key)
        files = file_result.get("data") if isinstance(file_result, dict) else None
        if not isinstance(files, list):
            raise CatalogError("CurseForge lieferte eine ungültige Dateiliste.")
        extension = ".zip" if kind == "datapack" else ".jar"
        stable_files = sorted(
            (
                item for item in files
                if isinstance(item, dict)
                and item.get("releaseType") == 1
                and item.get("isAvailable", True)
                and isinstance(item.get("gameVersions"), list)
                and profile.minecraft_version in item["gameVersions"]
                and str(item.get("fileName", "")).lower().endswith(extension)
            ),
            key=lambda item: str(item.get("fileDate") or ""),
            reverse=True,
        )
        if not stable_files:
            raise CatalogError(
                f"Für CurseForge-Projekt {project_id} wurde keine stabile {extension.upper()}-Datei für Minecraft "
                f"{profile.minecraft_version} gefunden."
            )
        selected = stable_files[0]
        filename = _safe_filename(selected.get("fileName"), extension=extension)
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
        project_url = str(
            project_links.get("websiteUrl")
            or f"https://www.curseforge.com/minecraft/{CURSEFORGE_PROJECT_PATHS[kind]}/{slug}"
        )
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
    """Search Modrinth/CurseForge and safely install compatible content files."""

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

    def prepare_install(
        self,
        source: str,
        kind: str,
        project_id: str,
        profile: ServerProfile,
        *,
        include_beta: bool = False,
        version_id: str | None = None,
    ) -> InstallPlan:
        provider = self._provider(source)
        if (include_beta or version_id is not None) and source != "modrinth":
            raise CatalogError("Beta-Opt-in und feste Versions-IDs werden derzeit nur für Modrinth unterstützt.")
        if source == "modrinth":
            return provider.prepare_install(
                project_id.strip(),
                kind,
                profile,
                include_beta=include_beta,
                version_id=version_id,
            )
        return provider.prepare_install(project_id.strip(), kind, profile)

    @staticmethod
    def destination(profile: ServerProfile, kind: str) -> str:
        _allowed_profile_loader(profile, kind)
        if kind == "datapack":
            return f"{_datapack_world_name(profile)}/datapacks"
        return "mods" if kind == "mod" else "plugins"

    def install(
        self,
        plan: InstallPlan,
        profile: ServerProfile,
        *,
        allow_beta: bool = False,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        if plan.minecraft_version != profile.minecraft_version:
            raise CatalogError("Die Installationsauswahl passt nicht mehr zur Minecraft-Version dieses Profils.")
        if plan.release_channel not in {"release", "beta"}:
            raise CatalogError("Die Installationsauswahl enthält einen nicht unterstützten Veröffentlichungskanal.")
        if plan.release_channel == "beta" and not allow_beta:
            raise CatalogError("Beta-Dateien benötigen das ausdrückliche Opt-in allow_beta=True.")
        _allowed_profile_loader(profile, plan.kind)
        if plan.source not in {"modrinth", "curseforge"}:
            raise CatalogError("Unbekannte Katalogquelle in der Installationsauswahl.")
        extension = ".zip" if plan.kind == "datapack" else ".jar"
        filename = _safe_filename(plan.file_name, extension=extension)
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
        if plan.kind == "datapack":
            folder_name, target_dir = _datapack_target(profile)
        else:
            folder_name = "mods" if plan.kind == "mod" else "plugins"
            target_dir = root / folder_name
            if target_dir.is_symlink():
                raise CatalogError(f"Der Zielordner {folder_name} darf kein symbolischer Link sein.")
            target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target_dir.resolve().parent != root:
                raise CatalogError(f"Der Zielordner {folder_name} liegt nicht sicher innerhalb des Serverprofils.")
        relative_path = f"{folder_name}/{filename}"
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
        if any(item.get("path") == relative_path for item in manifest["files"] if isinstance(item, dict)):
            # A world reset or manual deletion can remove the archive while leaving its
            # manifest record behind. The target was verified absent above, so prune
            # stale entries instead of blocking a legitimate reinstall.
            manifest["files"] = [
                item for item in manifest["files"]
                if not isinstance(item, dict) or item.get("path") != relative_path
            ]
            self._write_manifest(manifest_path, manifest)
        try:
            if progress is None:
                download_file(download_url, target, checksum=digest, algorithm=algorithm, timeout=180.0)
            else:
                download_file(
                    download_url,
                    target,
                    checksum=digest,
                    algorithm=algorithm,
                    timeout=180.0,
                    progress=progress,
                )
            if not zipfile.is_zipfile(target):
                raise CatalogError("Die heruntergeladene Datei ist kein gültiges JAR/ZIP-Archiv.")
            if plan.kind == "datapack":
                _validate_datapack_archive(target)
            record = {
                "source": plan.source,
                "kind": plan.kind,
                "project_id": plan.project_id,
                "project_slug": plan.project_slug,
                "project_title": plan.project_title,
                "project_url": plan.project_url,
                "file_name": filename,
                "file_version": plan.file_version,
                "release_channel": plan.release_channel,
                "path": relative_path,
                "minecraft_version": profile.minecraft_version,
                "loader": "datapack" if plan.kind == "datapack" else (
                    profile.loader_family if profile.server_type == "arclight" else profile.server_type
                ),
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
        config_dir = Path(profile.server_dir) / "config"
        manifest_path = config_dir / "catalog-installed.json"
        if config_dir.is_symlink() or manifest_path.is_symlink():
            raise CatalogError("Die Katalog-Installationsliste und ihr Ordner dürfen keine symbolischen Links sein.")
        if not manifest_path.exists():
            return []
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Die Katalog-Installationsliste kann nicht gelesen werden: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("files"), list):
            raise CatalogError("Die Katalog-Installationsliste hat ein unbekanntes Format.")
        return [item for item in data["files"] if isinstance(item, dict)]
