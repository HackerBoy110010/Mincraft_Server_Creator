from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .minecraft import MinecraftDownloadError, USER_AGENT, download_file

FABRIC_META_URL = "https://meta.fabricmc.net/v2/versions"
FORGE_PROMOTIONS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN_BASE = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_METADATA_URL = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
NEOFORGE_MAVEN_BASE = "https://maven.neoforged.net/releases/net/neoforged/neoforge"


class LoaderInstallError(MinecraftDownloadError):
    """Raised when official loader metadata or an installer is invalid."""


@dataclass(frozen=True)
class LoaderInstallResult:
    """Launch metadata returned by a server loader installer."""

    loader_version: str
    launch_mode: str
    launch_target: str


def _fetch_json(url: str) -> Any:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "meta.fabricmc.net":
        raise LoaderInstallError("Fabric-Metadaten dürfen nur von meta.fabricmc.net über HTTPS geladen werden.")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=30.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LoaderInstallError(f"Fabric-Metadaten konnten nicht geladen werden: {exc}") from exc


def _fetch_official_text(url: str, host: str, *, accept: str = "text/plain, application/xml") -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.port not in (None, 443):
        raise LoaderInstallError(f"Installationsmetadaten dürfen nur über HTTPS von {host} geladen werden.")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urlopen(request, timeout=30.0) as response:
            content = response.read(5_000_001)
        if len(content) > 5_000_000:
            raise LoaderInstallError(f"Metadaten von {host} überschreiten die zulässige Größe.")
        return content.decode("utf-8")
    except LoaderInstallError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise LoaderInstallError(f"Metadaten konnten nicht von {host} geladen werden: {exc}") from exc


def _fetch_official_json(url: str, host: str) -> Any:
    text = _fetch_official_text(url, host, accept="application/json")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LoaderInstallError(f"Ungültige JSON-Metadaten von {host}: {exc}") from exc


def _version_key(version: str) -> tuple[int, ...]:
    core = version.split("+", 1)[0].split("-", 1)[0]
    pieces = core.split(".")
    try:
        return tuple(int(piece) for piece in pieces)
    except ValueError:
        return ()


def _select_stable_version(
    records: Any,
    *,
    nested_key: str | None,
    minimum: tuple[int, int, int],
    label: str,
) -> str:
    if not isinstance(records, list):
        raise LoaderInstallError(f"Fabric lieferte keine gültige {label}-Versionsliste.")
    for record in records:
        if not isinstance(record, dict):
            continue
        data = record.get(nested_key, {}) if nested_key else record
        if not isinstance(data, dict) or data.get("stable") is not True:
            continue
        version = data.get("version")
        if isinstance(version, str) and _version_key(version) >= minimum:
            return version
    minimum_text = ".".join(map(str, minimum))
    raise LoaderInstallError(f"Keine stabile Fabric-{label}-Version ab {minimum_text} ist verfügbar.")


def _sha1_from_sidecar(url: str, host: str) -> str:
    text = _fetch_official_text(url, host)
    match = re.search(r"\b([0-9a-fA-F]{40})\b", text)
    if not match:
        raise LoaderInstallError(f"Der Installer-Download von {host} hat keine gültige SHA-1-Prüfsumme.")
    return match.group(1).lower()


