from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import platform
from pathlib import Path
import tempfile
from typing import Any

from .models import PortSettings, ServerProfile

SCHEMA_VERSION = 1


class ConfigError(RuntimeError):
    """Raised when the local manager configuration cannot be read or written."""


def config_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "MinecraftServerCreator"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "MinecraftServerCreator"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mcsc"


def data_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "MinecraftServerCreator"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "MinecraftServerCreator"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mcsc"


@dataclass
class AppConfig:
    servers_root: str = ""
    port_defaults: PortSettings = field(default_factory=PortSettings)
    profiles: list[ServerProfile] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def defaults(cls) -> "AppConfig":
        configured_root = os.environ.get("MCSC_SERVERS_DIR")
        root = Path(configured_root).expanduser() if configured_root else Path.home() / "MinecraftServers"
        return cls(servers_root=str(root))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        version = int(data.get("schema_version", 1))
        if version != SCHEMA_VERSION:
            raise ConfigError(
                f"Konfigurationsversion {version} wird nicht unterstützt (erwartet {SCHEMA_VERSION})."
            )
        try:
            profiles = [ServerProfile.from_dict(item) for item in data.get("profiles", [])]
            return cls(
                servers_root=str(data.get("servers_root") or AppConfig.defaults().servers_root),
                port_defaults=PortSettings.from_dict(data.get("port_defaults")),
                profiles=profiles,
                schema_version=version,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Die Konfigurationsdatei ist ungültig: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "servers_root": self.servers_root,
            "port_defaults": self.port_defaults.to_dict(),
            "profiles": [profile.to_dict() for profile in self.profiles],
        }

    def find_profile(self, selector: str) -> ServerProfile:
        matches = [
            profile
            for profile in self.profiles
            if selector.lower() in {profile.id.lower(), profile.slug.lower(), profile.name.lower()}
        ]
        if not matches:
            raise ConfigError(f"Kein Serverprofil mit dem Namen oder der Kennung '{selector}' gefunden.")
        if len(matches) > 1:
            raise ConfigError(f"'{selector}' passt auf mehrere Serverprofile. Bitte den Slug verwenden.")
        return matches[0]


class ConfigStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else config_directory() / "config.json"

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig.defaults()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Konfiguration konnte nicht gelesen werden ({self.path}): {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Die Konfigurationsdatei muss ein JSON-Objekt enthalten.")
        return AppConfig.from_dict(raw)

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, prefix="config-", suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except OSError as exc:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise ConfigError(f"Konfiguration konnte nicht gespeichert werden ({self.path}): {exc}") from exc
