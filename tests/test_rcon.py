import socket
import struct
import unittest

from minecraft_server_creator.rcon import RconPacket, encode_packet, receive_packet


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


if __name__ == "__main__":
    unittest.main()
