import socket
import unittest

from minecraft_server_creator.models import PortSettings
from minecraft_server_creator.network import describe_endpoints, port_available


class NetworkTests(unittest.TestCase):
    def test_detects_bound_tcp_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            self.assertFalse(port_available(port, "tcp", "127.0.0.1"))
        finally:
            sock.close()
        self.assertTrue(port_available(port, "tcp", "127.0.0.1"))

    def test_ipv6_endpoint_uses_brackets(self):
        lines = describe_endpoints(PortSettings(25565, None, None), "2001:db8::1", None)
        self.assertEqual(lines, ["Öffentlich konfiguriert: [2001:db8::1]:25565 (Java/TCP)"])

    def test_rejects_unknown_protocol(self):
        with self.assertRaises(ValueError):
            port_available(25565, "sctp")


if __name__ == "__main__":
    unittest.main()
