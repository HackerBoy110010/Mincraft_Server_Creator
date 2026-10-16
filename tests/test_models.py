import unittest

from minecraft_server_creator.models import PortSettings, ServerProfile, ValidationError, slugify


class ModelTests(unittest.TestCase):
    def test_slugify_normalizes_names(self):
        self.assertEqual(slugify("Überleben mit Freunden!"), "uberleben-mit-freunden")
        self.assertEqual(slugify("  !!!  "), "server")

    def test_ports_accept_optional_udp_values(self):
        ports = PortSettings(java_tcp=25570, bedrock_udp=None, voice_udp=24460)
        self.assertEqual(ports.to_dict(), {"java_tcp": 25570, "bedrock_udp": None, "voice_udp": 24460})

    def test_ports_reject_invalid_values(self):
        with self.assertRaises(ValidationError):
            PortSettings(java_tcp=0)
        with self.assertRaises(ValidationError):
            PortSettings(java_tcp=25565, bedrock_udp=65536)
        with self.assertRaises(ValidationError):
            PortSettings(java_tcp=25565, bedrock_udp=24454, voice_udp=24454)

    def test_profile_round_trip(self):
        profile = ServerProfile(
            id="abc123",
            name="Home Server",
            slug="home-server",
            server_type="vanilla",
            minecraft_version="1.21.4",
            java_major=21,
            server_dir="/tmp/home-server",
            ports=PortSettings(25565, 19132, None),
            auto_start_on_boot=True,
        )
        restored = ServerProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.name, "Home Server")
        self.assertEqual(restored.ports, profile.ports)
        self.assertTrue(restored.auto_start_on_boot)


if __name__ == "__main__":
    unittest.main()
