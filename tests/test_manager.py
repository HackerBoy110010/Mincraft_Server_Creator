import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_server_creator.config import ConfigStore
from minecraft_server_creator.manager import ManagerError, ServerManager
from minecraft_server_creator.loaders import LoaderInstallResult
from minecraft_server_creator.models import PortSettings
from minecraft_server_creator.properties import read_properties, update_properties


class FakeMetadata:
    def resolve_version(self, requested):
        return "1.21.4" if not requested or requested == "latest" else requested

    def latest_release(self):
        return "1.21.4"

    def version_info(self, version):
        return {"javaVersion": {"majorVersion": 21}}

    def install_vanilla(self, version, server_dir):
        target = Path(server_dir) / "server.jar"
        target.write_bytes(b"fake server jar")
        return {"id": version}, target


class FakeFabric:
    def install(self, version, version_info, server_dir):
        target = Path(server_dir) / "server.jar"
        target.write_bytes(b"fake Fabric server jar")
        return LoaderInstallResult("0.18.1", "jar", "server.jar")


class FakeRuntime:
    def __init__(self):
        self.features = []

    def ensure(self, feature):
        self.features.append(feature)
        return Path("/managed/java")


class FakeForge:
    def install(self, version, version_info, server_dir, java_path):
        loader_version = "54.1.0"
        target = f"libraries/net/minecraftforge/forge/{version}-{loader_version}/unix_args.txt"
        launch_file = Path(server_dir) / target
        launch_file.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text("-cp fake Main", encoding="utf-8")
        return LoaderInstallResult(loader_version, "args", target)


class FakeNeoForge:
    def install(self, version, version_info, server_dir, java_path):
        loader_version = "21.1.100"
        target = f"libraries/net/neoforged/neoforge/{loader_version}/unix_args.txt"
        launch_file = Path(server_dir) / target
        launch_file.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text("-cp fake Main", encoding="utf-8")
        return LoaderInstallResult(loader_version, "args", target)


class FakeArclight:
    def install(self, version, version_info, server_dir, loader_family):
        target = Path(server_dir) / "server.jar"
        target.write_bytes(b"fake Arclight server jar")
        return LoaderInstallResult("1.0.1-8ec9529", "jar", "server.jar", loader_family)


