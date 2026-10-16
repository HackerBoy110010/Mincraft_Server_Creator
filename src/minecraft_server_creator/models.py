from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any

SERVER_TYPES = ("vanilla", "fabric", "forge", "neoforge", "arclight")
IMPLEMENTED_SERVER_TYPES = frozenset({"vanilla", "fabric"})


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
    bedrock_udp: int | None = 19132
    voice_udp: int | None = 24454

    def __post_init__(self) -> None:
        validate_port(self.java_tcp, "Java-TCP-Port")
        validate_port(self.bedrock_udp, "Bedrock-UDP-Port", optional=True)
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
            "ports": self.ports.to_dict(),
            "auto_start_on_boot": self.auto_start_on_boot,
            "eula_accepted": self.eula_accepted,
            "created_at": self.created_at,
        }
