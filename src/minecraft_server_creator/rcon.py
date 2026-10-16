from __future__ import annotations

import ipaddress
import re
import socket
import struct
from dataclasses import dataclass


class RconError(RuntimeError):
    pass


DEFAULT_RCON_PORT = 25575
_PLAYER_NAME = re.compile(r"[A-Za-z0-9_]{1,16}\Z")
_ITEM_ID = re.compile(r"[a-z0-9_.-]+:[a-z0-9/._-]+\Z")


def validate_player_name(name: str) -> str:
    if not _PLAYER_NAME.fullmatch(name):
        raise RconError("Java-Spielernamen dürfen nur 1–16 Buchstaben, Ziffern und Unterstriche enthalten.")
    return name


def normalize_item_id(item_id: str) -> str:
    normalized = item_id.lower()
    if ":" not in normalized:
        normalized = f"minecraft:{normalized}"
    if not _ITEM_ID.fullmatch(normalized):
        raise RconError("Ungültige Item-ID; erlaubt sind IDs wie minecraft:diamond oder modname:item_name.")
    return normalized


def player_data_disabled_reason(server_type: str, minecraft_version: str) -> str | None:
    if server_type != "vanilla":
        return f"Spielerdaten über RCON wurden für {server_type} noch nicht auf Kompatibilität geprüft."
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", minecraft_version)
    if not match:
        return "Die Minecraft-Version konnte nicht geprüft werden; RCON-Spielerdaten bleiben vorsichtshalber deaktiviert."
    if tuple(map(int, match.groups())) < (1, 13):
        return "RCON-Spielerdaten benötigen Minecraft 1.13 oder neuer (data get)."
    return None


def inventory_editing_disabled_reason(server_type: str, minecraft_version: str) -> str | None:
    reason = player_data_disabled_reason(server_type, minecraft_version)
    if reason:
        return reason
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", minecraft_version)
    if match and tuple(map(int, match.groups())) < (1, 17):
        return "Sichere Inventaränderungen über /item replace benötigen Minecraft 1.17 oder neuer."
    return None


@dataclass(frozen=True)
class RconPacket:
    request_id: int
    packet_type: int
    payload: str


def encode_packet(request_id: int, packet_type: int, payload: str) -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise RconError("RCON-Verbindung wurde unerwartet geschlossen.")
        chunks.extend(chunk)
    return bytes(chunks)


def receive_packet(sock: socket.socket) -> RconPacket:
    length = struct.unpack("<i", _recv_exact(sock, 4))[0]
    if length < 10 or length > 4 * 1024 * 1024:
        raise RconError(f"Ungültige RCON-Paketlänge: {length}")
    body = _recv_exact(sock, length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    payload_bytes = body[8:]
    if payload_bytes.endswith(b"\x00\x00"):
        payload_bytes = payload_bytes[:-2]
    return RconPacket(request_id, packet_type, payload_bytes.decode("utf-8", errors="replace"))


class RconClient:
    """Small Source-RCON client. Call only on an explicitly secured local endpoint."""

    SERVERDATA_RESPONSE_VALUE = 0
    SERVERDATA_EXECCOMMAND = 2
    SERVERDATA_AUTH_RESPONSE = 2
    SERVERDATA_AUTH = 3

    def __init__(self, host: str, port: int, password: str, timeout: float = 3.0) -> None:
        try:
            address = ipaddress.ip_address(host)
            numeric_port = int(port)
        except ValueError as exc:
            raise RconError("RCON darf ausschließlich über eine numerische Loopback-IP angesprochen werden.") from exc
        if not address.is_loopback:
            raise RconError("Unsicherer RCON-Zielhost: nur Loopback-Adressen 127.0.0.1 oder ::1 sind zulässig.")
        if not 1 <= numeric_port <= 65535:
            raise RconError("Ungültiger RCON-Port.")
        if not password or "\x00" in password:
            raise RconError("Das RCON-Passwort darf nicht leer sein oder Nullbytes enthalten.")
        self.host = str(address)
        self.port = numeric_port
        self.password = password
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._request_id = 1

    def connect(self) -> None:
        try:
            self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._socket.settimeout(self.timeout)
            self._socket.sendall(encode_packet(self._request_id, self.SERVERDATA_AUTH, self.password))
            # A few implementations send an empty response packet before auth confirmation.
            response = receive_packet(self._socket)
            if response.request_id == -1:
                raise RconError("RCON-Anmeldung abgelehnt; Passwort prüfen.")
            if response.request_id == self._request_id and response.packet_type == self.SERVERDATA_AUTH_RESPONSE:
                return
            if response.request_id == self._request_id and response.packet_type == self.SERVERDATA_RESPONSE_VALUE:
                response = receive_packet(self._socket)
                if response.request_id == self._request_id and response.packet_type == self.SERVERDATA_AUTH_RESPONSE:
                    return
            raise RconError("Unerwartete Antwort bei der RCON-Anmeldung.")
        except (OSError, TimeoutError) as exc:
            self.close()
            raise RconError(f"RCON-Verbindung fehlgeschlagen: {exc}") from exc
        except RconError:
            self.close()
            raise

    def command(self, command: str) -> str:
        if self._socket is None:
            self.connect()
        assert self._socket is not None
        self._request_id += 1
        command_id = self._request_id
        terminator_id = command_id + 1
        try:
            self._socket.sendall(encode_packet(command_id, self.SERVERDATA_EXECCOMMAND, command))
            # Sentinel request lets us collect commands that span multiple RCON packets.
            self._socket.sendall(encode_packet(terminator_id, self.SERVERDATA_RESPONSE_VALUE, ""))
            chunks: list[str] = []
            for _ in range(128):
                response = receive_packet(self._socket)
                if response.request_id == command_id:
                    chunks.append(response.payload)
                if response.request_id == terminator_id:
                    break
            else:
                raise RconError("RCON-Antwort besteht aus zu vielen Paketen.")
            return "".join(chunks)
        except (OSError, TimeoutError) as exc:
            raise RconError(f"RCON-Befehl fehlgeschlagen: {exc}") from exc

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
