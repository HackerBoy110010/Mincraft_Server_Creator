from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = "MinecraftServerCreator/0.1.0 (https://github.com/HackerBoy110010/Mincraft_Server_Creator)"
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


class MinecraftDownloadError(RuntimeError):
    pass


def fetch_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    if urlparse(url).scheme != "https":
        raise MinecraftDownloadError("Nur HTTPS-Downloadquellen werden akzeptiert.")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MinecraftDownloadError(f"Metadaten konnten nicht geladen werden: {exc}") from exc
    if not isinstance(data, dict):
        raise MinecraftDownloadError("Der Minecraft-Metadatenserver lieferte ein unerwartetes Format.")
    return data


def download_file(
    url: str,
    destination: str | Path,
    *,
    checksum: str | None = None,
    algorithm: str = "sha1",
    timeout: float = 60.0,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download a file atomically and verify its checksum when one is supplied."""
    if urlparse(url).scheme != "https":
        raise MinecraftDownloadError("Nur HTTPS-Downloadquellen werden akzeptiert.")
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    hasher = hashlib.new(algorithm) if checksum else None
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with tempfile.NamedTemporaryFile("wb", dir=dest.parent, prefix=f".{dest.name}.", suffix=".part", delete=False) as out:
            temp_path = Path(out.name)
            with urlopen(request, timeout=timeout) as response:
                total_bytes: int | None = None
                headers = getattr(response, "headers", None)
                raw_length = headers.get("Content-Length") if headers is not None else None
                if raw_length is not None:
                    try:
                        parsed_length = int(raw_length)
                        if parsed_length >= 0:
                            total_bytes = parsed_length
                    except (TypeError, ValueError):
                        pass
                downloaded_bytes = 0
                if progress:
                    progress(downloaded_bytes, total_bytes)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded_bytes += len(chunk)
                    if hasher:
                        hasher.update(chunk)
                    if progress:
                        progress(downloaded_bytes, total_bytes)
            out.flush()
            os.fsync(out.fileno())
        if hasher and hasher.hexdigest().lower() != str(checksum).lower():
            raise MinecraftDownloadError(
                f"Prüfsumme stimmt nicht überein (erwartet {checksum}, erhalten {hasher.hexdigest()})."
            )
        os.replace(temp_path, dest)
        return dest
    except MinecraftDownloadError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise MinecraftDownloadError(f"Download fehlgeschlagen: {exc}") from exc
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


class MinecraftMetadataClient:
    def __init__(self, manifest_url: str = MANIFEST_URL) -> None:
        self.manifest_url = manifest_url
        self._manifest: dict[str, Any] | None = None

    def manifest(self, refresh: bool = False) -> dict[str, Any]:
        if self._manifest is None or refresh:
            self._manifest = fetch_json(self.manifest_url)
            if not isinstance(self._manifest.get("versions"), list):
                raise MinecraftDownloadError("Die Minecraft-Versionsliste ist unvollständig.")
        return self._manifest

    def latest_release(self) -> str:
        latest = self.manifest().get("latest", {}).get("release")
        if not latest:
            raise MinecraftDownloadError("Die neueste stabile Minecraft-Version wurde nicht gefunden.")
        return str(latest)

    def known_versions(self, *, include_snapshots: bool = False) -> list[str]:
        versions = self.manifest().get("versions", [])
        return [
            str(item["id"])
            for item in versions
            if item.get("id") and (include_snapshots or item.get("type") == "release")
        ]

    def resolve_version(self, requested: str | None) -> str:
        if not requested or requested.strip().lower() == "latest":
            return self.latest_release()
        version = requested.strip()
        if version not in set(self.known_versions(include_snapshots=True)):
            raise MinecraftDownloadError(f"Minecraft-Version '{version}' wurde in den offiziellen Metadaten nicht gefunden.")
        return version

    def version_info(self, version: str) -> dict[str, Any]:
        for item in self.manifest().get("versions", []):
            if item.get("id") == version:
                url = item.get("url")
                if not url:
                    break
                return fetch_json(str(url))
        raise MinecraftDownloadError(f"Metadaten für Minecraft {version} wurden nicht gefunden.")

    def install_vanilla(self, version: str, server_dir: str | Path) -> tuple[dict[str, Any], Path]:
        info = self.version_info(version)
        java_major = info.get("javaVersion", {}).get("majorVersion")
        if not java_major:
            raise MinecraftDownloadError(f"Die benötigte Java-Version für Minecraft {version} fehlt in den Metadaten.")
        server_asset = info.get("downloads", {}).get("server")
        if not server_asset or not server_asset.get("url"):
            raise MinecraftDownloadError(f"Für Minecraft {version} ist kein Vanilla-Serverdownload verfügbar.")
        server_path = Path(server_dir) / "server.jar"
        download_file(
            str(server_asset["url"]),
            server_path,
            checksum=server_asset.get("sha1"),
            algorithm="sha1",
        )
        version_path = Path(server_dir) / "minecraft-version.json"
        version_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return info, server_path