class FakeGeyserInstaller:
    def __init__(self):
        self.prepared = []
        self.installed = []
        self.configured = []

    def prepare(self, profile, *, allow_beta=False):
        self.prepared.append((profile.slug, allow_beta))
        return "geyser-plan"

    def install(self, profile, plan):
        self.installed.append((profile.slug, plan))
        return "geyser-result"

    def configure(self, profile):
        self.configured.append(profile.slug)
        return Path(profile.server_dir) / "config" / "Geyser-Fabric" / "config.yml"


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

    def test_create_fabric_profile_uses_the_loader_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            fabric = FakeFabric()
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), fabric=fabric
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Fabric world", server_type="fabric")
            self.assertEqual(profile.server_type, "fabric")
            self.assertEqual((Path(profile.server_dir) / "server.jar").read_bytes(), b"fake Fabric server jar")

    def test_create_forge_profile_installs_with_managed_java_and_persists_launch_args(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = FakeRuntime()
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                runtimes=runtime,
                forge=FakeForge(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Forge world", server_type="forge")
            expected = "libraries/net/minecraftforge/forge/1.21.4-54.1.0/unix_args.txt"
            self.assertEqual(runtime.features, [21])
            self.assertEqual(profile.launch_mode, "args")
            self.assertEqual(profile.launch_target, expected)
            self.assertEqual(profile.loader_version, "54.1.0")
            self.assertTrue((Path(profile.server_dir) / expected).is_file())
            restored = manager.get_profile(profile.slug)
            self.assertEqual(restored.launch_target, expected)

    def test_create_neoforge_profile_persists_its_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                runtimes=FakeRuntime(),
                neoforge=FakeNeoForge(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("NeoForge world", server_type="neoforge")
            expected = "libraries/net/neoforged/neoforge/21.1.100/unix_args.txt"
            self.assertEqual(profile.launch_target, expected)
            self.assertEqual(profile.loader_version, "21.1.100")
            self.assertTrue((Path(profile.server_dir) / expected).is_file())

    def test_create_arclight_profile_requires_and_persists_base_loader(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                arclight=FakeArclight(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            with self.assertRaisesRegex(ManagerError, "explizite Basis-Auswahl"):
                manager.create_server("Arclight missing base", server_type="arclight")
            profile = manager.create_server("Arclight forge", server_type="arclight", arclight_loader="forge")
            self.assertEqual(profile.loader_family, "forge")
            self.assertEqual(profile.loader_version, "1.0.1-8ec9529")
            self.assertTrue((Path(profile.server_dir) / "server.jar").is_file())
            self.assertEqual(manager.get_profile(profile.slug).loader_family, "forge")

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

    def test_resource_pack_configuration_preserves_properties_and_requires_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Pack test")
            properties_path = manager.configure_resource_pack(
                profile.slug,
                url="https://cdn.modrinth.com/data/example/pack.zip",
                sha1="A" * 40,
                required=True,
            )
            values = read_properties(properties_path)
            self.assertEqual(values["resource-pack"], "https://cdn.modrinth.com/data/example/pack.zip")
            self.assertEqual(values["resource-pack-sha1"], "a" * 40)
            self.assertEqual(values["require-resource-pack"], "true")
            self.assertEqual(values["server-port"], "25565")

            manager.configure_resource_pack(
                profile.slug,
                url="https://cdn.modrinth.com/data/example/optional.zip",
                sha1="b" * 40,
                required=False,
            )
            self.assertEqual(read_properties(properties_path)["require-resource-pack"], "false")

    def test_resource_pack_validation_rejects_bad_url_hash_and_running_server(self):
        with tempfile.TemporaryDirectory() as temp:
            services = FakeServices()
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), services=services
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Pack validation")
            with self.assertRaisesRegex(ManagerError, "HTTPS"):
                manager.configure_resource_pack(
                    profile.slug, url="http://example.com/pack.zip", sha1="a" * 40, required=True
                )
            with self.assertRaisesRegex(ManagerError, "40 Hexadezimalzeichen"):
                manager.configure_resource_pack(
                    profile.slug, url="https://example.com/pack.zip", sha1="bad", required=True
                )
            services.active = True
            with self.assertRaisesRegex(ManagerError, "Stoppe den Server"):
                manager.configure_resource_pack(
                    profile.slug, url="https://example.com/pack.zip", sha1="a" * 40, required=True
                )

    def test_rejects_unknown_server_type_without_creating_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata())
            manager.setup(Path(temp) / "servers", PortSettings())
            with self.assertRaisesRegex(Exception, "noch nicht implementiert"):
                manager.create_server("Unsupported world", server_type="unknown")
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


    def test_geyser_operations_require_latest_stable_fabric_and_bedrock_port(self):
        with tempfile.TemporaryDirectory() as temp:
            geyser = FakeGeyserInstaller()
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
                fabric=FakeFabric(),
                geyser=geyser,
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            fabric = manager.create_server("Current Fabric", server_type="fabric")
            plan = manager.prepare_geyser_install(fabric.slug, allow_beta=True)
            self.assertEqual(plan, "geyser-plan")
            self.assertEqual(geyser.prepared, [(fabric.slug, True)])
            self.assertEqual(manager.install_geyser(fabric.slug, plan), "geyser-result")
            self.assertEqual(geyser.installed, [(fabric.slug, "geyser-plan")])
            self.assertEqual(
                manager.configure_geyser(fabric.slug),
                Path(fabric.server_dir) / "config" / "Geyser-Fabric" / "config.yml",
            )
            self.assertEqual(geyser.configured, [fabric.slug])

            vanilla = manager.create_server("Vanilla")
            with self.assertRaisesRegex(ManagerError, "reinen Fabric"):
                manager.prepare_geyser_install(vanilla.slug)

            older = manager.create_server("Older Fabric", server_type="fabric", version="1.20.1")
            with self.assertRaisesRegex(ManagerError, "neueste stabile Minecraft-Version"):
                manager.prepare_geyser_install(older.slug)

            no_bedrock = manager.create_server(
                "No Bedrock", server_type="fabric", ports=PortSettings(bedrock_udp=None)
            )
            with self.assertRaisesRegex(ManagerError, "Bedrock-Port"):
                manager.prepare_geyser_install(no_bedrock.slug)

    def test_backup_reports_live_progress_and_excludes_logs_and_old_backups(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Backup progress")
            server_dir = Path(profile.server_dir)
            world = server_dir / "world"
            world.mkdir()
            (world / "region.dat").write_bytes(b"world data" * 100_000)
            (server_dir / "logs").mkdir(exist_ok=True)
            (server_dir / "logs" / "latest.log").write_text("log", encoding="utf-8")
            messages = []

            archive_path = manager.backup(profile.slug, progress=messages.append)

            self.assertTrue(archive_path.is_file())
            self.assertTrue(any("Backup-Fortschritt:" in message for message in messages))
            self.assertTrue(any("100.0%" in message for message in messages))
            self.assertIn("Komprimierung startet", messages[1])
            self.assertTrue(messages[-1].startswith("Vollständiges Backup abgeschlossen:"))
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("world/region.dat", names)
            self.assertNotIn("logs/latest.log", names)
            self.assertFalse(any(name.startswith("backups/") for name in names))

    def test_backup_removes_partial_archive_on_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Interrupted backup")
            backup_dir = Path(profile.server_dir) / "backups"

            with patch("minecraft_server_creator.manager._add_backup_path", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    manager.backup(profile.slug, progress=lambda _message: None)

            self.assertEqual(list(backup_dir.glob("*.tar.gz")), [])

    def test_regenerate_world_backs_up_and_resets_all_dimensions_with_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("World reset")
            server_dir = Path(profile.server_dir)
            update_properties(
                server_dir / "server.properties",
                {"level-name": "world", "level-seed": "old-seed"},
            )
            for folder in ("world", "world_nether", "world_the_end"):
                world_dir = server_dir / folder
                world_dir.mkdir()
                (world_dir / "marker.dat").write_text(folder, encoding="utf-8")

            world_name, backup_path = manager.regenerate_world(
                profile.slug, seed="oak valley", create_backup=True
            )

            self.assertEqual(world_name, "world")
            self.assertIsNotNone(backup_path)
            self.assertTrue(backup_path.is_file())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            self.assertEqual(read_properties(server_dir / "server.properties")["level-seed"], "oak valley")
            for folder in ("world", "world_nether", "world_the_end"):
                self.assertFalse((server_dir / folder).exists())
            with tarfile.open(backup_path, "r:gz") as archive:
                names = set(archive.getnames())
            for folder in ("world", "world_nether", "world_the_end"):
                self.assertIn(f"{folder}/marker.dat", names)

    def test_regenerate_world_without_seed_uses_random_seed_and_can_skip_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"),
                metadata=FakeMetadata(),
                services=FakeServices(),
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Random world")
            server_dir = Path(profile.server_dir)
            update_properties(server_dir / "server.properties", {"level-seed": "previous-seed"})
            (server_dir / "world").mkdir()

            world_name, backup_path = manager.regenerate_world(
                profile.slug, seed=None, create_backup=False
            )

            self.assertEqual(world_name, "world")
            self.assertIsNone(backup_path)
            self.assertEqual(read_properties(server_dir / "server.properties")["level-seed"], "")
            self.assertFalse((server_dir / "world").exists())

    def test_regenerate_world_requires_stopped_server_and_safe_level_name(self):
        with tempfile.TemporaryDirectory() as temp:
            services = FakeServices(active=True)
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), services=services
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Active world")
            with self.assertRaisesRegex(ManagerError, "Stoppe den Server"):
                manager.regenerate_world(profile.slug, seed=None, create_backup=False)

        with tempfile.TemporaryDirectory() as temp:
            manager = ServerManager(
                store=ConfigStore(Path(temp) / "config.json"), metadata=FakeMetadata(), services=FakeServices()
            )
            manager.setup(Path(temp) / "servers", PortSettings())
            profile = manager.create_server("Unsafe level")
            server_dir = Path(profile.server_dir)
            outside = Path(temp) / "outside"
            outside.mkdir()
            update_properties(server_dir / "server.properties", {"level-name": "../outside"})
            with self.assertRaisesRegex(ManagerError, "sicherer Ordnername"):
                manager.regenerate_world(profile.slug, seed=None, create_backup=False)
            self.assertTrue(outside.is_dir())


if __name__ == "__main__":
    unittest.main()