def _run_server_installer(java_path: str | Path, installer_path: Path, server_dir: Path) -> None:
    try:
        result = subprocess.run(
            [str(java_path), "-jar", str(installer_path), "--installServer"],
            cwd=server_dir,
            text=True,
            capture_output=True,
            timeout=900.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoaderInstallError(f"Der Server-Installer konnte nicht ausgeführt werden: {exc}") from exc
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        detail = "\n".join(output.splitlines()[-20:])[:4000]
        suffix = f"\nInstaller-Ausgabe:\n{detail}" if detail else ""
        raise LoaderInstallError(f"Server-Installer wurde mit Status {result.returncode} beendet.{suffix}")


def _write_metadata(server_dir: Path, filename: str, data: dict[str, Any]) -> None:
    (server_dir / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class FabricInstaller:
    """Install Fabric's official unattended server launcher jar for one Minecraft version."""

    def install(
        self, minecraft_version: str, version_info: dict[str, Any], server_dir: str | Path
    ) -> LoaderInstallResult:
        encoded_game = quote(minecraft_version, safe="")
        loader_records = _fetch_json(f"{FABRIC_META_URL}/loader/{encoded_game}")
        loader_version = _select_stable_version(
            loader_records,
            nested_key="loader",
            minimum=(0, 12, 0),
            label="Loader",
        )
        installer_records = _fetch_json(f"{FABRIC_META_URL}/installer")
        installer_version = _select_stable_version(
            installer_records,
            nested_key=None,
            minimum=(0, 8, 0),
            label="Installer",
        )

        url = (
            f"{FABRIC_META_URL}/loader/{encoded_game}/{quote(loader_version, safe='')}"
            f"/{quote(installer_version, safe='')}/server/jar"
        )
        server_path = Path(server_dir) / "server.jar"
        download_file(url, server_path, timeout=180.0)
        _write_metadata(Path(server_dir), "minecraft-version.json", version_info)
        _write_metadata(
            Path(server_dir),
            "fabric-loader.json",
            {
                "minecraft_version": minecraft_version,
                "loader_version": loader_version,
                "installer_version": installer_version,
                "source": url,
            },
        )
        return LoaderInstallResult(loader_version, "jar", "server.jar")


class ForgeInstaller:
    """Download and run the official Forge server installer for the requested game version."""

    def install(
        self,
        minecraft_version: str,
        version_info: dict[str, Any],
        server_dir: str | Path,
        java_path: str | Path,
    ) -> LoaderInstallResult:
        if not re.fullmatch(r"\d+(?:\.\d+){1,2}", minecraft_version):
            raise LoaderInstallError("Forge kann nur für veröffentlichte numerische Minecraft-Versionen installiert werden.")
        promotions = _fetch_official_json(FORGE_PROMOTIONS_URL, "files.minecraftforge.net")
        if not isinstance(promotions, dict) or not isinstance(promotions.get("promos"), dict):
            raise LoaderInstallError("Forge lieferte keine gültige Promotions-Versionsliste.")
        promos = promotions["promos"]
        loader_version = None
        for key in (f"{minecraft_version}-latest", f"{minecraft_version}-recommended"):
            candidate = promos.get(key)
            if isinstance(candidate, str) and re.fullmatch(r"\d+(?:\.\d+){1,4}", candidate):
                loader_version = candidate
                break
        if loader_version is None:
            raise LoaderInstallError(
                f"Forge stellt keinen stabilen Installer für Minecraft {minecraft_version} bereit."
            )

        coordinate = f"{minecraft_version}-{loader_version}"
        artifact = f"forge-{coordinate}-installer.jar"
        base_url = f"{FORGE_MAVEN_BASE}/{quote(coordinate, safe='')}/{quote(artifact, safe='')}"
        checksum = _sha1_from_sidecar(f"{base_url}.sha1", "maven.minecraftforge.net")
        return self._install(
            minecraft_version,
            loader_version,
            version_info,
            server_dir,
            java_path,
            base_url,
            checksum,
            artifact,
            f"libraries/net/minecraftforge/forge/{coordinate}/unix_args.txt",
        )

    @staticmethod
    def _install(
        minecraft_version: str,
        loader_version: str,
        version_info: dict[str, Any],
        server_dir: str | Path,
        java_path: str | Path,
        installer_url: str,
        checksum: str,
        installer_name: str,
        launch_target: str,
    ) -> LoaderInstallResult:
        root = Path(server_dir)
        installer_path = root / f".{installer_name}"
        parsed = urlparse(installer_url)
        if parsed.scheme != "https" or parsed.hostname != "maven.minecraftforge.net":
            raise LoaderInstallError("Forge-Installer dürfen nur von maven.minecraftforge.net geladen werden.")
        try:
            download_file(installer_url, installer_path, checksum=checksum, algorithm="sha1", timeout=180.0)
            _run_server_installer(java_path, installer_path, root)
        finally:
            installer_path.unlink(missing_ok=True)
        launch_file = root / launch_target
        if not launch_file.is_file() or launch_file.is_symlink():
            raise LoaderInstallError(
                "Der Forge-Installer hat keine unterstützte unix_args.txt erzeugt; diese Version wird nicht gestartet."
            )
        _write_metadata(root, "minecraft-version.json", version_info)
        _write_metadata(
            root,
            "forge-loader.json",
            {
                "minecraft_version": minecraft_version,
                "loader_version": loader_version,
                "installer": installer_name,
                "installer_sha1": checksum,
                "source": installer_url,
                "launch_target": launch_target,
            },
        )
        return LoaderInstallResult(loader_version, "args", launch_target)


class NeoForgeInstaller:
    """Resolve a stable NeoForge build from its official Maven metadata and install it."""

    @staticmethod
    def minecraft_prefix(minecraft_version: str) -> str:
        if not re.fullmatch(r"\d+(?:\.\d+){1,2}", minecraft_version):
            raise LoaderInstallError(
                "NeoForge kann nur für veröffentlichte numerische Minecraft-Versionen installiert werden."
            )
        parts = [int(part) for part in minecraft_version.split(".")]
        if parts[0] == 1:
            game_major = parts[1]
            game_minor = parts[2] if len(parts) > 2 else 0
        else:
            game_major = parts[0]
            game_minor = parts[1]
        return f"{game_major}.{game_minor}."

    def install(
        self,
        minecraft_version: str,
        version_info: dict[str, Any],
        server_dir: str | Path,
        java_path: str | Path,
    ) -> LoaderInstallResult:
        prefix = self.minecraft_prefix(minecraft_version)
        metadata = _fetch_official_text(NEOFORGE_METADATA_URL, "maven.neoforged.net", accept="application/xml")
        try:
            root = ET.fromstring(metadata)
        except ET.ParseError as exc:
            raise LoaderInstallError(f"NeoForge-Maven-Metadaten sind ungültig: {exc}") from exc
        versions = [item.text.strip() for item in root.iter("version") if item.text and item.text.strip()]
        stable = [
            version
            for version in versions
            if version.startswith(prefix) and re.fullmatch(r"\d+(?:\.\d+){1,4}", version)
        ]
        if not stable:
            raise LoaderInstallError(f"NeoForge stellt keinen stabilen Installer für Minecraft {minecraft_version} bereit.")
        loader_version = max(stable, key=_version_key)

        artifact = f"neoforge-{loader_version}-installer.jar"
        base_url = (
            f"{NEOFORGE_MAVEN_BASE}/{quote(loader_version, safe='')}/{quote(artifact, safe='')}"
        )
        checksum = _sha1_from_sidecar(f"{base_url}.sha1", "maven.neoforged.net")
        launch_target = f"libraries/net/neoforged/neoforge/{loader_version}/unix_args.txt"
        root_dir = Path(server_dir)
        installer_path = root_dir / f".{artifact}"
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "maven.neoforged.net":
            raise LoaderInstallError("NeoForge-Installer dürfen nur von maven.neoforged.net geladen werden.")
        try:
            download_file(base_url, installer_path, checksum=checksum, algorithm="sha1", timeout=180.0)
            _run_server_installer(java_path, installer_path, root_dir)
        finally:
            installer_path.unlink(missing_ok=True)
        launch_file = root_dir / launch_target
        if not launch_file.is_file() or launch_file.is_symlink():
            raise LoaderInstallError(
                "Der NeoForge-Installer hat keine unterstützte unix_args.txt erzeugt; diese Version wird nicht gestartet."
            )
        _write_metadata(root_dir, "minecraft-version.json", version_info)
        _write_metadata(
            root_dir,
            "neoforge-loader.json",
            {
                "minecraft_version": minecraft_version,
                "loader_version": loader_version,
                "installer": artifact,
                "installer_sha1": checksum,
                "source": base_url,
                "launch_target": launch_target,
            },
        )
        return LoaderInstallResult(loader_version, "args", launch_target)
