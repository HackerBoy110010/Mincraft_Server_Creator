from __future__ import annotations

import ipaddress
import socket
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import PortSettings, validate_port


class NetworkError(RuntimeError):
    pass


def port_available(port: int, protocol: str, host: str = "0.0.0.0") -> bool:
    port = int(validate_port(port, "Port"))
    kind = socket.SOCK_STREAM if protocol.lower() == "tcp" else socket.SOCK_DGRAM if protocol.lower() == "udp" else None
    if kind is None:
        raise ValueError("Protokoll muss TCP oder UDP sein.")
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def get_lan_ip() -> str | None:
    """Discover the preferred local IPv4 address without sending application data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        address = sock.getsockname()[0]
        return address if not address.startswith("127.") else None
    except OSError:
        return None
    finally:
        sock.close()


def get_public_ip(timeout: float = 4.0) -> str | None:
    """Ask a public IP echo service; returns None when offline or unavailable."""
    request = Request("https://api.ipify.org", headers={"User-Agent": "mcsc/0.1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(128).decode("ascii", errors="strict").strip()
        return str(ipaddress.ip_address(raw))
    except (OSError, URLError, UnicodeError, ValueError):
        return None


def describe_endpoints(ports: PortSettings, public_ip: str | None, lan_ip: str | None) -> list[str]:
    lines: list[str] = []

    def endpoint_host(address: str) -> str:
        return f"[{address}]" if ":" in address else address

    if public_ip:
        public_host = endpoint_host(public_ip)
        lines.append(f"Öffentlich konfiguriert: {public_host}:{ports.java_tcp} (Java/TCP)")
        if ports.bedrock_udp:
            lines.append(f"Öffentlich konfiguriert: {public_host}:{ports.bedrock_udp} (UDP; benötigt Geyser)")
        if ports.voice_udp:
            lines.append(f"Öffentlich konfiguriert: {public_host}:{ports.voice_udp} (UDP; benötigt Simple Voice Chat)")
    if lan_ip:
        local_host = endpoint_host(lan_ip)
        lines.append(f"Im Heimnetz: {local_host}:{ports.java_tcp} (Java/TCP)")
        if ports.bedrock_udp:
            lines.append(f"Im Heimnetz: {local_host}:{ports.bedrock_udp} (Bedrock/Geyser/UDP)")
        if ports.voice_udp:
            lines.append(f"Im Heimnetz: {local_host}:{ports.voice_udp} (Voicechat/UDP)")
    if not lines:
        lines.append("Keine IP-Adresse konnte ermittelt werden.")
    return lines
