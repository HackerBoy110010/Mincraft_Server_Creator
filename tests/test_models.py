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

    def test_modded_profile_launch_metadata_round_trip(self):
        profile = ServerProfile(
            id="forge1",
            name="Forge",
            slug="forge",
            server_type="forge",
            minecraft_version="1.21.4",
            java_major=21,
            server_dir="/tmp/forge",
            loader_version="54.1.0",
            launch_mode="args",
            launch_target="libraries/net/minecraftforge/forge/1.21.4-54.1.0/unix_args.txt",
        )
        restored = ServerProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.loader_version, "54.1.0")
        self.assertEqual(restored.launch_mode, "args")
        self.assertEqual(restored.launch_target, profile.launch_target)

    def test_arclight_profile_round_trips_selected_base_loader(self):
        profile = ServerProfile(
            id="arc1",
            name="Arclight",
            slug="arclight",
            server_type="arclight",
            minecraft_version="1.21.1",
            java_major=21,
            server_dir="/tmp/arclight",
            loader_version="1.0.1-8ec9529",
            loader_family="forge",
        )
        restored = ServerProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.loader_family, "forge")
        self.assertEqual(restored.launch_target, "server.jar")

    def test_arclight_requires_supported_base_loader(self):
        with self.assertRaises(ValidationError):
            ServerProfile(
                id="arc1",
                name="Arclight",
                slug="arclight",
                server_type="arclight",
                minecraft_version="1.21.1",
                java_major=21,
                server_dir="/tmp/arclight",
            )

    def test_profile_rejects_unsafe_launch_target(self):
        with self.assertRaises(ValidationError):
            ServerProfile(
                id="forge1",
                name="Forge",
                slug="forge",
                server_type="forge",
                minecraft_version="1.21.4",
                java_major=21,
                server_dir="/tmp/forge",
                loader_version="54.1.0",
                launch_mode="args",
                launch_target="../outside/unix_args.txt",
            )

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
