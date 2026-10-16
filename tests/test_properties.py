import tempfile
import unittest
from pathlib import Path

from minecraft_server_creator.properties import PropertiesError, read_properties, update_properties


class PropertiesTests(unittest.TestCase):
    def test_update_preserves_comments_and_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "server.properties"
            path.write_text("# keep me\nserver-port=25565\nmotd=old\n", encoding="utf-8")
            update_properties(path, {"server-port": 25570, "online-mode": True})
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep me", text)
            self.assertIn("server-port=25570", text)
            self.assertIn("motd=old", text)
            self.assertIn("online-mode=True", text)
            self.assertEqual(read_properties(path)["server-port"], "25570")

    def test_rejects_newlines(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(PropertiesError):
                update_properties(Path(temp) / "server.properties", {"motd": "hello\nserver-port=1"})


if __name__ == "__main__":
    unittest.main()
