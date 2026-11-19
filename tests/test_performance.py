from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import Mock

from minecraft_server_creator.catalog import InstallPlan, NoCompatibleVersionError
from minecraft_server_creator.models import ServerProfile
from minecraft_server_creator.performance import (
    PERFORMANCE_MODS,
    PerformancePackError,
    PreparedPerformancePackage,
    PlannedPerformanceMod,
    install_performance_package,
    prepare_performance_package,
)


def _profile(root: Path, server_type: str = "fabric") -> ServerProfile:
    server_dir = root / "server"
    (server_dir / "mods").mkdir(parents=True, exist_ok=True)
    return ServerProfile(
        id="profile1",
        name="Performance test",
        slug="performance-test",
        server_type=server_type,
        minecraft_version="1.21.1",
        java_major=21,
        server_dir=str(server_dir),
    )


def _plan(profile: ServerProfile, project_id: str, *, slug: str | None = None, title: str | None = None,
          client_side: str | None = "optional", server_side: str | None = "required",
          dependencies: tuple[str, ...] = (), dependency_ids: tuple[str, ...] = (),
          filename: str | None = None) -> InstallPlan:
    slug = slug or project_id.lower()
    return InstallPlan(
        source="modrinth",
        kind="mod",
        project_id=project_id,
        project_slug=slug,
        project_title=title or slug,
        project_url=f"https://modrinth.com/mod/{slug}",
        file_name=filename or f"{slug}.jar",
        file_version="1.0.0",
        download_url=f"https://cdn.modrinth.com/{slug}.jar",
        checksum="a" * 128,
        algorithm="sha512",
        required_dependencies=dependencies,
        minecraft_version=profile.minecraft_version,
        release_channel="release",
        client_side=client_side,
        server_side=server_side,
        required_dependency_project_ids=dependency_ids,
    )


