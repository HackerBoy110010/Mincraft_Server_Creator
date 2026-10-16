import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from minecraft_server_creator.loaders import FABRIC_META_URL, FabricInstaller, LoaderInstallError


class FabricInstallerTests(unittest.TestCase):
    def test_selects_stable_compatible_versions_and_downloads_launcher(self):
        loaders = [
            {"loader": {"version": "0.18.1", "stable": True}},
            {"loader": {"version": "0.11.9", "stable": True}},
            {"loader": {"version": "0.19.0", "stable": False}},
        ]
        installers = [
            {"version": "0.7.9", "stable": True},
            {"version": "0.8.4", "stable": True},
            {"version": "0.9.0", "stable": False},
        ]
        with tempfile.TemporaryDirectory() as temp:
            server_dir = Path(temp)
            with patch("minecraft_server_creator.loaders._fetch_json", side_effect=[loaders, installers]) as fetch:
                with patch("minecraft_server_creator.loaders.download_file") as download:
                    result = FabricInstaller().install(
                        "1.21.4", {"id": "1.21.4", "javaVersion": {"majorVersion": 21}}, server_dir
                    )
            self.assertEqual(result, server_dir / "server.jar")
            self.assertEqual(
                fetch.call_args_list,
                [
                    call(f"{FABRIC_META_URL}/loader/1.21.4"),
                    call(f"{FABRIC_META_URL}/installer"),
                ],
            )
            download.assert_called_once_with(
                f"{FABRIC_META_URL}/loader/1.21.4/0.18.1/0.8.4/server/jar",
                server_dir / "server.jar",
                timeout=180.0,
            )
            self.assertEqual(json.loads((server_dir / "minecraft-version.json").read_text())["id"], "1.21.4")
            loader_info = json.loads((server_dir / "fabric-loader.json").read_text())
            self.assertEqual(loader_info["loader_version"], "0.18.1")
            self.assertEqual(loader_info["installer_version"], "0.8.4")

    def test_encodes_explicit_snapshot_version_in_metadata_url(self):
        loader_records = [{"loader": {"version": "0.16.10", "stable": True}}]
        installer_records = [{"version": "1.0.0", "stable": True}]
        with tempfile.TemporaryDirectory() as temp:
            with patch("minecraft_server_creator.loaders._fetch_json", side_effect=[loader_records, installer_records]) as fetch:
                with patch("minecraft_server_creator.loaders.download_file"):
                    FabricInstaller().install("1.14 Pre-Release 5", {"id": "snapshot"}, temp)
            self.assertEqual(fetch.call_args_list[0], call(f"{FABRIC_META_URL}/loader/1.14%20Pre-Release%205"))

    def test_rejects_fabric_loader_older_than_unattended_install_minimum(self):
        records = [{"loader": {"version": "0.11.9", "stable": True}}]
        with patch("minecraft_server_creator.loaders._fetch_json", return_value=records):
            with self.assertRaisesRegex(LoaderInstallError, "0.12.0"):
                FabricInstaller().install("1.21.4", {"id": "1.21.4"}, tempfile.gettempdir())

    def test_metadata_source_is_restricted_to_official_https_host(self):
        with patch("minecraft_server_creator.loaders.urlopen") as open_url:
            from minecraft_server_creator.loaders import _fetch_json

            with self.assertRaisesRegex(LoaderInstallError, "meta.fabricmc.net"):
                _fetch_json("https://example.com/fabric.json")
            open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
