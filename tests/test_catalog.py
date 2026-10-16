from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from minecraft_server_creator.catalog import CatalogError, ContentCatalog, InstallPlan
from minecraft_server_creator.models import ServerProfile


def _profile(root: Path, server_type: str = "fabric", loader_family: str | None = None) -> ServerProfile:
    server_dir = root / "server"
    for folder in ("mods", "plugins", "config"):
        (server_dir / folder).mkdir(parents=True, exist_ok=True)
    return ServerProfile(
        id="profile1",
        name="Catalog test",
        slug="catalog-test",
        server_type=server_type,
        minecraft_version="1.21.1",
        java_major=21,
        server_dir=str(server_dir),
        loader_version="1.0.0" if server_type == "arclight" else None,
        loader_family=loader_family,
    )


def _write_zip(url, destination, **kwargs):
    target = Path(destination)
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    return target


def _write_datapack_zip(url, destination, **kwargs):
    target = Path(destination)
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 48, "description": "Test pack"}}))
        archive.writestr("data/example/function/test.mcfunction", "say hello\n")
    return target


class ContentCatalogTests(unittest.TestCase):
    def test_modrinth_search_is_scoped_to_type_version_and_loader(self):
        response = {
            "hits": [
                {
                    "project_id": "abc123",
                    "slug": "example-mod",
                    "title": "Example Mod",
                    "description": "A server-side mod",
                    "downloads": 10,
                    "project_type": "mod",
                    "environment": ["client_and_server"],
                },
                {
                    "project_id": "client-only",
                    "slug": "client-only",
                    "title": "Client only",
                    "description": "Not server safe",
                    "downloads": 5,
                    "project_type": "mod",
                    "environment": ["client_only"],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with patch("minecraft_server_creator.catalog._request_json", return_value=response) as request:
                projects = ContentCatalog().search("modrinth", "mod", "example", profile)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].project_id, "abc123")
        self.assertEqual(projects[0].url, "https://modrinth.com/mod/example-mod")
        url = request.call_args.args[0]
        params = parse_qs(urlparse(url).query)
        self.assertEqual(json.loads(params["facets"][0]), [["all_project_types:mod"], ["versions:1.21.1"], ["categories:fabric"]])
        self.assertEqual(request.call_args.args[1], "api.modrinth.com")

    def test_modrinth_datapack_search_and_install_use_world_directory_and_zip_metadata(self):
        search_response = {
            "hits": [
                {
                    "project_id": "dp123",
                    "slug": "example-pack",
                    "title": "Example Pack",
                    "description": "A world datapack",
                    "downloads": 42,
                    "project_type": "datapack",
                    "all_project_types": ["datapack"],
                }
            ]
        }
        project = {"id": "dp123", "slug": "example-pack", "title": "Example Pack", "project_type": "datapack"}
        checksum = "e" * 128
        versions = [
            {
                "id": "pack-release",
                "version_number": "1.0.0",
                "version_type": "release",
                "status": "listed",
                "loaders": ["datapack"],
                "game_versions": ["1.21.1"],
                "files": [
                    {
                        "filename": "example-pack.zip",
                        "primary": True,
                        "url": "https://cdn.modrinth.com/data/dp123/example-pack.zip",
                        "hashes": {"sha512": checksum},
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            (Path(profile.server_dir) / "server.properties").write_text("level-name=adventure_world\n", encoding="utf-8")
            catalog = ContentCatalog()
            with patch("minecraft_server_creator.catalog._request_json", return_value=search_response) as request:
                projects = catalog.search("modrinth", "datapack", "example", profile)
            self.assertEqual(projects[0].kind, "datapack")
            self.assertEqual(projects[0].url, "https://modrinth.com/datapack/example-pack")
            facets = json.loads(parse_qs(urlparse(request.call_args.args[0]).query)["facets"][0])
            self.assertEqual(facets, [["all_project_types:datapack"], ["versions:1.21.1"], ["categories:datapack"]])

            with patch("minecraft_server_creator.catalog._request_json", side_effect=[project, versions]):
                plan = catalog.prepare_install("modrinth", "datapack", "dp123", profile)
            self.assertEqual(plan.file_name, "example-pack.zip")
            self.assertEqual(catalog.destination(profile, "datapack"), "adventure_world/datapacks")
            with patch("minecraft_server_creator.catalog.download_file", side_effect=_write_datapack_zip):
                installed = catalog.install(plan, profile)
            self.assertEqual(
                installed,
                Path(profile.server_dir).resolve() / "adventure_world" / "datapacks" / "example-pack.zip",
            )
            self.assertEqual(catalog.installed(profile)[0]["path"], "adventure_world/datapacks/example-pack.zip")

    def test_modrinth_plugin_search_uses_all_project_types_facet(self):
        response = {
            "hits": [
                {
                    "project_id": "plg123",
                    "slug": "example-plugin",
                    "title": "Example Plugin",
                    "project_type": "mod",
                    "all_project_types": ["mod", "plugin"],
                    "downloads": 20,
                    "environment": ["server_only"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp), server_type="arclight", loader_family="forge")
            with patch("minecraft_server_creator.catalog._request_json", return_value=response) as request:
                projects = ContentCatalog().search("modrinth", "plugin", "example", profile)
        self.assertEqual(projects[0].kind, "plugin")
        self.assertEqual(projects[0].url, "https://modrinth.com/plugin/example-plugin")
        facets = json.loads(parse_qs(urlparse(request.call_args.args[0]).query)["facets"][0])
        self.assertIn(["all_project_types:plugin"], facets)
        self.assertIn(["categories:bukkit", "categories:spigot"], facets)

    def test_modrinth_install_selects_latest_server_compatible_release_and_verifies_hash(self):
        checksum = "a" * 128
        project = {"id": "abc123", "slug": "example-mod", "title": "Example Mod", "project_type": "mod"}
        versions = [
            {
                "id": "alpha1",
                "version_type": "alpha",
                "environment": "client_and_server",
                "loaders": ["fabric"],
                "game_versions": ["1.21.1"],
                "files": [],
            },
            {
                "id": "release2",
                "version_number": "2.0.0",
                "version_type": "release",
                "status": "listed",
                "environment": "client_and_server",
                "loaders": ["fabric"],
                "game_versions": ["1.21.1"],
                "dependencies": [{"dependency_type": "required", "project_id": "dependency1"}],
                "files": [
                    {
                        "filename": "example-mod-2.0.0.jar",
                        "primary": True,
                        "url": "https://cdn.modrinth.com/data/abc123/example-mod-2.0.0.jar",
                        "hashes": {"sha512": checksum},
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            catalog = ContentCatalog()
            with patch("minecraft_server_creator.catalog._request_json", side_effect=[project, versions]):
                plan = catalog.prepare_install("modrinth", "mod", "abc123", profile)
            self.assertEqual(plan.file_version, "2.0.0")
            self.assertEqual(plan.required_dependencies, ("dependency1",))
            with patch("minecraft_server_creator.catalog.download_file", side_effect=_write_zip) as download:
                installed = catalog.install(plan, profile)
            self.assertEqual(installed, Path(profile.server_dir).resolve() / "mods" / "example-mod-2.0.0.jar")
            download.assert_called_once_with(
                plan.download_url,
                installed,
                checksum=checksum,
                algorithm="sha512",
                timeout=180.0,
            )
            manifest = catalog.installed(profile)
            self.assertEqual(manifest[0]["project_id"], "abc123")
            self.assertEqual(manifest[0]["required_dependencies"], ["dependency1"])

    def test_modrinth_accepts_plugin_loader_versions_for_mixed_project_type(self):
        project = {"id": "plg123", "slug": "example-plugin", "title": "Example Plugin", "project_type": "mod"}
        versions = [
            {
                "id": "plugin-release",
                "version_number": "1.2.0",
                "version_type": "release",
                "environment": "server_only",
                "loaders": ["spigot"],
                "game_versions": ["1.21.1"],
                "files": [
                    {
                        "filename": "example-plugin.jar",
                        "primary": True,
                        "url": "https://cdn.modrinth.com/data/plg123/example-plugin.jar",
                        "hashes": {"sha1": "a" * 40},
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp), server_type="arclight", loader_family="forge")
            with patch("minecraft_server_creator.catalog._request_json", side_effect=[project, versions]):
                plan = ContentCatalog().prepare_install("modrinth", "plugin", "plg123", profile)
        self.assertEqual(plan.kind, "plugin")
        self.assertEqual(plan.algorithm, "sha1")

    def test_modrinth_rejects_client_only_versions(self):
        project = {"id": "abc123", "slug": "client-only", "project_type": "mod"}
        versions = [
            {
                "version_type": "release",
                "environment": "client_only",
                "loaders": ["fabric"],
                "game_versions": ["1.21.1"],
                "files": [],
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with patch("minecraft_server_creator.catalog._request_json", side_effect=[project, versions]):
                with self.assertRaisesRegex(CatalogError, "serverfähige"):
                    ContentCatalog().prepare_install("modrinth", "mod", "abc123", profile)

    def test_mods_and_plugins_are_limited_to_compatible_server_types(self):
        with tempfile.TemporaryDirectory() as temp:
            vanilla = _profile(Path(temp), server_type="vanilla")
            with self.assertRaisesRegex(CatalogError, "Vanilla unterstützt keine Loader-Mods"):
                ContentCatalog().search("modrinth", "mod", "map", vanilla)
        with tempfile.TemporaryDirectory() as temp:
            fabric = _profile(Path(temp), server_type="fabric")
            with self.assertRaisesRegex(CatalogError, "nur auf Arclight"):
                ContentCatalog().search("modrinth", "plugin", "plugin", fabric)

    def test_curseforge_requires_api_key_only_for_its_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with self.assertRaisesRegex(CatalogError, "CURSEFORGE_API_KEY"):
                ContentCatalog(curseforge_api_key="").search("curseforge", "mod", "map", profile)
            self.assertEqual(ContentCatalog()._provider("modrinth").__class__.__name__, "ModrinthCatalog")

    def test_curseforge_search_uses_compatible_loader_and_api_key_header(self):
        response = {"data": [{"id": 222, "name": "Example", "slug": "example", "classId": 6}]}
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp), server_type="arclight", loader_family="neoforge")
            with patch("minecraft_server_creator.catalog._request_json", return_value=response) as request:
                projects = ContentCatalog(curseforge_api_key="test-secret").search(
                    "curseforge", "mod", "example", profile
                )
        self.assertEqual(projects[0].project_id, "222")
        params = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(params["classId"], ["6"])
        self.assertEqual(params["gameVersion"], ["1.21.1"])
        self.assertEqual(params["modLoaderType"], ["6"])
        self.assertEqual(request.call_args.kwargs["api_key"], "test-secret")

    def test_curseforge_datapack_category_is_discovered_from_api(self):
        category_response = {
            "data": [{"id": 8123, "name": "Data Packs", "slug": "data-packs", "isClass": False, "classId": 9876}]
        }
        search_response = {
            "data": [
                {
                    "id": 555,
                    "name": "Example Pack",
                    "slug": "example-pack",
                    "classId": 9876,
                    "categories": [{"id": 8123, "name": "Data Packs"}],
                }
            ]
        }
        project_response = {
            "data": {
                "id": 555,
                "name": "Example Pack",
                "slug": "example-pack",
                "classId": 9876,
                "categories": [{"id": 8123, "name": "Data Packs"}],
            }
        }
        files_response = {
            "data": [
                {
                    "id": 501,
                    "fileName": "example-pack.zip",
                    "displayName": "Example release",
                    "fileDate": "2026-09-10T00:00:00Z",
                    "releaseType": 1,
                    "isAvailable": True,
                    "gameVersions": ["1.21.1"],
                    "downloadUrl": "https://edge.forgecdn.net/files/50/1/example-pack.zip",
                    "hashes": [{"algo": 1, "value": "f" * 40}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            catalog = ContentCatalog(curseforge_api_key="secret")
            with patch(
                "minecraft_server_creator.catalog._request_json",
                side_effect=[category_response, search_response],
            ) as request:
                projects = catalog.search("curseforge", "datapack", "example", profile)
            self.assertEqual(projects[0].url, "https://www.curseforge.com/minecraft/data-packs/example-pack")
            params = parse_qs(urlparse(request.call_args.args[0]).query)
            self.assertEqual(params["classId"], ["9876"])
            self.assertEqual(params["categoryId"], ["8123"])
            self.assertNotIn("modLoaderType", params)

            with patch(
                "minecraft_server_creator.catalog._request_json",
                side_effect=[category_response, project_response, files_response],
            ) as request:
                plan = catalog.prepare_install("curseforge", "datapack", "555", profile)
            self.assertEqual(plan.file_name, "example-pack.zip")
            self.assertEqual(plan.project_url, "https://www.curseforge.com/minecraft/data-packs/example-pack")
            with patch("minecraft_server_creator.catalog.download_file", side_effect=_write_datapack_zip):
                installed = catalog.install(plan, profile)
            self.assertEqual(installed, Path(profile.server_dir).resolve() / "world" / "datapacks" / "example-pack.zip")
            self.assertEqual(request.call_count, 3)
            self.assertIn("/categories?", request.call_args_list[0].args[0])
            self.assertIn("/mods/555/files?", request.call_args_list[2].args[0])

    def test_curseforge_install_chooses_latest_stable_file_and_sha1(self):
        project = {"data": {"id": 222, "name": "Example", "slug": "example", "classId": 6}}
        files = {
            "data": [
                {
                    "id": 2,
                    "fileName": "example-old.jar",
                    "displayName": "Older release",
                    "fileDate": "2026-01-01T00:00:00Z",
                    "releaseType": 1,
                    "isAvailable": True,
                    "gameVersions": ["1.21.1"],
                    "downloadUrl": "https://edge.forgecdn.net/files/2/example-old.jar",
                    "hashes": [{"algo": 1, "value": "b" * 40}],
                },
                {
                    "id": 3,
                    "fileName": "example-latest.jar",
                    "displayName": "Latest release",
                    "fileDate": "2026-09-01T00:00:00Z",
                    "releaseType": 1,
                    "isAvailable": True,
                    "gameVersions": ["1.21.1"],
                    "downloadUrl": "https://edge.forgecdn.net/files/3/example-latest.jar",
                    "hashes": [{"algo": 2, "value": "c" * 32}, {"algo": 1, "value": "d" * 40}],
                    "dependencies": [{"modId": 999, "relationType": 3}],
                },
                {
                    "id": 4,
                    "fileName": "example-beta.jar",
                    "displayName": "Beta",
                    "fileDate": "2026-10-01T00:00:00Z",
                    "releaseType": 2,
                    "isAvailable": True,
                    "gameVersions": ["1.21.1"],
                    "downloadUrl": "https://edge.forgecdn.net/files/4/example-beta.jar",
                    "hashes": [{"algo": 1, "value": "e" * 40}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp), server_type="forge")
            with patch("minecraft_server_creator.catalog._request_json", side_effect=[project, files]) as request:
                plan = ContentCatalog(curseforge_api_key="secret").prepare_install(
                    "curseforge", "mod", "222", profile
                )
        self.assertEqual(plan.file_name, "example-latest.jar")
        self.assertEqual(plan.algorithm, "sha1")
        self.assertEqual(plan.checksum, "d" * 40)
        self.assertEqual(plan.required_dependencies, ("999",))
        self.assertEqual(request.call_count, 2)

    def test_datapack_rejects_unsafe_world_name_before_download(self):
        plan = InstallPlan(
            "modrinth", "datapack", "id", "slug", "Pack", "https://modrinth.com/datapack/slug",
            "pack.zip", "1.0", "https://cdn.modrinth.com/pack.zip", "a" * 128, "sha512", (), "1.21.1"
        )
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            (Path(profile.server_dir) / "server.properties").write_text("level-name=../outside\n", encoding="utf-8")
            with patch("minecraft_server_creator.catalog.download_file") as download:
                with self.assertRaisesRegex(CatalogError, "sicherer einzelner Weltordnername"):
                    ContentCatalog().install(plan, profile)
            download.assert_not_called()
            self.assertFalse((Path(temp) / "outside" / "datapacks").exists())

    def test_datapack_without_root_pack_metadata_is_removed(self):
        plan = InstallPlan(
            "modrinth", "datapack", "id", "slug", "Pack", "https://modrinth.com/datapack/slug",
            "pack.zip", "1.0", "https://cdn.modrinth.com/pack.zip", "a" * 128, "sha512", (), "1.21.1"
        )
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with patch("minecraft_server_creator.catalog.download_file", side_effect=_write_zip):
                with self.assertRaisesRegex(CatalogError, "pack.mcmeta direkt im Stammverzeichnis"):
                    ContentCatalog().install(plan, profile)
            self.assertFalse((Path(profile.server_dir) / "world" / "datapacks" / "pack.zip").exists())
            self.assertEqual(ContentCatalog.installed(profile), [])

    def test_unsafe_filename_and_untrusted_download_host_are_rejected(self):
        with self.assertRaisesRegex(CatalogError, "sichere, portable Dateinamen"):
            from minecraft_server_creator.catalog import _safe_filename
            _safe_filename("../../outside.jar")
        bad_plan = InstallPlan(
            "modrinth", "mod", "id", "slug", "Project", "https://modrinth.com/mod/slug",
            "file.jar", "1.0", "https://evil.example/file.jar", "a" * 128, "sha512", (), "1.21.1"
        )
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with self.assertRaisesRegex(CatalogError, "cdn.modrinth.com"):
                ContentCatalog().install(bad_plan, profile)

    def test_existing_files_are_never_overwritten(self):
        plan = InstallPlan(
            "modrinth", "mod", "id", "slug", "Project", "https://modrinth.com/mod/slug",
            "file.jar", "1.0", "https://cdn.modrinth.com/file.jar", "a" * 128, "sha512", (), "1.21.1"
        )
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            target = Path(profile.server_dir) / "mods" / "file.jar"
            target.write_bytes(b"user file")
            with patch("minecraft_server_creator.catalog.download_file") as download:
                with self.assertRaisesRegex(CatalogError, "wird nicht überschrieben"):
                    ContentCatalog().install(plan, profile)
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