class PerformancePackTests(unittest.TestCase):
    def test_prepare_aborts_entire_package_if_any_mod_has_no_stable_release(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            plans = {
                spec.project_id: _plan(profile, spec.project_id, slug=spec.name.lower(), title=spec.name)
                for spec in PERFORMANCE_MODS
            }

            class FakeCatalog:
                @staticmethod
                def installed(_profile):
                    return []

                @staticmethod
                def prepare_install(_source, _kind, project_id, _profile):
                    if project_id == PERFORMANCE_MODS[2].project_id:
                        raise NoCompatibleVersionError("no stable release")
                    return plans[project_id]

            with self.assertRaisesRegex(PerformancePackError, "gesamte Performance-Paket wurde abgelehnt") as raised:
                prepare_performance_package(FakeCatalog(), profile)
            self.assertIn("C2ME", str(raised.exception))
            self.assertIn("no stable release", str(raised.exception))
            self.assertEqual(list((Path(profile.server_dir) / "mods").iterdir()), [])

    def test_prepare_rejects_client_required_mod_and_unsupported_server_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            plans = {
                spec.project_id: _plan(
                    profile,
                    spec.project_id,
                    slug=spec.name.lower(),
                    title=spec.name,
                    client_side="required" if spec.name == "Lithium" else "optional",
                )
                for spec in PERFORMANCE_MODS
            }

            class FakeCatalog:
                @staticmethod
                def installed(_profile):
                    return []

                @staticmethod
                def prepare_install(_source, _kind, project_id, _profile):
                    return plans[project_id]

            with self.assertRaisesRegex(PerformancePackError, "Client-Seite=required"):
                prepare_performance_package(FakeCatalog(), profile)
            self.assertEqual(list((Path(profile.server_dir) / "mods").iterdir()), [])

            forge_profile = _profile(Path(temp) / "forge", server_type="forge")
            with self.assertRaisesRegex(PerformancePackError, "reine Fabric"):
                prepare_performance_package(FakeCatalog(), forge_profile)

    def test_prepare_resolves_required_server_safe_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            direct_plans = {
                spec.project_id: _plan(
                    profile,
                    spec.project_id,
                    slug=spec.name.lower(),
                    title=spec.name,
                    dependencies=("fabric-api-id",) if spec.name == "Lithium" else (),
                    dependency_ids=("fabric-api-id",) if spec.name == "Lithium" else (),
                )
                for spec in PERFORMANCE_MODS
            }
            dependency = _plan(profile, "fabric-api-id", slug="fabric-api", title="Fabric API")

            class FakeCatalog:
                @staticmethod
                def installed(_profile):
                    return []

                @staticmethod
                def prepare_install(_source, _kind, project_id, _profile):
                    return dependency if project_id == "fabric-api-id" else direct_plans[project_id]

            prepared = prepare_performance_package(FakeCatalog(), profile)
            dependency_items = [item for item in prepared.plans if item.is_dependency]
            self.assertEqual(len(dependency_items), 1)
            self.assertEqual(dependency_items[0].plan.project_title, "Fabric API")
            main_project_ids = {
                item.plan.project_id for item in prepared.plans if not item.is_dependency
            }
            self.assertEqual(main_project_ids, {spec.project_id for spec in PERFORMANCE_MODS})

    def test_prepare_aborts_if_required_dependency_is_not_server_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            plans = {
                spec.project_id: _plan(
                    profile,
                    spec.project_id,
                    slug=spec.name.lower(),
                    title=spec.name,
                    dependencies=("unsafe-client-mod",) if spec.name == "Lithium" else (),
                    dependency_ids=("unsafe-client-mod",) if spec.name == "Lithium" else (),
                )
                for spec in PERFORMANCE_MODS
            }

            class FakeCatalog:
                @staticmethod
                def installed(_profile):
                    return []

                @staticmethod
                def prepare_install(_source, _kind, project_id, _profile):
                    if project_id == "unsafe-client-mod":
                        return _plan(
                            profile,
                            project_id,
                            slug="unsafe-client-mod",
                            client_side="required",
                            server_side="optional",
                        )
                    return plans[project_id]

            with self.assertRaisesRegex(PerformancePackError, "Spieler sie möglicherweise ebenfalls benötigen"):
                prepare_performance_package(FakeCatalog(), profile)

    def test_prepare_aborts_entire_package_if_a_main_mod_is_already_installed(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            jar_path = Path(profile.server_dir) / "mods" / "lithium-local.jar"
            with zipfile.ZipFile(jar_path, "w") as archive:
                archive.writestr("fabric.mod.json", '{"id":"lithium","version":"local"}')

            class FakeCatalog:
                @staticmethod
                def installed(_profile):
                    return []

                @staticmethod
                def prepare_install(*_args):
                    raise AssertionError("No versions should be prepared after detecting a conflicting existing mod")

            with self.assertRaisesRegex(PerformancePackError, "gesamte Performance-Paket wurde abgelehnt"):
                prepare_performance_package(FakeCatalog(), profile)
            self.assertTrue(jar_path.is_file())

    def test_install_rolls_back_earlier_files_if_a_later_download_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            first = _plan(profile, "first", filename="first.jar")
            second = _plan(profile, "second", filename="second.jar")
            package = PreparedPerformancePackage(
                (PlannedPerformanceMod(first), PlannedPerformanceMod(second)), ()
            )

            class FakeCatalog:
                def install(self, plan, _profile, **_kwargs):
                    if plan.project_id == "second":
                        raise OSError("download interrupted")
                    target = Path(profile.server_dir) / "mods" / plan.file_name
                    target.write_bytes(b"installed")
                    return target

            with self.assertRaisesRegex(PerformancePackError, "zurückgerollt"):
                install_performance_package(FakeCatalog(), profile, package)
            self.assertFalse((Path(profile.server_dir) / "mods" / "first.jar").exists())
            self.assertFalse((Path(profile.server_dir) / "mods" / "second.jar").exists())
            self.assertFalse((Path(profile.server_dir) / "config" / "catalog-installed.json").exists())

    def test_install_refuses_existing_target_before_downloading(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            plan = _plan(profile, "lithium", filename="lithium.jar")
            target = Path(profile.server_dir) / "mods" / "lithium.jar"
            target.write_bytes(b"keep")
            catalog = Mock()
            package = PreparedPerformancePackage((PlannedPerformanceMod(plan),), ())
            with self.assertRaisesRegex(PerformancePackError, "existiert bereits"):
                install_performance_package(catalog, profile, package)
            catalog.install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
