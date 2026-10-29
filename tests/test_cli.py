from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from minecraft_server_creator.cli import _mods_command, _world_regenerate_command, build_parser, main
from minecraft_server_creator.manager import ManagerError


class CliParserTests(unittest.TestCase):
    def test_create_arguments(self):
        parser = build_parser()
        args = parser.parse_args(["create", "my world", "--type", "vanilla", "--version", "latest", "--java-port", "25570"])
        self.assertEqual(args.command, "create")
        self.assertEqual(args.name, "my world")
        self.assertEqual(args.java_port, 25570)
        forge = parser.parse_args(["create", "modded", "--type", "forge"])
        self.assertEqual(forge.type, "forge")
        arclight = parser.parse_args(
            ["create", "hybrid", "--type", "arclight", "--arclight-loader", "neoforge"]
        )
        self.assertEqual(arclight.arclight_loader, "neoforge")
        catalog_search = parser.parse_args(
            ["catalog", "search", "home", "minimap", "--source", "modrinth", "--kind", "mod"]
        )
        self.assertEqual(catalog_search.catalog_action, "search")
        self.assertEqual(catalog_search.source, "modrinth")
        self.assertEqual(catalog_search.kind, "mod")
        catalog_install = parser.parse_args(
            [
                "catalog", "install", "home", "project-id", "--source", "modrinth", "--kind", "mod",
                "--version-id", "version-id", "--allow-beta",
            ]
        )
        self.assertEqual(catalog_install.version_id, "version-id")
        self.assertTrue(catalog_install.allow_beta)
        forced_pack = parser.parse_args(
            [
                "resource-pack", "set", "home", "--url", "https://cdn.modrinth.com/pack.zip",
                "--sha1", "a" * 40, "--required",
            ]
        )
        self.assertEqual(forced_pack.command, "resource-pack")
        self.assertEqual(forced_pack.resource_pack_action, "set")
        self.assertTrue(forced_pack.required)
        datapack_search = parser.parse_args(
            ["catalog", "search", "home", "terrain", "--source", "curseforge", "--kind", "datapack"]
        )
        self.assertEqual(datapack_search.kind, "datapack")
        self.assertEqual(datapack_search.source, "curseforge")
        geyser = parser.parse_args(["geyser", "install", "fabric-home", "--allow-beta", "--yes"])
        self.assertEqual(geyser.command, "geyser")
        self.assertEqual(geyser.geyser_action, "install")
        self.assertTrue(geyser.allow_beta)
        self.assertTrue(geyser.yes)
        configure_geyser = parser.parse_args(["geyser", "configure", "fabric-home"])
        self.assertEqual(configure_geyser.geyser_action, "configure")
        regenerate = parser.parse_args(
            ["world", "regenerate", "home", "--seed", "oak-valley", "--backup", "--yes"]
        )
        self.assertEqual(regenerate.command, "world")
        self.assertEqual(regenerate.world_action, "regenerate")
        self.assertEqual(regenerate.seed, "oak-valley")
        self.assertTrue(regenerate.backup)
        self.assertTrue(regenerate.yes)
        random_world = parser.parse_args(["world", "regenerate", "home", "--no-backup"])
        self.assertIsNone(random_world.seed)
        self.assertFalse(random_world.backup)

        mods_update = parser.parse_args(["mods", "update", "home", "--backup", "--yes", "--allow-beta"])
        self.assertEqual(mods_update.mods_action, "update")
        self.assertTrue(mods_update.backup)
        self.assertTrue(mods_update.yes)
        self.assertTrue(mods_update.allow_beta)
        self.assertEqual(mods_update.startup_timeout, 180)
        mods_rollback = parser.parse_args(["mods", "rollback", "home", "txn-123", "--yes"])
        self.assertEqual(mods_rollback.transaction_id, "txn-123")
        mods_clumps = parser.parse_args(["mods", "install-clumps", "home", "--yes"])
        self.assertEqual(mods_clumps.mods_action, "install-clumps")
        mods_backups = parser.parse_args(["mods", "backups", "home"])
        self.assertEqual(mods_backups.mods_action, "backups")

    def test_world_regeneration_requires_explicit_noninteractive_backup_and_confirmation(self):
        class FakeManager:
            def __init__(self):
                self.services = SimpleNamespace(is_active=lambda _profile: False)
                self.calls = []

            @staticmethod
            def get_profile(selector):
                return SimpleNamespace(slug=selector)

            def regenerate_world(self, selector, *, seed, create_backup):
                self.calls.append((selector, seed, create_backup))
                return "world", None

        manager = FakeManager()
        args = SimpleNamespace(server="home", seed="12345", backup=None, yes=True)
        with patch("minecraft_server_creator.cli.sys.stdin", SimpleNamespace(isatty=lambda: False)):
            with self.assertRaisesRegex(ManagerError, "--backup oder --no-backup"):
                _world_regenerate_command(manager, args)
            args.backup = False
            args.yes = False
            with self.assertRaisesRegex(ManagerError, "--yes"):
                _world_regenerate_command(manager, args)
            args.yes = True
            _world_regenerate_command(manager, args)

        self.assertEqual(manager.calls, [("home", "12345", False)])

    def test_world_regeneration_interactive_flow_asks_seed_backup_and_confirmation(self):
        class FakeManager:
            def __init__(self):
                self.services = SimpleNamespace(is_active=lambda _profile: False)
                self.calls = []

            @staticmethod
            def get_profile(selector):
                return SimpleNamespace(slug=selector)

            def regenerate_world(self, selector, *, seed, create_backup):
                self.calls.append((selector, seed, create_backup))
                return "world", None

        manager = FakeManager()
        args = SimpleNamespace(server="home", seed=None, backup=None, yes=False)
        with patch("minecraft_server_creator.cli.sys.stdin", SimpleNamespace(isatty=lambda: True)):
            with patch("builtins.input", side_effect=["12345", "", "home"]):
                _world_regenerate_command(manager, args)
        self.assertEqual(manager.calls, [("home", "12345", True)])

    def test_mods_command_dispatches_and_clumps_uses_modrinth_project_id(self):
        store = SimpleNamespace(exists=True)
        manager = object()
        with patch("minecraft_server_creator.cli.ConfigStore", return_value=store), patch(
            "minecraft_server_creator.cli.ServerManager", return_value=manager
        ), patch("minecraft_server_creator.cli._ensure_setup") as setup, patch(
            "minecraft_server_creator.cli._mods_command"
        ) as command:
            self.assertEqual(main(["mods", "backups", "home"]), 0)
        setup.assert_called_once_with(manager, store)
        self.assertEqual(command.call_args.args[0], manager)
        self.assertEqual(command.call_args.args[1].mods_action, "backups")

        profile = SimpleNamespace(slug="home")
        manager = SimpleNamespace(get_profile=lambda _selector: profile)
        with patch("minecraft_server_creator.cli.ContentCatalog"), patch(
            "minecraft_server_creator.cli._install_catalog_project"
        ) as install:
            _mods_command(manager, build_parser().parse_args(["mods", "install-clumps", "home", "--yes"]))
        self.assertEqual(install.call_args.args[2:], ("home", "modrinth", "mod", "Wnxd13zP"))
        self.assertTrue(install.call_args.kwargs["assume_yes"])

    def test_noninteractive_mod_update_requires_backup_choice_and_explicit_yes(self):
        profile = SimpleNamespace(slug="home", name="Home", minecraft_version="1.21.1")
        manager = SimpleNamespace(get_profile=lambda _selector: profile)
        candidate = SimpleNamespace(
            plan=SimpleNamespace(project_title="Example", file_version="2.0"),
            record={"file_version": "1.0"},
        )
        prepared = SimpleNamespace(candidates=(candidate,), skipped=())
        report = SimpleNamespace(
            outcomes=[], backup_dir=None, full_server_backup=None, transaction_id=None, fatal_error=None
        )
        updater = SimpleNamespace(prepare=lambda *_args, **_kwargs: prepared, apply=Mock(return_value=report))
        with patch("minecraft_server_creator.cli.ContentCatalog"), patch(
            "minecraft_server_creator.cli.SafeModUpdater", return_value=updater
        ), patch("minecraft_server_creator.cli.sys.stdin", SimpleNamespace(isatty=lambda: False)):
            with self.assertRaisesRegex(ManagerError, "--backup oder --no-backup"):
                _mods_command(manager, build_parser().parse_args(["mods", "update", "home", "--yes"]))
            with self.assertRaisesRegex(ManagerError, "--yes"):
                _mods_command(manager, build_parser().parse_args(["mods", "update", "home", "--no-backup"]))
            _mods_command(
                manager,
                build_parser().parse_args(["mods", "update", "home", "--no-backup", "--yes", "--startup-timeout", "90"]),
            )
        updater.apply.assert_called_once()
        self.assertEqual(updater.apply.call_args.kwargs["startup_timeout"], 90)
        self.assertFalse(updater.apply.call_args.kwargs["full_server_backup"])

    def test_optional_port_can_be_disabled_explicitly(self):
        parser = build_parser()
        args = parser.parse_args(["create", "my-world", "--bedrock-port", "off"])
        self.assertIsNone(args.bedrock_port)

    def test_rcon_item_command_arguments(self):
        args = build_parser().parse_args(["rcon", "set-item", "home", "Alex", "5", "diamond", "3", "--yes"])
        self.assertEqual(args.command, "rcon")
        self.assertEqual(args.rcon_action, "set-item")
        self.assertEqual((args.server, args.player, args.slot, args.item, args.count), ("home", "Alex", 5, "diamond", 3))
        self.assertTrue(args.yes)

    def test_help_parser_has_expected_commands(self):
        parser = build_parser()
        choices = parser._subparsers._group_actions[0].choices
        self.assertIn("create", choices)
        self.assertIn("configure", choices)
        self.assertIn("rcon", choices)
        self.assertIn("catalog", choices)
        self.assertIn("mods", choices)
        self.assertIn("resource-pack", choices)
        self.assertIn("geyser", choices)
        self.assertIn("world", choices)
        self.assertIn("start", choices)
        self.assertIn("autostart", choices)


if __name__ == "__main__":
    unittest.main()
