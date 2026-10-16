from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from typing import Any

from .minecraft import MinecraftDownloadError, USER_AGENT, download_file

FABRIC_META_URL = "https://meta.fabricmc.net/v2/versions"


class LoaderInstallError(MinecraftDownloadError):
    """Raised when a supported loader's official metadata or installer is invalid."""


def _fetch_json(url: str) -> Any:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "meta.fabricmc.net":
        raise LoaderInstallError("Fabric-Metadaten dürfen nur von meta.fabricmc.net über HTTPS geladen werden.")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=30.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LoaderInstallError(f"Fabric-Metadaten konnten nicht geladen werden: {exc}") from exc


def _version_key(version: str) -> tuple[int, int, int]:
    core = version.split("+", 1)[0].split("-", 1)[0]
    pieces = core.split(".")
    try:
        values = [int(piece) for piece in pieces[:3]]
    except ValueError:
        return (0, 0, 0)
    values.extend([0] * (3 - len(values)))
    return values[0], values[1], values[2]


def _select_stable_version(records: Any, *, nested_key: str | None, minimum: tuple[int, int, int], label: str) -> str:
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


class FabricInstaller:
    """Install Fabric's official unattended server launcher jar for one Minecraft version."""

    def install(self, minecraft_version: str, version_info: dict[str, Any], server_dir: str | Path) -> Path:
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
        (Path(server_dir) / "minecraft-version.json").write_text(
            json.dumps(version_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (Path(server_dir) / "fabric-loader.json").write_text(
            json.dumps(
                {
                    "minecraft_version": minecraft_version,
                    "loader_version": loader_version,
                    "installer_version": installer_version,
                    "source": url,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return server_path
