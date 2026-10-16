import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import call, patch

from minecraft_server_creator.loaders import (
    ARCLIGHT_RELEASES_URL,
    FABRIC_META_URL,
    FORGE_MAVEN_BASE,
    FORGE_PROMOTIONS_URL,
    NEOFORGE_MAVEN_BASE,
    NEOFORGE_METADATA_URL,
    ArclightInstaller,
    FabricInstaller,
    ForgeInstaller,
    LoaderInstallError,
    LoaderInstallResult,
    NeoForgeInstaller,
)


def _write_fake_download(_url, destination, **_kwargs):
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake installer")
    return target


def _write_fake_jar(url, destination, **_kwargs):
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\\n")
    return target


def _fake_installer_process(target: str):
    def run(args, *, cwd, **_kwargs):
        launch_file = Path(cwd) / target
        launch_file.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text("-cp fake Main", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="installed", stderr="")

    return run


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
            self.assertEqual(result, LoaderInstallResult("0.18.1", "jar", "server.jar"))
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


class ForgeInstallerTests(unittest.TestCase):
    def test_downloads_latest_stable_official_installer_and_builds_argument_launcher(self):
        minecraft_version = "1.21.4"
        loader_version = "54.1.0"
        coordinate = f"{minecraft_version}-{loader_version}"
        artifact = f"forge-{coordinate}-installer.jar"
        installer_url = f"{FORGE_MAVEN_BASE}/{coordinate}/{artifact}"
        launch_target = f"libraries/net/minecraftforge/forge/{coordinate}/unix_args.txt"
        promotions = {"promos": {f"{minecraft_version}-latest": loader_version}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("minecraft_server_creator.loaders._fetch_official_json", return_value=promotions) as fetch_json:
                with patch("minecraft_server_creator.loaders._fetch_official_text", return_value="a" * 40) as fetch_text:
                    with patch("minecraft_server_creator.loaders.download_file", side_effect=_write_fake_download) as download:
                        with patch(
                            "minecraft_server_creator.loaders.subprocess.run",
                            side_effect=_fake_installer_process(launch_target),
                        ) as run_installer:
                            result = ForgeInstaller().install(
                                minecraft_version,
                                {"id": minecraft_version},
                                root,
                                "/managed/java",
                            )
            self.assertEqual(result, LoaderInstallResult(loader_version, "args", launch_target))
            fetch_json.assert_called_once_with(FORGE_PROMOTIONS_URL, "files.minecraftforge.net")
            fetch_text.assert_called_once_with(f"{installer_url}.sha1", "maven.minecraftforge.net")
            download.assert_called_once_with(
                installer_url,
                root / f".{artifact}",
                checksum="a" * 40,
                algorithm="sha1",
                timeout=180.0,
            )
            self.assertEqual(run_installer.call_args.args[0], ["/managed/java", "-jar", str(root / f".{artifact}"), "--installServer"])
            self.assertFalse((root / f".{artifact}").exists())
            metadata = json.loads((root / "forge-loader.json").read_text())
            self.assertEqual(metadata["loader_version"], loader_version)
            self.assertEqual(metadata["launch_target"], launch_target)

    def test_falls_back_to_recommended_stable_build(self):
        with tempfile.TemporaryDirectory() as temp:
            promotions = {"promos": {"1.20.1-latest": "not-stable", "1.20.1-recommended": "47.4.0"}}
            launch_target = "libraries/net/minecraftforge/forge/1.20.1-47.4.0/unix_args.txt"
            with patch("minecraft_server_creator.loaders._fetch_official_json", return_value=promotions):
                with patch("minecraft_server_creator.loaders._fetch_official_text", return_value="b" * 40):
                    with patch("minecraft_server_creator.loaders.download_file", side_effect=_write_fake_download):
                        with patch(
                            "minecraft_server_creator.loaders.subprocess.run",
                            side_effect=_fake_installer_process(launch_target),
                        ):
                            result = ForgeInstaller().install("1.20.1", {"id": "1.20.1"}, temp, "/java")
            self.assertEqual(result.loader_version, "47.4.0")

    def test_rejects_missing_stable_forge_build(self):
        with patch(
            "minecraft_server_creator.loaders._fetch_official_json",
            return_value={"promos": {"1.21.1-latest": "beta-1", "1.21.1-recommended": ""}},
        ):
            with self.assertRaisesRegex(LoaderInstallError, "keinen stabilen Installer"):
                ForgeInstaller().install("1.21.1", {"id": "1.21.1"}, tempfile.gettempdir(), "/java")

    def test_rejects_forge_snapshots(self):
        with self.assertRaisesRegex(LoaderInstallError, "numerische Minecraft-Versionen"):
            ForgeInstaller().install("1.21.1-pre1", {"id": "1.21.1-pre1"}, tempfile.gettempdir(), "/java")


class NeoForgeInstallerTests(unittest.TestCase):
    def test_game_version_prefixes(self):
        self.assertEqual(NeoForgeInstaller.minecraft_prefix("1.21.4"), "21.4.")
        self.assertEqual(NeoForgeInstaller.minecraft_prefix("1.21"), "21.0.")
        self.assertEqual(NeoForgeInstaller.minecraft_prefix("26.1"), "26.1.")

    def test_selects_latest_stable_compatible_maven_version(self):
        metadata = """<metadata><versioning><versions>
          <version>21.1.9</version><version>21.1.12</version><version>21.1.100</version>
          <version>21.1.101-beta</version><version>21.2.1</version><version>20.4.8</version>
        </versions></versioning></metadata>"""
        loader_version = "21.1.100"
        artifact = f"neoforge-{loader_version}-installer.jar"
        installer_url = f"{NEOFORGE_MAVEN_BASE}/{loader_version}/{artifact}"
        launch_target = f"libraries/net/neoforged/neoforge/{loader_version}/unix_args.txt"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "minecraft_server_creator.loaders._fetch_official_text",
                side_effect=[metadata, "c" * 40],
            ) as fetch_text:
                with patch("minecraft_server_creator.loaders.download_file", side_effect=_write_fake_download) as download:
                    with patch(
                        "minecraft_server_creator.loaders.subprocess.run",
                        side_effect=_fake_installer_process(launch_target),
                    ) as run_installer:
                        result = NeoForgeInstaller().install("1.21.1", {"id": "1.21.1"}, root, "/managed/java")
            self.assertEqual(result, LoaderInstallResult(loader_version, "args", launch_target))
            self.assertEqual(
                fetch_text.call_args_list,
                [
                    call(NEOFORGE_METADATA_URL, "maven.neoforged.net", accept="application/xml"),
                    call(f"{installer_url}.sha1", "maven.neoforged.net"),
                ],
            )
            download.assert_called_once_with(
                installer_url,
                root / f".{artifact}",
                checksum="c" * 40,
                algorithm="sha1",
                timeout=180.0,
            )
            self.assertEqual(run_installer.call_args.args[0][0:2], ["/managed/java", "-jar"])
            self.assertTrue((root / launch_target).is_file())
            saved = json.loads((root / "neoforge-loader.json").read_text())
            self.assertEqual(saved["loader_version"], loader_version)

    def test_rejects_missing_stable_compatible_build(self):
        metadata = "<metadata><versioning><versions><version>21.2.1</version><version>21.1.2-beta</version></versions></versioning></metadata>"
        with patch("minecraft_server_creator.loaders._fetch_official_text", return_value=metadata):
            with self.assertRaisesRegex(LoaderInstallError, "keinen stabilen Installer"):
                NeoForgeInstaller().install("1.21.1", {"id": "1.21.1"}, tempfile.gettempdir(), "/java")

    def test_rejects_untrusted_metadata_hosts(self):
        with patch("minecraft_server_creator.loaders.urlopen") as open_url:
            from minecraft_server_creator.loaders import _fetch_official_text

            with self.assertRaisesRegex(LoaderInstallError, "maven.neoforged.net"):
                _fetch_official_text("https://evil.example/metadata.xml", "maven.neoforged.net")
            open_url.assert_not_called()


