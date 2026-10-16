import tempfile
import unittest
from pathlib import Path

from minecraft_server_creator.config import AppConfig, ConfigError, ConfigStore
from minecraft_server_creator.models import PortSettings, ServerProfile


class ConfigStoreTests(unittest.TestCase):
    def test_load_defaults_and_save_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json")
            config = store.load()
            self.assertFalse(store.exists)
            config.servers_root = str(Path(temp) / "servers")
            config.port_defaults = PortSettings(25566, 19133, None)
            config.profiles.append(
                ServerProfile("id1", "Test", "test", "vanilla", "1.21.4", 21, str(Path(temp) / "servers" / "test"))
            )
            store.save(config)
            loaded = store.load()
            self.assertTrue(store.exists)
            self.assertEqual(loaded.port_defaults.java_tcp, 25566)
            self.assertEqual(loaded.profiles[0].name, "Test")
            self.assertEqual(loaded.find_profile("test").id, "id1")

    def test_unknown_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text('{"schema_version": 99}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                ConfigStore(path).load()


if __name__ == "__main__":
    unittest.main()
