import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_server_creator.config import ConfigStore
from minecraft_server_creator.manager import ManagerError, ServerManager
from minecraft_server_creator.models import PortSettings
from minecraft_server_creator.properties import read_properties, update_properties


class FakeMetadata:
    def resolve_version(self, requested):
        return "1.21.4" if not requested or requested == "latest" else requested

    def version_info(self, version):
        return {"javaVersion": {"majorVersion": 21}}

    def install_vanilla(self, version, server_dir):
        target = Path(server_dir) / "server.jar"
        target.write_bytes(b"fake server jar")
        return {"id": version}, target


class FakeServices:
    def __init__(self, active=False):
        self.active = active

    def is_active(self, profile):
        return self.active


class FakeRcon:
    def __init__(self):
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def command(self, command):
        self.commands.append(command)
        return "ok"


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

    def test_rcon_is_local_only_and_secret_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("RCON test")
            with patch("minecraft_server_creator.manager.port_available", return_value=True):
                manager.enable_rcon(profile.slug)
            properties = read_properties(Path(profile.server_dir) / "server.properties")
            secret_path = Path(profile.server_dir) / "config" / "rcon-password"
            password = secret_path.read_text(encoding="utf-8").strip()
            self.assertEqual(properties["enable-rcon"], "true")
            self.assertEqual(properties["rcon.ip"], "127.0.0.1")
            self.assertEqual(properties["rcon.port"], "25575")
            self.assertEqual(properties["rcon.password"], password)
            self.assertTrue(manager.rcon_is_enabled(profile.slug))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ManagerError, "dürfen nicht identisch sein"):
                manager.update_ports(profile.slug, PortSettings(java_tcp=25575))
            manager.disable_rcon(profile.slug)
            self.assertFalse(manager.rcon_is_enabled(profile.slug))
            self.assertFalse(secret_path.exists())

    def test_rcon_setup_requires_stopped_server_and_nonconflicting_port(self):
        with tempfile.TemporaryDirectory() as temp:
            services = FakeServices(active=True)
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), services=services
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("RCON active")
            with self.assertRaisesRegex(ManagerError, "gestoppt"):
                manager.enable_rcon(profile.slug)

        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), services=FakeServices()
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Port conflict", ports=PortSettings(java_tcp=25575))
            with self.assertRaisesRegex(ManagerError, "belegt bereits"):
                manager.enable_rcon(profile.slug)

    def test_rcon_client_refuses_a_non_loopback_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            services = FakeServices()
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), services=services
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("RCON binding")
            with patch("minecraft_server_creator.manager.port_available", return_value=True):
                manager.enable_rcon(profile.slug)
            update_properties(Path(profile.server_dir) / "server.properties", {"rcon.ip": "0.0.0.0"})
            services.active = True
            with self.assertRaisesRegex(ManagerError, "127.0.0.1"):
                manager.rcon_players(profile.slug)

    def test_inventory_edit_builds_a_constrained_live_player_command(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(active=True),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Inventory")
            fake_rcon = FakeRcon()
            manager._rcon_client = lambda _: fake_rcon
            self.assertEqual(manager.rcon_set_inventory_item(profile.slug, "Alex", 4, "diamond", 2), "ok")
            self.assertEqual(
                fake_rcon.commands,
                ["item replace entity @a[name=Alex,limit=1] hotbar.4 with minecraft:diamond 2"],
            )
            manager.rcon_set_inventory_item(profile.slug, "Alex", 9, "diamond", 1)
            self.assertEqual(
                fake_rcon.commands[-1],
                "item replace entity @a[name=Alex,limit=1] inventory.0 with minecraft:diamond 1",
            )
            with self.assertRaisesRegex(ManagerError, "Java-Spielernamen"):
                manager.rcon_set_inventory_item(profile.slug, "Alex;op", 4, "diamond", 2)
            with self.assertRaisesRegex(ManagerError, "Inventarplatz"):
                manager.rcon_set_inventory_item(profile.slug, "Alex", 36, "diamond", 2)


if __name__ == "__main__":
    unittest.main()
