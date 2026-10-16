import socket
import struct
import threading
import unittest
from unittest.mock import patch

from minecraft_server_creator.rcon import (
    RconClient,
    RconError,
    RconPacket,
    encode_packet,
    inventory_editing_disabled_reason,
    normalize_item_id,
    player_data_disabled_reason,
    receive_packet,
    validate_player_name,
)


class RconTests(unittest.TestCase):
    def test_packet_encoding_and_decoding(self):
        packet = encode_packet(7, 2, "list")
        length = struct.unpack("<i", packet[:4])[0]
        self.assertEqual(length, len(packet) - 4)
        left, right = socket.socketpair()
        try:
            left.sendall(packet)
            received = receive_packet(right)
        finally:
            left.close()
            right.close()
        self.assertEqual(received, RconPacket(7, 2, "list"))

    def test_client_authenticates_and_runs_a_command(self):
        client_socket, server_socket = socket.socketpair()
        failures = []

        def fake_server():
            try:
                auth = receive_packet(server_socket)
                self.assertEqual(auth.packet_type, RconClient.SERVERDATA_AUTH)
                server_socket.sendall(encode_packet(auth.request_id, RconClient.SERVERDATA_AUTH_RESPONSE, ""))
                command = receive_packet(server_socket)
                terminator = receive_packet(server_socket)
                self.assertEqual(command.payload, "list")
                server_socket.sendall(encode_packet(command.request_id, RconClient.SERVERDATA_RESPONSE_VALUE, "There are 1 players: Alex"))
                server_socket.sendall(encode_packet(terminator.request_id, RconClient.SERVERDATA_RESPONSE_VALUE, ""))
            except Exception as exc:  # surfaced in the test thread
                failures.append(exc)
            finally:
                server_socket.close()

        thread = threading.Thread(target=fake_server, daemon=True)
        thread.start()
        try:
            with patch("minecraft_server_creator.rcon.socket.create_connection", return_value=client_socket):
                with RconClient("127.0.0.1", 25575, "test-secret") as client:
                    result = client.command("list")
        finally:
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result, "There are 1 players: Alex")

    def test_client_rejects_non_loopback_destinations(self):
        with self.assertRaisesRegex(RconError, "Loopback"):
            RconClient("203.0.113.1", 25575, "secret")
        with self.assertRaises(RconError):
            RconClient("rcon.example.test", 25575, "secret")

    def test_player_and_item_input_is_restricted(self):
        self.assertEqual(validate_player_name("Alex_123"), "Alex_123")
        with self.assertRaises(RconError):
            validate_player_name("Alex;op Notch")
        self.assertEqual(normalize_item_id("Diamond"), "minecraft:diamond")
        self.assertEqual(normalize_item_id("examplemod:magic_sword"), "examplemod:magic_sword")
        with self.assertRaises(RconError):
            normalize_item_id("diamond;op Alex")

    def test_inventory_capabilities_are_version_and_type_gated(self):
        self.assertIsNone(inventory_editing_disabled_reason("vanilla", "1.21.4"))
        self.assertIn("1.17", inventory_editing_disabled_reason("vanilla", "1.16.5"))
        self.assertIn("Kompatibilität", inventory_editing_disabled_reason("forge", "1.21.4"))
        self.assertIn("1.13", player_data_disabled_reason("vanilla", "1.12.2"))


if __name__ == "__main__":
    unittest.main()
