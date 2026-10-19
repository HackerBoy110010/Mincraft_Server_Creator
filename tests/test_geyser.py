from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from minecraft_server_creator.catalog import InstallPlan, NoCompatibleVersionError
from minecraft_server_creator.geyser import (
    FABRIC_API_PROJECT_ID,
    GEYSER_PROJECT_ID,
    GeyserBetaOptInRequired,
    GeyserError,
    GeyserInstaller,
)
from minecraft_server_creator.models import PortSettings, ServerProfile


class FakeCatalog:
    def __init__(self):
        self.records = []
        self.calls = []
        self.installs = []

    def prepare_install(self, source, kind, project_id, profile, *, include_beta=False):
        self.calls.append((source, kind, project_id, include_beta))
        if project_id == GEYSER_PROJECT_ID and not include_beta:
            raise NoCompatibleVersionError("no stable Geyser build")
        is_geyser = project_id == GEYSER_PROJECT_ID
        filename = "Geyser-Fabric-2.12.0-b1265.jar" if is_geyser else "fabric-api-0.164.0+26.3.jar"
        return InstallPlan(
            "modrinth",
            "mod",
            project_id,
            "geyser" if is_geyser else "fabric-api",
            "Geyser" if is_geyser else "Fabric API",
            f"https://modrinth.com/mod/{'geyser' if is_geyser else 'fabric-api'}",
            filename,
            "2.12.0-b1265" if is_geyser else "0.164.0+26.3",
            f"https://cdn.modrinth.com/data/{project_id}/{filename}",
            "a" * 128,
            "sha512",
            (FABRIC_API_PROJECT_ID,) if is_geyser else (),
            profile.minecraft_version,
            "beta" if is_geyser else "release",
        )

    def installed(self, _profile):
        return list(self.records)

    def install(self, plan, profile, *, allow_beta=False):
        self.installs.append((plan.project_id, allow_beta))
        path = Path(profile.server_dir) / "mods" / plan.file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock mod")
        self.records.append(
            {
                "kind": "mod",
                "project_id": plan.project_id,
                "path": f"mods/{plan.file_name}",
                "minecraft_version": profile.minecraft_version,
                "file_version": plan.file_version,
                "release_channel": plan.release_channel,
            }
        )
        return path


def _profile(root: Path, *, server_type: str = "fabric", bedrock_port: int | None = 19132) -> ServerProfile:
    server_dir = root / "server"
    for folder in ("mods", "config"):
        (server_dir / folder).mkdir(parents=True, exist_ok=True)
    return ServerProfile(
        id="profile1",
        name="Geyser test",
        slug="geyser-test",
        server_type=server_type,
        minecraft_version="26.3",
        java_major=25,
        server_dir=str(server_dir),
        ports=PortSettings(java_tcp=25565, bedrock_udp=bedrock_port, voice_udp=24454),
    )


class GeyserInstallerTests(unittest.TestCase):
    def test_beta_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with self.assertRaises(GeyserBetaOptInRequired):
                GeyserInstaller(FakeCatalog()).prepare(profile)

    def test_installs_latest_geyser_beta_and_stable_fabric_api_then_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            catalog = FakeCatalog()
            installer = GeyserInstaller(catalog)
            plan = installer.prepare(profile, allow_beta=True)
            self.assertEqual(plan.geyser.version, "2.12.0-b1265")
            self.assertEqual(plan.geyser.release_channel, "beta")
            self.assertEqual(plan.fabric_api.version, "0.164.0+26.3")
            self.assertEqual(
                catalog.calls,
                [
                    ("modrinth", "mod", GEYSER_PROJECT_ID, False),
                    ("modrinth", "mod", GEYSER_PROJECT_ID, True),
                    ("modrinth", "mod", FABRIC_API_PROJECT_ID, False),
                ],
            )

            result = installer.install(profile, plan)
            self.assertTrue(result.geyser_path.is_file())
            self.assertTrue(result.fabric_api_path.is_file())
            self.assertEqual(result.geyser_release_channel, "beta")
            self.assertEqual(catalog.installs, [(FABRIC_API_PROJECT_ID, False), (GEYSER_PROJECT_ID, True)])

            repeated = installer.prepare(profile)
            self.assertTrue(repeated.geyser.already_installed)
            self.assertTrue(repeated.fabric_api.already_installed)
            repeated_result = installer.install(profile, repeated)
            self.assertTrue(repeated_result.geyser_already_installed)
            self.assertTrue(repeated_result.fabric_api_already_installed)
            self.assertEqual(len(catalog.installs), 2)

    def test_rejects_non_fabric_profile_and_disabled_bedrock_port(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(GeyserError, "reinen Fabric"):
                GeyserInstaller(FakeCatalog()).prepare(_profile(Path(temp), server_type="vanilla"))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(GeyserError, "Bedrock-Port"):
                GeyserInstaller(FakeCatalog()).prepare(_profile(Path(temp), bedrock_port=None))

    def test_rejects_port_collision_with_java_tcp(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp), bedrock_port=25565)
            with self.assertRaisesRegex(GeyserError, "Java-TCP-Port"):
                GeyserInstaller(FakeCatalog()).prepare(profile)

    def test_configure_updates_bedrock_port_and_nethernet_without_touching_other_sections(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp), bedrock_port=19133)
            config_path = Path(profile.server_dir) / "config" / "Geyser-Fabric" / "config.yml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "bedrock:\n  address: 0.0.0.0\n  port: 19132 # old port\n  transport: raknet\n"
                "java:\n  address: 127.0.0.1\n",
                encoding="utf-8",
            )
            result = GeyserInstaller(FakeCatalog()).configure(profile)
            self.assertEqual(result, config_path)
            contents = config_path.read_text(encoding="utf-8")
            self.assertIn("  port: 19133  # old port", contents)
            self.assertIn("  transport: nethernet", contents)
            self.assertIn("java:\n  address: 127.0.0.1", contents)

    def test_configure_adds_missing_settings_and_rejects_duplicate_bedrock_sections(self):
        text = "bedrock:\n  address: 0.0.0.0\njava:\n  port: 25565\n"
        result = GeyserInstaller._patch_bedrock_settings(text, 19132)
        self.assertIn("bedrock:\n  port: 19132\n  transport: nethernet\n  address: 0.0.0.0", result)
        self.assertEqual(
            GeyserInstaller._patch_bedrock_settings("bedrock:", 19132),
            "bedrock:\n  port: 19132\n  transport: nethernet\n",
        )
        with self.assertRaisesRegex(GeyserError, "genau einen"):
            GeyserInstaller._patch_bedrock_settings("bedrock:\n  port: 1\nbedrock:\n  port: 2\n", 19132)


if __name__ == "__main__":
    unittest.main()
