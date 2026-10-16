import tempfile
import unittest
from pathlib import Path

from minecraft_server_creator.config import ConfigStore
from minecraft_server_creator.manager import ManagerError, ServerManager
from minecraft_server_creator.models import PortSettings
from minecraft_server_creator.properties import read_properties


class FakeMetadata:
    def resolve_version(self, requested):
        return "1.21.4" if not requested or requested == "latest" else requested

    def version_info(self, version):
        return {"javaVersion": {"majorVersion": 21}}

    def install_vanilla(self, version, server_dir):
        target = Path(server_dir) / "server.jar"
        target.write_bytes(b"fake server jar")
        return {"id": version}, target


class ManagerTests(unittest.TestCase):
    def test_create_profile_and_write_server_properties(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "servers"
            manager = ServerManager(store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata())
            manager.setup(root, PortSettings(25571, 19140, 24460))
            profile = manager.create_server("Home Friends", ports=PortSettings(25572, None, 24461))
            self.assertEqual(profile.minecraft_version, "1.21.4")
            self.assertTrue((Path(profile.server_dir) / "server.jar").is_file())
            self.assertEqual((Path(profile.server_dir) / "eula.txt").read_text(encoding="utf-8"), "eula=false\n")
            self.assertIn("server-port=25572", (Path(profile.server_dir) / "server.properties").read_text(encoding="utf-8"))
            self.assertIsNone(manager.get_profile("home-friends").ports.bedrock_udp)

    def test_guided_settings_validate_and_update_properties(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata())
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Settings", ports=PortSettings())
            manager.update_settings(
                profile.slug,
                {"motd": "Friends server", "max-players": "12", "difficulty": "hard", "pvp": "false"},
            )
            values = read_properties(Path(profile.server_dir) / "server.properties")
            self.assertEqual(values["motd"], "Friends server")
            self.assertEqual(values["max-players"], "12")
            self.assertEqual(values["difficulty"], "hard")
            self.assertEqual(values["pvp"], "false")
            with self.assertRaises(ManagerError):
                manager.update_settings(profile.slug, {"difficulty": "impossible"})

    def test_rejects_unimplemented_loader_without_creating_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata())
            manager.setup(Path(temp) / "servers", PortSettings())
            with self.assertRaisesRegex(Exception, "noch nicht implementiert"):
                manager.create_server("Forge world", server_type="forge")
            self.assertEqual(manager.load().profiles, [])


if __name__ == "__main__":
    unittest.main()