class ArclightInstallerTests(unittest.TestCase):
    def test_selects_family_specific_stable_release_and_verifies_sha256(self):
        checksum = "d" * 64
        release = {
            "tag_name": "FeudalKings/1.0.1",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "arclight-fabric-1.21.1-1.0.1-8ec9529.jar",
                    "browser_download_url": "https://github.com/IzzelAliz/Arclight/releases/download/FeudalKings/1.0.1/arclight-fabric-1.21.1-1.0.1-8ec9529.jar",
                    "digest": f"sha256:{checksum}",
                },
                {
                    "name": "arclight-forge-1.21.1-1.0.1-8ec9529.jar",
                    "browser_download_url": "https://github.com/IzzelAliz/Arclight/releases/download/FeudalKings/1.0.1/arclight-forge-1.21.1-1.0.1-8ec9529.jar",
                    "digest": f"sha256:{checksum}",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            server_dir = Path(temp)
            with patch("minecraft_server_creator.loaders._fetch_official_json", return_value=[release]) as fetch:
                with patch("minecraft_server_creator.loaders.download_file", side_effect=_write_fake_jar) as download:
                    result = ArclightInstaller().install(
                        "1.21.1", {"id": "1.21.1"}, server_dir, "forge"
                    )
            self.assertEqual(result, LoaderInstallResult("1.0.1-8ec9529", "jar", "server.jar", "forge"))
            fetch.assert_called_once_with(f"{ARCLIGHT_RELEASES_URL}?per_page=100&page=1", "api.github.com")
            download.assert_called_once_with(
                release["assets"][1]["browser_download_url"],
                server_dir / "server.jar",
                checksum=checksum,
                algorithm="sha256",
                timeout=180.0,
            )
            saved = json.loads((server_dir / "arclight-loader.json").read_text())
            self.assertEqual(saved["loader_family"], "forge")
            self.assertTrue((server_dir / "server.jar").is_file())

    def test_skips_prereleases_and_rejects_unsupported_builds(self):
        snapshot = {
            "tag_name": "1.0.0-SNAPSHOT",
            "draft": False,
            "prerelease": True,
            "assets": [
                {
                    "name": "arclight-fabric-1.21.1-1.0.0-SNAPSHOT.jar",
                    "browser_download_url": "https://github.com/IzzelAliz/Arclight/releases/download/snapshot.jar",
                }
            ],
        }
        with patch(
            "minecraft_server_creator.loaders._fetch_official_json",
            side_effect=[[snapshot], []],
        ):
            with self.assertRaisesRegex(LoaderInstallError, "keinen stabilen fabric-Build"):
                ArclightInstaller().install("1.21.1", {"id": "1.21.1"}, tempfile.gettempdir(), "fabric")

    def test_rejects_untrusted_asset_host(self):
        release = {
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "arclight-neoforge-1.21.1-1.0.1.jar",
                    "browser_download_url": "https://evil.example/arclight.jar",
                }
            ],
        }
        with patch(
            "minecraft_server_creator.loaders._fetch_official_json",
            side_effect=[[release], []],
        ):
            with self.assertRaisesRegex(LoaderInstallError, "keinen stabilen neoforge-Build"):
                ArclightInstaller().install("1.21.1", {"id": "1.21.1"}, tempfile.gettempdir(), "neoforge")


if __name__ == "__main__":
    unittest.main()
