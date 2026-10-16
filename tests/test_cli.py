import unittest

from minecraft_server_creator.cli import build_parser


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
        self.assertIn("start", choices)
        self.assertIn("autostart", choices)


if __name__ == "__main__":
    unittest.main()
