import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_server_creator.minecraft import MinecraftMetadataClient, download_file


class MinecraftMetadataTests(unittest.TestCase):
    def test_latest_stable_and_version_resolution(self):
        manifest = {
            "latest": {"release": "1.21.4", "snapshot": "25w01a"},
            "versions": [
                {"id": "1.21.4", "type": "release", "url": "https://example.invalid/release.json"},
                {"id": "25w01a", "type": "snapshot", "url": "https://example.invalid/snapshot.json"},
            ],
        }
        with patch("minecraft_server_creator.minecraft.fetch_json", return_value=manifest):
            client = MinecraftMetadataClient()
            self.assertEqual(client.resolve_version("latest"), "1.21.4")
            self.assertEqual(client.resolve_version("25w01a"), "25w01a")
            self.assertEqual(client.known_versions(), ["1.21.4"])

    def test_download_verifies_checksum(self):
        payload = b"minecraft-server-jar"
        digest = hashlib.sha1(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "server.jar"
            with patch("minecraft_server_creator.minecraft.urlopen", return_value=io.BytesIO(payload)):
                download_file("https://example.invalid/server.jar", target, checksum=digest, algorithm="sha1")
            self.assertEqual(target.read_bytes(), payload)

    def test_download_rejects_bad_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "server.jar"
            with patch("minecraft_server_creator.minecraft.urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaises(Exception):
                    download_file("https://example.invalid/server.jar", target, checksum="bad", algorithm="sha1")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
