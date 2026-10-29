from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from minecraft_server_creator.catalog import ContentCatalog, InstallPlan
from minecraft_server_creator.models import ServerProfile
from minecraft_server_creator.mod_updates import ModUpdateError, SafeModUpdater, mod_update_lock, tcp_listener_present


def _jar_bytes(label: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        archive.writestr("mod-info.txt", label)
    return stream.getvalue()


def _profile(root: Path) -> ServerProfile:
    server_dir = root / "server"
    for folder in ("mods", "config", "logs"):
        (server_dir / folder).mkdir(parents=True, exist_ok=True)
    return ServerProfile(
        id="profile-id",
        name="Update test",
        slug="update-test",
        server_type="fabric",
        minecraft_version="1.21.1",
        java_major=21,
        server_dir=str(server_dir),
    )


def _manifest_record(project_id: str, filename: str, contents: bytes, *, version: str = "1.0") -> dict[str, object]:
    return {
        "source": "modrinth",
        "kind": "mod",
        "project_id": project_id,
        "project_slug": project_id,
        "project_title": project_id.title(),
        "project_url": f"https://modrinth.com/mod/{project_id}",
        "file_name": filename,
        "file_version": version,
        "release_channel": "release",
        "path": f"mods/{filename}",
        "minecraft_version": "1.21.1",
        "loader": "fabric",
        "checksum": hashlib.sha512(contents).hexdigest(),
        "algorithm": "sha512",
        "installed_at": "2026-01-01T00:00:00+00:00",
        "required_dependencies": [],
    }


def _write_profile_mods(profile: ServerProfile, specs: list[tuple[str, str, bytes]]) -> dict[str, bytes]:
    contents_by_id: dict[str, bytes] = {}
    records = []
    for project_id, filename, contents in specs:
        (Path(profile.server_dir) / "mods" / filename).write_bytes(contents)
        contents_by_id[project_id] = contents
        records.append(_manifest_record(project_id, filename, contents))
    manifest_path = Path(profile.server_dir) / "config" / "catalog-installed.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "files": records}, indent=2) + "\n", encoding="utf-8")
    return contents_by_id


def _plan(project_id: str, version: str, filename: str, contents: bytes) -> InstallPlan:
    return InstallPlan(
        source="modrinth",
        kind="mod",
        project_id=project_id,
        project_slug=project_id,
        project_title=project_id.title(),
        project_url=f"https://modrinth.com/mod/{project_id}",
        file_name=filename,
        file_version=version,
        download_url=f"https://cdn.modrinth.com/data/{project_id}/{filename}",
        checksum=hashlib.sha512(contents).hexdigest(),
        algorithm="sha512",
        required_dependencies=(),
        minecraft_version="1.21.1",
        release_channel="release",
    )


class FakeCatalog:
    def __init__(self, plans: dict[str, InstallPlan], contents: dict[str, bytes]):
        self.plans = plans
        self.contents = contents
        self.prepared: list[str] = []
        self.installed: list[str] = []

    def prepare_install(self, source, kind, project_id, profile, *, include_beta=False, version_id=None):
        self.prepared.append(project_id)
        return self.plans[project_id]

    def install(self, plan, profile, *, allow_beta=False, progress=None):
        self.installed.append(plan.project_id)
        content = self.contents[plan.project_id]
        if progress:
            progress(0, len(content))
            progress(len(content), len(content))
        target = Path(profile.server_dir) / "mods" / plan.file_name
        target.write_bytes(content)
        manifest_path = Path(profile.server_dir) / "config" / "catalog-installed.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(
            _manifest_record(plan.project_id, plan.file_name, self.contents[plan.project_id], version=plan.file_version)
        )
        ContentCatalog._write_manifest(manifest_path, manifest)
        return target


class FakeServices:
    def __init__(self, active: bool = False):
        self.active = active

    def is_active(self, _profile):
        return self.active

    @staticmethod
    def supported():
        return True


