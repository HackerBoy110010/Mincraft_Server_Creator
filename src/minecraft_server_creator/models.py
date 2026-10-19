from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any

SERVER_TYPES = ("vanilla", "fabric", "forge", "neoforge", "arclight")
ARCLIGHT_LOADER_FAMILIES = ("fabric", "forge", "neoforge")
IMPLEMENTED_SERVER_TYPES = frozenset({"vanilla", "fabric", "forge", "neoforge", "arclight"})


class ValidationError(ValueError):
    """Raised when a profile or setting is invalid."""


def validate_port(value: int | None, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{label} muss eine Portnummer zwischen 1 und 65535 sein.")
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} muss eine Portnummer zwischen 1 und 65535 sein.") from exc
    if not 1 <= port <= 65535:
        raise ValidationError(f"{label} muss zwischen 1 und 65535 liegen.")
    return port


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    slug = slug[:48].strip("-")
    return slug or "server"


@dataclass(frozen=True)
class PortSettings:
    java_tcp: int = 25565
    # Keep the historical field name for config compatibility; NetherNet uses this number on TCP and UDP.
    bedrock_udp: int | None = 19132
    voice_udp: int | None = 24454

    def __post_init__(self) -> None:
        validate_port(self.java_tcp, "Java-TCP-Port")
        validate_port(self.bedrock_udp, "Bedrock/Geyser-Port", optional=True)
        validate_port(self.voice_udp, "Voicechat-UDP-Port", optional=True)
        if self.bedrock_udp is not None and self.bedrock_udp == self.voice_udp:
            raise ValidationError("Bedrock/Geyser und Voicechat benötigen unterschiedliche UDP-Ports.")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PortSettings":
        data = data or {}
        return cls(
            java_tcp=data.get("java_tcp", 25565),
            bedrock_udp=data.get("bedrock_udp", 19132),
            voice_udp=data.get("voice_udp", 24454),
        )

    def to_dict(self) -> dict[str, int | None]:
        return {
            "java_tcp": self.java_tcp,
            "bedrock_udp": self.bedrock_udp,
            "voice_udp": self.voice_udp,
        }


@dataclass
class ServerProfile:
    id: str
    name: str
    slug: str
    server_type: str
    minecraft_version: str
    java_major: int
    server_dir: str
    memory_mb: int = 4096
    ports: PortSettings = PortSettings()
    auto_start_on_boot: bool = False
    eula_accepted: bool = False
    created_at: str = ""
    loader_version: str | None = None
    loader_family: str | None = None
    launch_mode: str = "jar"
    launch_target: str = "server.jar"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationError("Der Servername darf nicht leer sein.")
        if self.server_type not in SERVER_TYPES:
            raise ValidationError(f"Unbekannter Servertyp: {self.server_type}")
        if not self.minecraft_version.strip():
            raise ValidationError("Die Minecraft-Version darf nicht leer sein.")
        if self.java_major < 8:
            raise ValidationError("Die benötigte Java-Version ist ungültig.")
        if self.memory_mb < 1024:
            raise ValidationError("Mindestens 1024 MB RAM müssen zugewiesen werden.")
        if self.loader_version is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", self.loader_version
        ):
            raise ValidationError("Die Loader-Version enthält ungültige Zeichen.")
        if self.server_type == "arclight" and self.loader_family not in {"fabric", "forge", "neoforge"}:
            raise ValidationError("Für Arclight muss Fabric, Forge oder NeoForge als Basis gewählt werden.")
        if self.server_type != "arclight" and self.loader_family is not None:
            raise ValidationError("Eine Loader-Familie darf nur für Arclight-Profile gesetzt sein.")
        if self.launch_mode not in {"jar", "args"}:
            raise ValidationError("Das Server-Startverfahren ist ungültig.")
        target = self.launch_target.replace("\\", "/")
        if (
            not target
            or target.startswith("/")
            or any(part in {"", ".", ".."} for part in target.split("/"))
            or "\n" in target
            or "\r" in target
        ):
            raise ValidationError("Das Startziel muss ein sicherer relativer Pfad sein.")
        if self.launch_mode == "jar" and target != "server.jar":
            raise ValidationError("JAR-Profile müssen server.jar als Startziel verwenden.")
        if self.launch_mode == "args" and self.server_type not in {"forge", "neoforge"}:
            raise ValidationError("Startdateien sind nur für unterstützte Forge-/NeoForge-Profile zulässig.")
        if self.launch_mode == "args" and not self.loader_version:
            raise ValidationError("Für args-basierte Starts muss eine Loader-Version gespeichert sein.")
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerProfile":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            slug=str(data["slug"]),
            server_type=str(data.get("server_type", "vanilla")),
            minecraft_version=str(data["minecraft_version"]),
            java_major=int(data["java_major"]),
            server_dir=str(data["server_dir"]),
            memory_mb=int(data.get("memory_mb", 4096)),
            loader_version=(str(data["loader_version"]) if data.get("loader_version") is not None else None),
            loader_family=(str(data["loader_family"]) if data.get("loader_family") is not None else None),
            launch_mode=str(data.get("launch_mode", "jar")),
            launch_target=str(data.get("launch_target", "server.jar")),
            ports=PortSettings.from_dict(data.get("ports")),
            auto_start_on_boot=bool(data.get("auto_start_on_boot", False)),
            eula_accepted=bool(data.get("eula_accepted", False)),
            created_at=str(data.get("created_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "server_type": self.server_type,
            "minecraft_version": self.minecraft_version,
            "java_major": self.java_major,
            "server_dir": self.server_dir,
            "memory_mb": self.memory_mb,
            "loader_version": self.loader_version,
            "loader_family": self.loader_family,
            "launch_mode": self.launch_mode,
            "launch_target": self.launch_target,
            "ports": self.ports.to_dict(),
            "auto_start_on_boot": self.auto_start_on_boot,
            "eula_accepted": self.eula_accepted,
            "created_at": self.created_at,
        }