class FakeManager:
    def __init__(self, active: bool = False):
        self.services = FakeServices(active)
        self.starts = 0
        self.stops = 0
        self.backups = 0

    def start(self, _selector, *, confirm_linger):
        self.starts += 1
        self.services.active = True

    def stop(self, _selector):
        self.stops += 1
        self.services.active = False

    def backup(self, selector, *, progress=None):
        self.backups += 1
        if progress:
            progress("test backup progress")
        return Path(tempfile.gettempdir()) / f"{selector}-test-full-backup.tar.gz"


class SafeModUpdaterTests(unittest.TestCase):
    def _fixture(self, root: Path, *, active: bool = False):
        profile = _profile(root)
        old_alpha = _jar_bytes("alpha old")
        old_beta = _jar_bytes("beta old")
        new_alpha = _jar_bytes("alpha new")
        new_beta = _jar_bytes("beta new")
        unmanaged = _jar_bytes("manually installed; not in catalog")
        catalog_contents = _write_profile_mods(
            profile,
            [("alpha", "alpha-1.0.jar", old_alpha), ("beta", "beta-1.0.jar", old_beta)],
        )
        unmanaged_path = Path(profile.server_dir) / "mods" / "manual-mod.jar"
        unmanaged_path.write_bytes(unmanaged)
        plans = {
            "alpha": _plan("alpha", "2.0", "alpha-2.0.jar", new_alpha),
            "beta": _plan("beta", "2.0", "beta-2.0.jar", new_beta),
        }
        catalog = FakeCatalog(plans, {"alpha": new_alpha, "beta": new_beta})
        manager = FakeManager(active)
        return profile, manager, catalog, catalog_contents, unmanaged_path, unmanaged

    def test_failed_mod_is_reverted_and_next_managed_mod_is_still_updated(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, original, unmanaged_path, unmanaged_bytes = self._fixture(Path(temp))
            checks = iter(
                [
                    (False, "new alpha does not boot"),
                    (True, "old alpha boots"),
                    (True, "new beta boots"),
                ]
            )
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: next(checks))
            prepared = updater.prepare(profile)
            report = updater.apply(profile, prepared, full_server_backup=False)

            alpha = Path(profile.server_dir) / "mods" / "alpha-1.0.jar"
            beta = Path(profile.server_dir) / "mods" / "beta-2.0.jar"
            self.assertEqual(alpha.read_bytes(), original["alpha"])
            self.assertTrue(beta.is_file())
            self.assertFalse((Path(profile.server_dir) / "mods" / "beta-1.0.jar").exists())
            self.assertEqual(unmanaged_path.read_bytes(), unmanaged_bytes)
            self.assertEqual(catalog.installed, ["alpha", "beta"])
            self.assertEqual([item.status for item in report.outcomes], ["zurückgesetzt", "aktualisiert"])
            self.assertEqual(manager.starts, 3)
            self.assertFalse(manager.services.active)
            self.assertIsNotNone(report.transaction_id)

    def test_apply_reports_download_backup_and_startup_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, _original, _unmanaged_path, _unmanaged_bytes = self._fixture(Path(temp))
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            messages: list[str] = []
            prepared = updater.prepare(profile, progress=messages.append)
            updater.apply(profile, prepared, progress=messages.append)

            self.assertTrue(any("Update-Prüfung" in message and "100.0%" in message for message in messages))
            self.assertTrue(any("Rollback-Sicherung" in message for message in messages))
            self.assertTrue(any("Download" in message and "MiB" not in message for message in messages))
            self.assertTrue(any("Abschlussprüfung" in message for message in messages))
            self.assertTrue(any("Server wird gestartet" in message for message in messages))
            self.assertTrue(any("Serverstartprüfung erfolgreich" in message for message in messages))

    def test_manual_rollback_restores_only_updated_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, original, unmanaged_path, unmanaged_bytes = self._fixture(Path(temp))
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            prepared = updater.prepare(profile)
            report = updater.apply(profile, prepared)
            self.assertEqual(len(report.outcomes), 2)
            transaction_id = report.transaction_id
            self.assertIsNotNone(transaction_id)
            result = updater.rollback(profile, transaction_id=transaction_id)

            self.assertEqual(result, transaction_id)
            self.assertEqual((Path(profile.server_dir) / "mods" / "alpha-1.0.jar").read_bytes(), original["alpha"])
            self.assertEqual((Path(profile.server_dir) / "mods" / "beta-1.0.jar").read_bytes(), original["beta"])
            self.assertFalse((Path(profile.server_dir) / "mods" / "alpha-2.0.jar").exists())
            self.assertFalse((Path(profile.server_dir) / "mods" / "beta-2.0.jar").exists())
            self.assertEqual(unmanaged_path.read_bytes(), unmanaged_bytes)
            manifest = json.loads((Path(profile.server_dir) / "config" / "catalog-installed.json").read_text(encoding="utf-8"))
            self.assertEqual([item["path"] for item in manifest["files"]], ["mods/alpha-1.0.jar", "mods/beta-1.0.jar"])
            self.assertFalse(manager.services.active)

    def test_interrupt_during_full_backup_restores_initial_running_state(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, original, _unmanaged_path, _unmanaged_bytes = self._fixture(
                Path(temp), active=True
            )
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            prepared = updater.prepare(profile)
            with patch.object(manager, "backup", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply(profile, prepared, full_server_backup=True)

            self.assertEqual(manager.stops, 1)
            self.assertEqual(manager.starts, 1)
            self.assertTrue(manager.services.active)
            for project_id, filename in (("alpha", "alpha-1.0.jar"), ("beta", "beta-1.0.jar")):
                self.assertEqual((Path(profile.server_dir) / "mods" / filename).read_bytes(), original[project_id])
            self.assertFalse(list((Path(profile.server_dir) / "backups" / "mod-updates").glob("*/transaction.json")))

    def test_interrupt_during_mod_install_rolls_back_and_restores_initial_running_state(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, original, _unmanaged_path, _unmanaged_bytes = self._fixture(
                Path(temp), active=True
            )
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            prepared = updater.prepare(profile)

            def interrupted_install(plan, install_profile, **_kwargs):
                target = Path(install_profile.server_dir) / "mods" / plan.file_name
                target.write_bytes(catalog.contents[plan.project_id])
                raise KeyboardInterrupt

            with patch.object(catalog, "install", side_effect=interrupted_install):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply(profile, prepared)

            self.assertTrue(manager.services.active)
            self.assertEqual(manager.starts, 1)
            for project_id, filename in (("alpha", "alpha-1.0.jar"), ("beta", "beta-1.0.jar")):
                self.assertEqual((Path(profile.server_dir) / "mods" / filename).read_bytes(), original[project_id])
            self.assertFalse((Path(profile.server_dir) / "mods" / "alpha-2.0.jar").exists())
            transaction_files = list((Path(profile.server_dir) / "backups" / "mod-updates").glob("*/transaction.json"))
            self.assertEqual(len(transaction_files), 1)
            metadata = json.loads(transaction_files[0].read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "auto_rolled_back")

    def test_update_preserves_initial_running_state_and_can_make_full_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, _original, _unmanaged_path, _unmanaged_bytes = self._fixture(Path(temp), active=True)
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            report = updater.apply(profile, updater.prepare(profile), full_server_backup=True)
            self.assertTrue(manager.services.active)
            self.assertEqual(manager.backups, 1)
            self.assertIsNotNone(report.full_server_backup)
            self.assertEqual(manager.starts, 3)  # two per-mod checks plus the final restart

    def test_failed_rollback_aborts_remaining_updates_and_restores_the_whole_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, original, _unmanaged_path, _unmanaged_bytes = self._fixture(Path(temp))
            checks = iter([(False, "new alpha fails"), (False, "old alpha also fails")])
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: next(checks))
            prepared = updater.prepare(profile)
            with self.assertRaisesRegex(ModUpdateError, "Alle Mod-Änderungen.*zurückgesetzt"):
                updater.apply(profile, prepared)

            self.assertEqual(catalog.installed, ["alpha"])
            self.assertEqual((Path(profile.server_dir) / "mods" / "alpha-1.0.jar").read_bytes(), original["alpha"])
            self.assertEqual((Path(profile.server_dir) / "mods" / "beta-1.0.jar").read_bytes(), original["beta"])
            self.assertFalse((Path(profile.server_dir) / "mods" / "alpha-2.0.jar").exists())
            self.assertFalse(manager.services.active)

    def test_manual_rollback_refuses_files_changed_after_update(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, _original, _unmanaged_path, _unmanaged_bytes = self._fixture(Path(temp))
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            report = updater.apply(profile, updater.prepare(profile))
            updated = Path(profile.server_dir) / "mods" / "alpha-2.0.jar"
            updated.write_bytes(_jar_bytes("changed after update"))
            with self.assertRaisesRegex(ModUpdateError, "verändert"):
                updater.rollback(profile, transaction_id=report.transaction_id)
            self.assertTrue(updated.is_file())

    def test_prepare_refuses_a_mod_jar_changed_after_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, manager, catalog, _original, _unmanaged_path, _unmanaged_bytes = self._fixture(Path(temp))
            (Path(profile.server_dir) / "mods" / "alpha-1.0.jar").write_bytes(_jar_bytes("manual edit"))
            updater = SafeModUpdater(manager, catalog, readiness_check=lambda *_args: (True, "Done"))
            with self.assertRaisesRegex(ModUpdateError, "verändert"):
                updater.prepare(profile)
            self.assertEqual(manager.starts, 0)
            self.assertEqual(catalog.installed, [])

    def test_listener_check_detects_a_running_java_tcp_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(tcp_listener_present(port))
        self.assertFalse(tcp_listener_present(port))

    def test_profile_lock_rejects_concurrent_mod_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            with mod_update_lock(profile):
                with self.assertRaisesRegex(ModUpdateError, "läuft bereits"):
                    with mod_update_lock(profile):
                        pass

    def test_startup_check_requires_a_fresh_done_line_and_stable_service(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            log = Path(profile.server_dir) / "logs" / "latest.log"
            log.write_text("old boot Done (0.1s)!\n", encoding="utf-8")
            offset = log.stat().st_size
            manager = FakeManager(active=True)
            clock = [0.0]

            def sleep(seconds: float) -> None:
                clock[0] += seconds
                if clock[0] == 1.0:
                    with log.open("a", encoding="utf-8") as stream:
                        stream.write("[Server thread/INFO]: Done (2.0s)! For help, type \"help\"\n")

            updater = SafeModUpdater(manager, readiness_check=None, sleep=sleep, monotonic=lambda: clock[0])
            progress: list[tuple[float, float]] = []
            ready, detail = updater._wait_for_ready(profile, 20, offset, progress=lambda elapsed, timeout: progress.append((elapsed, timeout)))
            self.assertTrue(ready)
            self.assertIn("Done", detail)
            self.assertGreaterEqual(clock[0], 6.0)
            self.assertEqual(progress, [(5.0, 20)])

    def test_startup_check_detects_rewritten_log_even_if_it_grows_past_old_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            log = Path(profile.server_dir) / "logs" / "latest.log"
            log.write_text("old boot output\\n" + ("x" * 1000), encoding="utf-8")
            manager = FakeManager(active=True)
            clock = [0.0]

            def sleep(seconds: float) -> None:
                clock[0] += seconds
                if clock[0] == 1.0:
                    log.write_text("[Server thread/INFO]: Done (2.0s)!\\n" + ("y" * 1500), encoding="utf-8")

            updater = SafeModUpdater(manager, readiness_check=None, sleep=sleep, monotonic=lambda: clock[0])
            checkpoint = updater._log_checkpoint(profile)
            ready, detail = updater._wait_for_ready(profile, 20, checkpoint)
            self.assertTrue(ready)
            self.assertIn("Done", detail)
            self.assertGreater(log.stat().st_size, checkpoint.size)

    def test_startup_check_fails_when_systemd_service_exits_before_done(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = _profile(Path(temp))
            manager = FakeManager(active=True)

            def sleep(_seconds: float) -> None:
                manager.services.active = False

            updater = SafeModUpdater(manager, readiness_check=None, sleep=sleep, monotonic=lambda: 0.0)
            ready, detail = updater._wait_for_ready(profile, 30, 0)
            self.assertFalse(ready)
            self.assertIn("systemd-Dienst wurde beendet", detail)


if __name__ == "__main__":
    unittest.main()
