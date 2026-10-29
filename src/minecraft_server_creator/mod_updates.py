from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import tempfile
import time
import uuid
from typing import Any, Callable, Iterator
import zipfile

try:
    import fcntl
except ImportError:  # pragma: no cover - systemd deployments are Linux/Unix
    fcntl = None

from .catalog import ContentCatalog, InstallPlan, NoCompatibleVersionError, _safe_filename
from .models import ServerProfile


class ModUpdateError(RuntimeError):
    """Raised when a managed mod update or rollback cannot be performed safely."""


@dataclass(frozen=True)
class _LogCheckpoint:
    size: int
    prefix: bytes
    inode: int | None


@dataclass(frozen=True)
class ModUpdateCandidate:
    record: dict[str, Any]
    plan: InstallPlan
    old_path: str
    new_path: str
    old_sha256: str


@dataclass(frozen=True)
class ModUpdateOutcome:
    title: str
    old_version: str
    new_version: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class PreparedModUpdates:
    profile_slug: str
    manifest_bytes: bytes | None
    mod_records: tuple[dict[str, Any], ...]
    candidates: tuple[ModUpdateCandidate, ...]
    skipped: tuple[ModUpdateOutcome, ...]


@dataclass
class ModUpdateReport:
    transaction_id: str | None
    backup_dir: Path | None
    full_server_backup: Path | None
    outcomes: list[ModUpdateOutcome] = field(default_factory=list)
    initial_server_running: bool = False
    auto_rolled_back: bool = False
    fatal_error: str | None = None


@contextmanager
def mod_update_lock(profile: ServerProfile) -> Iterator[None]:
    """Serialize MCSC mod installs, updates, and rollbacks for one profile."""
    if fcntl is None:
        raise ModUpdateError("Sichere Mod-Updates benötigen eine Plattform mit flock/systemd.")
    root = Path(profile.server_dir).resolve()
    config_dir = root / "config"
    if config_dir.is_symlink():
        raise ModUpdateError("Der Konfigurationsordner darf kein symbolischer Link sein.")
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config_dir.resolve().parent != root:
        raise ModUpdateError("Der Konfigurationsordner liegt nicht sicher innerhalb des Serverprofils.")
    lock_path = config_dir / ".mcsc-mod-update.lock"
    if lock_path.is_symlink():
        raise ModUpdateError("Die Mod-Update-Sperrdatei darf kein symbolischer Link sein.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ModUpdateError(f"Mod-Update-Sperrdatei kann nicht geöffnet werden: {exc}") from exc
    stream = os.fdopen(descriptor, "r+b")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ModUpdateError("Eine andere MCSC-Mod-Installation, Aktualisierung oder ein Rollback läuft bereits.") from exc
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tcp_listener_present(port: int) -> bool:
    for family, address in (
        (socket.AF_INET, ("127.0.0.1", port)),
        (socket.AF_INET6, ("::1", port)),
    ):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.25)
                if connection.connect_ex(address) == 0:
                    return True
        except OSError:
            continue
    return False


def _read_manifest(profile: ServerProfile) -> tuple[Path, bytes | None, dict[str, Any]]:
    root = Path(profile.server_dir).resolve()
    config_dir = root / "config"
    manifest_path = config_dir / "catalog-installed.json"
    if config_dir.is_symlink() or manifest_path.is_symlink():
        raise ModUpdateError("Die Katalog-Installationsliste und ihr Ordner dürfen keine symbolischen Links sein.")
    if not manifest_path.exists():
        return manifest_path, None, {"schema_version": 1, "files": []}
    try:
        raw = manifest_path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModUpdateError(f"Die Katalog-Installationsliste kann nicht gelesen werden: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("files"), list):
        raise ModUpdateError("Die Katalog-Installationsliste hat ein unbekanntes Format.")
    if any(not isinstance(item, dict) for item in data["files"]):
        raise ModUpdateError("Die Katalog-Installationsliste enthält einen ungültigen Eintrag.")
    return manifest_path, raw, data


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)


def _restore_manifest(path: Path, raw: bytes | None) -> None:
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write_bytes(path, raw)


def _safe_mod_path(profile: ServerProfile, relative_path: str, *, must_exist: bool = True) -> Path:
    if not isinstance(relative_path, str):
        raise ModUpdateError("Ein verwalteter Mod-Pfad ist ungültig.")
    parts = PurePosixPath(relative_path).parts
    if (
        len(parts) != 2
        or parts[0] != "mods"
        or parts[1] in {"", ".", ".."}
        or "\\" in relative_path
        or "\n" in relative_path
        or "\r" in relative_path
    ):
        raise ModUpdateError(f"Unsicherer oder nicht unterstützter Mod-Pfad in der Installationsliste: {relative_path!r}")
    root = Path(profile.server_dir).resolve()
    mods_dir = root / "mods"
    if mods_dir.is_symlink():
        raise ModUpdateError("Der mods/-Ordner darf kein symbolischer Link sein.")
    mods_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if mods_dir.resolve().parent != root:
        raise ModUpdateError("Der mods/-Ordner liegt nicht sicher innerhalb des Serverprofils.")
    target = mods_dir / parts[1]
    if target.is_symlink():
        raise ModUpdateError(f"Mod-Dateien dürfen keine symbolischen Links sein: {relative_path}")
    if must_exist and (not target.is_file() or target.resolve().parent != mods_dir.resolve()):
        raise ModUpdateError(f"Verwaltete Mod-Datei fehlt oder liegt außerhalb von mods/: {relative_path}")
    if not must_exist and target.resolve(strict=False).parent != mods_dir.resolve():
        raise ModUpdateError(f"Ziel der Mod-Datei liegt außerhalb von mods/: {relative_path}")
    return target


def _snapshot_path(transaction_dir: Path, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    if len(parts) != 2 or parts[0] != "mods" or parts[1] in {"", ".", ".."} or "\\" in relative_path:
        raise ModUpdateError(f"Unsicherer Pfad im Mod-Update-Backup: {relative_path!r}")
    files_dir = transaction_dir / "files"
    mods_dir = files_dir / "mods"
    if files_dir.is_symlink() or mods_dir.is_symlink():
        raise ModUpdateError("Der Mod-Update-Sicherungspfad enthält einen symbolischen Link.")
    if files_dir.exists() and files_dir.resolve().parent != transaction_dir.resolve():
        raise ModUpdateError("Der Mod-Update-Sicherungspfad liegt außerhalb der Transaktion.")
    if mods_dir.exists() and mods_dir.resolve().parent != files_dir.resolve():
        raise ModUpdateError("Der Mod-Update-Sicherungspfad liegt außerhalb der Transaktion.")
    return mods_dir / parts[1]


def _write_transaction(transaction_dir: Path, metadata: dict[str, Any]) -> None:
    path = transaction_dir / "transaction.json"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=transaction_dir, prefix=".transaction-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)


def _load_transaction(profile: ServerProfile, transaction_id: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", transaction_id):
        raise ModUpdateError("Die Transaktions-ID ist ungültig.")
    root = Path(profile.server_dir).resolve()
    backup_root = root / "backups"
    base = backup_root / "mod-updates"
    transaction_dir = base / transaction_id
    metadata_path = transaction_dir / "transaction.json"
    if backup_root.is_symlink() or base.is_symlink() or transaction_dir.is_symlink() or metadata_path.is_symlink():
        raise ModUpdateError("Mod-Update-Backups dürfen keine symbolischen Links enthalten.")
    if not transaction_dir.is_dir() or transaction_dir.resolve().parent != base.resolve():
        raise ModUpdateError(f"Mod-Update-Backup nicht gefunden: {transaction_id}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModUpdateError(f"Mod-Update-Backup ist beschädigt ({transaction_id}): {exc}") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != 1
        or metadata.get("transaction_id") != transaction_id
        or not isinstance(metadata.get("original_manifest_exists"), bool)
        or not isinstance(metadata.get("original_files"), list)
        or not isinstance(metadata.get("planned_updates"), list)
        or not isinstance(metadata.get("candidate_paths"), list)
    ):
        raise ModUpdateError(f"Mod-Update-Backup hat ein unbekanntes Format: {transaction_id}")
    if metadata.get("status") not in {"in_progress", "complete", "auto_rolled_back", "rolled_back"}:
        raise ModUpdateError(f"Mod-Update-Backup enthält einen unbekannten Status: {transaction_id}")
    original_manifest_hash = metadata.get("original_manifest_sha256")
    if metadata["original_manifest_exists"]:
        if not isinstance(original_manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", original_manifest_hash):
            raise ModUpdateError(f"Mod-Update-Backup enthält eine ungültige Manifest-Prüfsumme: {transaction_id}")
    elif original_manifest_hash is not None:
        raise ModUpdateError(f"Mod-Update-Backup hat einen inkonsistenten Manifest-Eintrag: {transaction_id}")
    for item in metadata["original_files"]:
        if (
            not isinstance(item, dict)
            or not all(isinstance(item.get(key), str) for key in ("path", "backup", "sha256"))
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ModUpdateError(f"Mod-Update-Backup enthält einen ungültigen Dateieintrag: {transaction_id}")
    for item in metadata["planned_updates"]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("old_path", "new_path", "status")
        ):
            raise ModUpdateError(f"Mod-Update-Backup enthält einen ungültigen Updateplan: {transaction_id}")
    if not all(isinstance(item, str) for item in metadata["candidate_paths"]):
        raise ModUpdateError(f"Mod-Update-Backup enthält ungültige Mod-Pfade: {transaction_id}")
    planned_paths = {
        path
        for item in metadata["planned_updates"]
        for path in (item["old_path"], item["new_path"])
    }
    if planned_paths != set(metadata["candidate_paths"]):
        raise ModUpdateError(f"Mod-Update-Backup enthält inkonsistente Pfade: {transaction_id}")
    original_paths = [item["path"] for item in metadata["original_files"]]
    if len(original_paths) != len(set(original_paths)):
        raise ModUpdateError(f"Mod-Update-Backup enthält doppelte Originaldateien: {transaction_id}")
    return transaction_dir, metadata


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        shutil.copy2(source, temporary)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _record_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    return (str(record.get("source", "")), str(record.get("project_id", "")), str(record.get("path", "")))


class SafeModUpdater:
    """Update only MCSC-managed mods, testing each candidate and reverting failures."""

    def __init__(
        self,
        manager: Any,
        catalog: ContentCatalog | None = None,
        *,
        readiness_check: Callable[[ServerProfile, float, _LogCheckpoint], tuple[bool, str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.manager = manager
        self.catalog = catalog or ContentCatalog()
        self._readiness_check = readiness_check
        self._sleep = sleep
        self._monotonic = monotonic

    def prepare(self, profile: ServerProfile, *, allow_beta: bool = False) -> PreparedModUpdates:
        _, manifest_bytes, manifest = _read_manifest(profile)
        records = tuple(item for item in manifest["files"] if item.get("kind") == "mod")
        if not records:
            raise ModUpdateError(
                "Es sind keine MCSC-verwalteten Mods registriert. Installiere Mods über "
                "`mcsc catalog install` oder `mcsc mods install-clumps`, damit sie aktualisierbar sind."
            )

        installed_project_ids = {
            str(item.get("project_id")) for item in records if item.get("project_id") is not None
        }
        seen_projects: set[tuple[str, str]] = set()
        seen_paths: set[str] = set()
        candidates: list[ModUpdateCandidate] = []
        skipped: list[ModUpdateOutcome] = []
        for record in records:
            source = str(record.get("source") or "")
            project_id = str(record.get("project_id") or "")
            title = str(record.get("project_title") or project_id or "Unbekannter Mod")
            old_version = str(record.get("file_version") or "unbekannt")
            old_relative = str(record.get("path") or "")
            identity = (source, project_id)
            if not source or not project_id:
                raise ModUpdateError(f"Für '{title}' fehlen Katalogquelle oder Projekt-ID; sicherer Update abgebrochen.")
            if identity in seen_projects:
                raise ModUpdateError(
                    f"'{title}' ist mehrfach registriert. Entferne doppelte Katalogeinträge vor dem sicheren Update."
                )
            if old_relative in seen_paths:
                raise ModUpdateError(f"Mehrere Mods verweisen auf dieselbe Datei: {old_relative}")
            seen_projects.add(identity)
            seen_paths.add(old_relative)

            old_path = _safe_mod_path(profile, old_relative)
            algorithm = str(record.get("algorithm") or "")
            checksum = str(record.get("checksum") or "").lower()
            if algorithm not in {"sha512", "sha1", "md5"} or not checksum:
                raise ModUpdateError(f"Für '{title}' fehlt eine unterstützte Prüfsumme; sicherer Update abgebrochen.")
            try:
                hasher = hashlib.new(algorithm)
            except ValueError as exc:
                raise ModUpdateError(f"Prüfsummenalgorithmus für '{title}' wird nicht unterstützt.") from exc
            with old_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            if hasher.hexdigest().lower() != checksum:
                raise ModUpdateError(
                    f"'{title}' wurde seit der Kataloginstallation verändert. Datei nicht überschrieben: {old_relative}"
                )
            if not zipfile.is_zipfile(old_path):
                raise ModUpdateError(f"Die verwaltete Datei von '{title}' ist kein gültiges JAR-Archiv.")

            try:
                plan = self.catalog.prepare_install(
                    source,
                    "mod",
                    project_id,
                    profile,
                    include_beta=allow_beta,
                )
            except NoCompatibleVersionError as exc:
                skipped.append(ModUpdateOutcome(title, old_version, old_version, "übersprungen", str(exc)))
                continue
            if plan.kind != "mod" or plan.source != source or plan.project_id != project_id:
                raise ModUpdateError(f"Der Katalog lieferte für '{title}' eine unerwartete Projektidentität.")
            if plan.required_dependencies:
                missing = [item for item in plan.required_dependencies if item not in installed_project_ids]
                if missing:
                    skipped.append(
                        ModUpdateOutcome(
                            title,
                            old_version,
                            str(plan.file_version),
                            "blockiert",
                            "Neue erforderliche Abhängigkeiten sind nicht als MCSC-Mod installiert: " + ", ".join(missing),
                        )
                    )
                    continue
            new_filename = _safe_filename(plan.file_name, extension=".jar")
            new_relative = f"mods/{new_filename}"
            new_path = _safe_mod_path(profile, new_relative, must_exist=False)
            if new_relative != old_relative and new_path.exists():
                raise ModUpdateError(
                    f"Die neue Datei für '{title}' würde eine vorhandene Mod-Datei überschreiben: {new_relative}"
                )
            try:
                plan_hash = hashlib.new(plan.algorithm)
            except ValueError as exc:
                raise ModUpdateError(f"Der Prüfsummenalgorithmus für '{title}' wird nicht unterstützt.") from exc
            # A renamed file with identical bytes does not need a disruptive restart.
            with old_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    plan_hash.update(chunk)
            if plan_hash.hexdigest().lower() == plan.checksum.lower():
                skipped.append(ModUpdateOutcome(title, old_version, old_version, "aktuell", "Datei ist bereits aktuell."))
                continue
            candidates.append(
                ModUpdateCandidate(
                    dict(record),
                    plan,
                    old_relative,
                    new_relative,
                    _sha256_file(old_path),
                )
            )

        new_targets = [candidate.new_path for candidate in candidates]
        if len(new_targets) != len(set(new_targets)):
            raise ModUpdateError("Mehrere Updates würden dieselbe JAR-Datei erzeugen; es wurde nichts geändert.")
        return PreparedModUpdates(
            profile.slug,
            manifest_bytes,
            records,
            tuple(candidates),
            tuple(skipped),
        )

    @staticmethod
    def _manifest_path(profile: ServerProfile) -> Path:
        root = Path(profile.server_dir).resolve()
        config_dir = root / "config"
        manifest_path = config_dir / "catalog-installed.json"
        if config_dir.is_symlink() or manifest_path.is_symlink():
            raise ModUpdateError("Die Katalog-Installationsliste und ihr Ordner dürfen keine symbolischen Links sein.")
        return manifest_path

    def _create_transaction(
        self,
        profile: ServerProfile,
        prepared: PreparedModUpdates,
        *,
        full_server_backup: Path | None,
    ) -> tuple[str, Path, dict[str, Any]]:
        root = Path(profile.server_dir).resolve()
        backup_dir = root / "backups"
        update_root = backup_dir / "mod-updates"
        if backup_dir.is_symlink() or update_root.is_symlink():
            raise ModUpdateError("backups/ und backups/mod-updates/ dürfen keine symbolischen Links sein.")
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        update_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if update_root.resolve().parent != backup_dir.resolve():
            raise ModUpdateError("Der Mod-Update-Backupordner liegt nicht sicher innerhalb des Serverprofils.")
        if os.name != "nt":
            os.chmod(backup_dir, 0o700)
            os.chmod(update_root, 0o700)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f-UTC")
        transaction_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        transaction_dir = update_root / transaction_id
        transaction_dir.mkdir(mode=0o700)
        original_files: list[dict[str, str]] = []
        try:
            manifest_path = self._manifest_path(profile)
            current_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
            if current_manifest != prepared.manifest_bytes:
                raise ModUpdateError("Die Katalogliste änderte sich während der Update-Vorbereitung; starte den Update-Befehl erneut.")
            candidate_hashes = {candidate.old_path: candidate.old_sha256 for candidate in prepared.candidates}
            for record in prepared.mod_records:
                relative = str(record.get("path") or "")
                source = _safe_mod_path(profile, relative)
                before_hash = _sha256_file(source)
                if relative in candidate_hashes and before_hash != candidate_hashes[relative]:
                    raise ModUpdateError(f"'{relative}' änderte sich während der Update-Vorbereitung; starte den Befehl erneut.")
                backup_relative = f"files/{relative}"
                destination = transaction_dir / backup_relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if os.name != "nt":
                    os.chmod(destination, 0o600)
                if _sha256_file(destination) != before_hash or _sha256_file(source) != before_hash:
                    raise ModUpdateError(f"'{relative}' änderte sich während der Sicherung; es wurde nichts aktualisiert.")
                original_files.append(
                    {
                        "path": relative,
                        "backup": backup_relative,
                        "sha256": before_hash,
                    }
                )
            if prepared.manifest_bytes is not None:
                manifest_copy = transaction_dir / "catalog-installed.before.json"
                manifest_copy.write_bytes(prepared.manifest_bytes)
                if os.name != "nt":
                    os.chmod(manifest_copy, 0o600)
            planned = [
                {
                    "project_id": candidate.plan.project_id,
                    "project_title": candidate.plan.project_title,
                    "old_path": candidate.old_path,
                    "new_path": candidate.new_path,
                    "old_version": str(candidate.record.get("file_version") or "unknown"),
                    "new_version": candidate.plan.file_version,
                    "status": "pending",
                }
                for candidate in prepared.candidates
            ]
            candidate_paths = sorted(
                {path for item in planned for path in (item["old_path"], item["new_path"])}
            )
            metadata: dict[str, Any] = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "profile_slug": profile.slug,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "in_progress",
                "original_manifest_exists": prepared.manifest_bytes is not None,
                "original_manifest_sha256": _sha256_bytes(prepared.manifest_bytes) if prepared.manifest_bytes is not None else None,
                "original_files": original_files,
                "planned_updates": planned,
                "candidate_paths": candidate_paths,
                "candidate_file_hashes": {},
                "full_server_backup": str(full_server_backup) if full_server_backup else None,
                "final_manifest_sha256": None,
                "outcomes": [],
            }
            current_bytes = manifest_path.read_bytes() if manifest_path.is_file() else None
            if current_bytes != prepared.manifest_bytes:
                raise ModUpdateError("Die Katalogliste änderte sich während der Update-Vorbereitung; starte den Update-Befehl erneut.")
            _write_transaction(transaction_dir, metadata)
            return transaction_id, transaction_dir, metadata
        except Exception:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            raise

    def _replace_one(self, profile: ServerProfile, candidate: ModUpdateCandidate, transaction_dir: Path) -> Path:
        old_path = _safe_mod_path(profile, candidate.old_path)
        new_path = _safe_mod_path(profile, candidate.new_path, must_exist=False)
        if _sha256_file(old_path) != candidate.old_sha256:
            raise ModUpdateError(f"'{candidate.plan.project_title}' änderte sich während der Vorbereitung; Update abgebrochen.")
        if candidate.new_path != candidate.old_path and new_path.exists():
            raise ModUpdateError(f"Zieldatei ist inzwischen vorhanden: {candidate.new_path}")
        snapshot = _snapshot_path(transaction_dir, candidate.old_path)
        if not snapshot.is_file() or snapshot.is_symlink() or _sha256_file(snapshot) != candidate.old_sha256:
            raise ModUpdateError(f"Die Transaktionssicherung für '{candidate.plan.project_title}' fehlt oder ist beschädigt.")

        manifest_path, before_bytes, manifest = _read_manifest(profile)
        matches = [
            item for item in manifest["files"]
            if _record_identity(item) == _record_identity(candidate.record)
        ]
        if len(matches) != 1:
            raise ModUpdateError(f"Der Katalogeintrag von '{candidate.plan.project_title}' ist nicht mehr eindeutig.")
        old_entry = matches[0]
        manifest["files"] = [item for item in manifest["files"] if item is not old_entry]

        old_path.unlink()
        ContentCatalog._write_manifest(manifest_path, manifest)
        try:
            installed_path = self.catalog.install(candidate.plan, profile, allow_beta=candidate.plan.release_channel == "beta")
        except Exception:
            # The transaction snapshot is durable before any mutation, so restore the old JAR
            # and manifest even if the download or checksum validation fails.
            backup = _snapshot_path(transaction_dir, candidate.old_path)
            if (
                backup.is_file()
                and not backup.is_symlink()
                and _sha256_file(backup) == candidate.old_sha256
            ):
                _copy_atomic(backup, old_path)
            _restore_manifest(manifest_path, before_bytes)
            raise
        return installed_path

    def _restore_step(
        self,
        profile: ServerProfile,
        candidate: ModUpdateCandidate,
        transaction_dir: Path,
        manifest_before: bytes | None,
    ) -> None:
        old_path = _safe_mod_path(profile, candidate.old_path, must_exist=False)
        new_path = _safe_mod_path(profile, candidate.new_path, must_exist=False)
        manifest_path = self._manifest_path(profile)
        backup = _snapshot_path(transaction_dir, candidate.old_path)
        if (
            not backup.is_file()
            or backup.is_symlink()
            or _sha256_file(backup) != candidate.old_sha256
        ):
            raise ModUpdateError(f"Mod-Backup für {candidate.old_path} fehlt oder ist beschädigt; Rollback abgebrochen.")
        if candidate.new_path != candidate.old_path and new_path.exists() and not new_path.is_file():
            raise ModUpdateError(f"Unerwarteter Dateityp beim Zurücksetzen: {candidate.new_path}")
        if candidate.new_path != candidate.old_path and new_path.exists():
            new_path.unlink()
        _copy_atomic(backup, old_path)
        _restore_manifest(manifest_path, manifest_before)

    def _log_checkpoint(self, profile: ServerProfile) -> _LogCheckpoint:
        path = Path(profile.server_dir) / "logs" / "latest.log"
        try:
            stat_result = path.stat()
            prefix_size = min(stat_result.st_size, 4096)
            with path.open("rb") as stream:
                prefix = stream.read(prefix_size)
            return _LogCheckpoint(stat_result.st_size, prefix, stat_result.st_ino)
        except OSError:
            return _LogCheckpoint(0, b"", None)

    @staticmethod
    def _log_tail(profile: ServerProfile, *, max_lines: int = 35) -> str:
        path = Path(profile.server_dir) / "logs" / "latest.log"
        try:
            return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:])
        except OSError:
            return "(logs/latest.log ist nicht verfügbar)"

    def _wait_for_ready(
        self, profile: ServerProfile, timeout: float, checkpoint: _LogCheckpoint | int
    ) -> tuple[bool, str]:
        log_path = Path(profile.server_dir) / "logs" / "latest.log"
        if isinstance(checkpoint, int):
            checkpoint = _LogCheckpoint(checkpoint, b"", None)
        cursor = checkpoint.size
        carry = ""
        ready_at: float | None = None
        generation_checked = checkpoint.inode is None
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            if not self.manager.services.is_active(profile):
                return False, "Der systemd-Dienst wurde beendet, bevor Minecraft startbereit war.\n" + self._log_tail(profile)
            try:
                stat_result = log_path.stat()
                size = stat_result.st_size
                if not generation_checked:
                    changed = size < checkpoint.size or stat_result.st_ino != checkpoint.inode
                    if not changed and checkpoint.prefix:
                        with log_path.open("rb") as stream:
                            changed = stream.read(len(checkpoint.prefix)) != checkpoint.prefix
                    if changed:
                        cursor = 0
                        carry = ""
                        ready_at = None
                        generation_checked = True
                    elif size > cursor:
                        # Same file and prefix with a larger size means a simple append.
                        generation_checked = True
                elif size < cursor:
                    cursor = 0
                    carry = ""
                if generation_checked and size > cursor:
                    with log_path.open("rb") as stream:
                        stream.seek(cursor)
                        chunk = stream.read()
                        cursor = stream.tell()
                    text = carry + chunk.decode("utf-8", errors="replace")
                    if "Done (" in text:
                        ready_at = ready_at or self._monotonic()
                    carry = text[-32:]
            except OSError:
                pass
            if ready_at is not None and self.manager.services.is_active(profile):
                if self._monotonic() - ready_at >= 5.0:
                    return True, "Minecraft meldet Done und der Dienst blieb stabil."
            self._sleep(1.0)
        return False, f"Minecraft meldete innerhalb von {int(timeout)} s kein startbereites 'Done ('.\n{self._log_tail(profile)}"

    def _start_checked(
        self,
        profile: ServerProfile,
        *,
        startup_timeout: float,
        confirm_linger: Callable[[], bool],
    ) -> tuple[bool, str]:
        checkpoint = self._log_checkpoint(profile)
        self.manager.start(profile.slug, confirm_linger=confirm_linger)
        if self._readiness_check is not None:
            return self._readiness_check(profile, startup_timeout, checkpoint)
        return self._wait_for_ready(profile, startup_timeout, checkpoint)

    def _stop_if_active(self, profile: ServerProfile) -> None:
        if self.manager.services.is_active(profile):
            self.manager.stop(profile.slug)
        if self.manager.services.is_active(profile):
            raise ModUpdateError("Der Server ließ sich nicht sicher stoppen; keine weiteren Mod-Dateien geändert.")

    def _restore_snapshot(self, profile: ServerProfile, transaction_dir: Path, metadata: dict[str, Any]) -> None:
        original_entries = metadata.get("original_files")
        candidate_paths = metadata.get("candidate_paths")
        planned_updates = metadata.get("planned_updates")
        if not isinstance(original_entries, list) or not isinstance(candidate_paths, list) or not isinstance(planned_updates, list):
            raise ModUpdateError("Backup enthält keine gültigen Pfadlisten für den Rollback.")
        originals: dict[str, dict[str, Any]] = {}
        for item in original_entries:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ModUpdateError("Backup enthält einen ungültigen Originaldatei-Eintrag.")
            relative = item["path"]
            if relative in originals:
                raise ModUpdateError(f"Backup enthält die Originaldatei mehrfach: {relative}")
            originals[relative] = item
        if not all(isinstance(relative, str) for relative in candidate_paths):
            raise ModUpdateError("Backup enthält einen ungültigen Mod-Pfad.")
        if len(candidate_paths) != len(set(candidate_paths)):
            raise ModUpdateError("Backup enthält Mod-Dateien mehrfach in der Rollback-Liste.")
        for item in planned_updates:
            if not isinstance(item, dict) or item.get("old_path") not in originals:
                raise ModUpdateError("Backup enthält keine Originaldatei für einen geplanten Mod.")
            if item.get("new_path") != item.get("old_path") and item.get("new_path") in originals:
                raise ModUpdateError("Updateziel kollidiert mit einer anderen gesicherten Mod-Datei.")

        targets: dict[str, Path] = {}
        for relative in candidate_paths:
            target = _safe_mod_path(profile, relative, must_exist=False)
            if target.exists() and not target.is_file():
                raise ModUpdateError(f"Unerwarteter Dateityp beim Wiederherstellen: {relative}")
            targets[relative] = target

        snapshot_files: dict[str, Path] = {}
        for relative in candidate_paths:
            item = originals.get(relative)
            if item is None:
                continue
            expected_backup = f"files/{relative}"
            if item.get("backup") != expected_backup:
                raise ModUpdateError(f"Ungültiger Sicherungspfad im Mod-Update-Backup: {relative}")
            backup = _snapshot_path(transaction_dir, relative)
            if not backup.is_file() or backup.is_symlink() or _sha256_file(backup) != item.get("sha256"):
                raise ModUpdateError(f"Mod-Backup ist beschädigt oder fehlt: {relative}")
            snapshot_files[relative] = backup

        manifest_path = self._manifest_path(profile)
        original_manifest: bytes | None = None
        if metadata.get("original_manifest_exists"):
            backup = transaction_dir / "catalog-installed.before.json"
            if backup.is_symlink() or not backup.is_file() or _sha256_file(backup) != metadata.get("original_manifest_sha256"):
                raise ModUpdateError("Die gesicherte Katalogliste ist beschädigt oder fehlt.")
            original_manifest = backup.read_bytes()

        # Validate all paths and snapshots before the first deletion, so a corrupt
        # backup cannot leave a half-applied rollback.
        for relative, target in targets.items():
            if relative not in originals and target.exists():
                target.unlink()
        for relative, backup in snapshot_files.items():
            _copy_atomic(backup, targets[relative])
        _restore_manifest(manifest_path, original_manifest)


    def apply(
        self,
        profile: ServerProfile,
        prepared: PreparedModUpdates,
        *,
        startup_timeout: int = 180,
        confirm_linger: Callable[[], bool] = lambda: False,
        full_server_backup: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> ModUpdateReport:
        with mod_update_lock(profile):
            return self._apply_locked(
                profile,
                prepared,
                startup_timeout=startup_timeout,
                confirm_linger=confirm_linger,
                full_server_backup=full_server_backup,
                progress=progress,
            )

    def _apply_locked(
        self,
        profile: ServerProfile,
        prepared: PreparedModUpdates,
        *,
        startup_timeout: int = 180,
        confirm_linger: Callable[[], bool] = lambda: False,
        full_server_backup: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> ModUpdateReport:
        if profile.slug != prepared.profile_slug:
            raise ModUpdateError("Der vorbereitete Updateplan gehört zu einem anderen Serverprofil.")
        if not 30 <= startup_timeout <= 1800:
            raise ModUpdateError("Das Startup-Timeout muss zwischen 30 und 1800 Sekunden liegen.")
        if not prepared.candidates:
            return ModUpdateReport(None, None, None, list(prepared.skipped), self.manager.services.is_active(profile))
        if not self.manager.services.supported():
            raise ModUpdateError("Der sichere Mod-Updater benötigt einen unterstützten systemd-Benutzerdienst.")
        announce = progress or (lambda _message: None)
        if hasattr(self.manager, "active_profiles"):
            other_active = [item for item in self.manager.active_profiles() if item.id != profile.id]
            if other_active:
                names = ", ".join(item.name for item in other_active)
                raise ModUpdateError(f"Andere MCSC-Server laufen ({names}); stoppe sie vor dem Mod-Update.")
        was_running = self.manager.services.is_active(profile)
        full_backup_path: Path | None = None
        if was_running:
            self.manager.stop(profile.slug)
            if self.manager.services.is_active(profile):
                raise ModUpdateError("Server konnte vor dem Mod-Update nicht sicher gestoppt werden.")
        if tcp_listener_present(profile.ports.java_tcp):
            raise ModUpdateError(
                f"Der Java-Port {profile.ports.java_tcp}/TCP ist trotz gestopptem MCSC-Dienst erreichbar. "
                "Möglicherweise läuft ein manuell gestarteter Server; es wurden keine Mod-Dateien verändert."
            )
        try:
            if full_server_backup:
                announce("Vollständiges Server-Backup wird erstellt; regelmäßige Fortschrittsmeldungen folgen.")
                full_backup_path = self.manager.backup(profile.slug, progress=announce)
            transaction_id, transaction_dir, metadata = self._create_transaction(
                profile,
                prepared,
                full_server_backup=full_backup_path,
            )
        except BaseException as exc:
            # Ctrl+C during the pre-update backup must not strand an originally running server stopped.
            if was_running:
                try:
                    ok, detail = self._start_checked(
                        profile, startup_timeout=startup_timeout, confirm_linger=confirm_linger
                    )
                    if not ok:
                        self._stop_if_active(profile)
                        raise ModUpdateError(detail)
                except Exception as restart_error:
                    try:
                        self._stop_if_active(profile)
                    except Exception:
                        pass
                    raise ModUpdateError(
                        f"Update-Sicherung fehlgeschlagen ({exc}); der vorher laufende Server konnte nicht wieder gestartet werden: {restart_error}"
                    ) from restart_error
            raise

        report = ModUpdateReport(transaction_id, transaction_dir, full_backup_path, list(prepared.skipped), was_running)
        for index, candidate in enumerate(prepared.candidates):
            title = candidate.plan.project_title
            old_version = str(candidate.record.get("file_version") or "unbekannt")
            new_version = candidate.plan.file_version
            announce(f"[{index + 1}/{len(prepared.candidates)}] {title}: {old_version} → {new_version}")
            _, manifest_before, _ = _read_manifest(profile)
            item_meta = metadata["planned_updates"][index]
            try:
                self._replace_one(profile, candidate, transaction_dir)
                item_meta["status"] = "testing"
                _write_transaction(transaction_dir, metadata)
                ready, detail = self._start_checked(
                    profile,
                    startup_timeout=startup_timeout,
                    confirm_linger=confirm_linger,
                )
                if not ready:
                    raise ModUpdateError(detail)
                self._stop_if_active(profile)
                item_meta["status"] = "updated"
                item_meta["detail"] = detail
                report.outcomes.append(ModUpdateOutcome(title, old_version, new_version, "aktualisiert", detail))
                announce(f"  OK: {title} startet mit der neuen Version.")
            except Exception as candidate_error:
                try:
                    self._stop_if_active(profile)
                    self._restore_step(profile, candidate, transaction_dir, manifest_before)
                    item_meta["status"] = "reverted"
                    item_meta["detail"] = str(candidate_error)
                    _write_transaction(transaction_dir, metadata)
                    announce(f"  FEHLER: {title} — alte Version wird wiederhergestellt und geprüft.")
                    baseline_ready, baseline_detail = self._start_checked(
                        profile,
                        startup_timeout=startup_timeout,
                        confirm_linger=confirm_linger,
                    )
                    if not baseline_ready:
                        raise ModUpdateError(baseline_detail)
                    self._stop_if_active(profile)
                    report.outcomes.append(
                        ModUpdateOutcome(title, old_version, new_version, "zurückgesetzt", str(candidate_error))
                    )
                    announce(f"  Zurückgesetzt: {title}; Server läuft mit der alten Version.")
                except Exception as rollback_error:
                    self._stop_if_active(profile)
                    self._restore_snapshot(profile, transaction_dir, metadata)
                    metadata["status"] = "auto_rolled_back"
                    metadata["fatal_error"] = f"{candidate_error}; Rollback-Test: {rollback_error}"
                    metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
                    _write_transaction(transaction_dir, metadata)
                    report.auto_rolled_back = True
                    report.fatal_error = (
                        f"'{title}' schlug fehl; auch der Start mit der alten Mod-Version war nicht erfolgreich. "
                        "Alle Mod-Änderungen dieser Transaktion wurden zurückgesetzt."
                    )
                    if was_running:
                        try:
                            restored_ready, restored_detail = self._start_checked(
                                profile,
                                startup_timeout=startup_timeout,
                                confirm_linger=confirm_linger,
                            )
                            if not restored_ready:
                                report.fatal_error += f" Der ursprüngliche Server startet weiterhin nicht: {restored_detail}"
                                self._stop_if_active(profile)
                        except Exception as start_error:
                            try:
                                self._stop_if_active(profile)
                            except Exception:
                                pass
                            report.fatal_error += f" Der ursprüngliche Server konnte nicht gestartet werden: {start_error}"
                    report.fatal_error += f" Sicherung: {transaction_dir} (Transaktion {transaction_id})."
                    raise ModUpdateError(report.fatal_error) from rollback_error

        if was_running:
            try:
                final_ready, final_detail = self._start_checked(
                    profile,
                    startup_timeout=startup_timeout,
                    confirm_linger=confirm_linger,
                )
                if not final_ready:
                    raise ModUpdateError(final_detail)
            except Exception as final_error:
                self._stop_if_active(profile)
                self._restore_snapshot(profile, transaction_dir, metadata)
                metadata["status"] = "auto_rolled_back"
                metadata["fatal_error"] = str(final_error)
                metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
                _write_transaction(transaction_dir, metadata)
                report.auto_rolled_back = True
                report.fatal_error = (
                    "Der abschließende Serverstart schlug fehl; alle Mod-Änderungen dieser Transaktion wurden zurückgesetzt."
                )
                try:
                    old_ready, old_detail = self._start_checked(
                        profile,
                        startup_timeout=startup_timeout,
                        confirm_linger=confirm_linger,
                    )
                    if not old_ready:
                        report.fatal_error += f" Der vorherige Mod-Stand startet ebenfalls nicht: {old_detail}"
                        self._stop_if_active(profile)
                except Exception as start_error:
                    try:
                        self._stop_if_active(profile)
                    except Exception:
                        pass
                    report.fatal_error += f" Der vorherige Mod-Stand konnte nicht gestartet werden: {start_error}"
                report.fatal_error += f" Sicherung: {transaction_dir} (Transaktion {transaction_id})."
                raise ModUpdateError(report.fatal_error) from final_error
        else:
            self._stop_if_active(profile)
            final_detail = "Server war vor dem Update gestoppt und bleibt gestoppt."

        metadata["status"] = "complete"
        metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
        metadata["final_manifest_sha256"] = (
            _sha256_bytes(self._manifest_path(profile).read_bytes())
            if self._manifest_path(profile).is_file()
            else None
        )
        expected_hashes: dict[str, str | None] = {}
        for relative in metadata["candidate_paths"]:
            target = _safe_mod_path(profile, relative, must_exist=False)
            expected_hashes[relative] = _sha256_file(target) if target.is_file() and not target.is_symlink() else None
        metadata["candidate_file_hashes"] = expected_hashes
        _write_transaction(transaction_dir, metadata)
        if was_running:
            report.outcomes.append(ModUpdateOutcome("Server", "", "", "gestartet", final_detail))
        return report


    def _restore_all(
        self, profile: ServerProfile, transaction_id: str, *, allow_in_progress: bool = False
    ) -> tuple[Path, dict[str, Any]]:
        transaction_dir, metadata = _load_transaction(profile, transaction_id)
        if metadata.get("status") == "rolled_back":
            raise ModUpdateError(f"Transaktion {transaction_id} wurde bereits zurückgesetzt.")
        if metadata.get("status") == "auto_rolled_back":
            raise ModUpdateError(f"Transaktion {transaction_id} wurde bereits automatisch zurückgesetzt.")
        if metadata.get("status") == "in_progress" and not allow_in_progress:
            raise ModUpdateError("Die Mod-Update-Transaktion läuft noch; stoppe den Befehl und prüfe den Serverzustand.")
        if metadata.get("status") not in {"complete", "in_progress"}:
            raise ModUpdateError(
                f"Transaktion {transaction_id} kann nicht zurückgesetzt werden (Status: {metadata.get('status')})."
            )

        manifest_path = self._manifest_path(profile)
        if metadata.get("status") == "complete":
            current_hash = _sha256_bytes(manifest_path.read_bytes()) if manifest_path.is_file() else None
            if current_hash != metadata.get("final_manifest_sha256"):
                raise ModUpdateError(
                    "Die Katalogliste wurde nach diesem Update verändert. Rollback abgebrochen, "
                    "damit neuere Installationen erhalten bleiben."
                )
            expected = metadata.get("candidate_file_hashes")
            if not isinstance(expected, dict):
                raise ModUpdateError("Rollback-Backup enthält keine erwarteten Dateiprüfsummen.")
            for relative, expected_hash in expected.items():
                target = _safe_mod_path(profile, str(relative), must_exist=False)
                if expected_hash is None:
                    if target.exists():
                        raise ModUpdateError(
                            f"'{relative}' wurde seit dem Update verändert; Rollback bricht ab, "
                            "um Änderungen nicht zu überschreiben."
                        )
                    continue
                if not isinstance(expected_hash, str) or not target.is_file() or _sha256_file(target) != expected_hash:
                    raise ModUpdateError(
                        f"'{relative}' wurde seit dem Update verändert; Rollback bricht ab, "
                        "um Änderungen nicht zu überschreiben."
                    )
        self._restore_snapshot(profile, transaction_dir, metadata)
        metadata["status"] = "rolled_back"
        metadata["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _write_transaction(transaction_dir, metadata)
        return transaction_dir, metadata

    def rollback(self, profile: ServerProfile, *, transaction_id: str | None = None) -> str:
        with mod_update_lock(profile):
            if not self.manager.services.supported():
                raise ModUpdateError("Ein sicherer Mod-Rollback benötigt einen unterstützten systemd-Benutzerdienst.")
            if self.manager.services.is_active(profile) or tcp_listener_present(profile.ports.java_tcp):
                raise ModUpdateError("Stoppe den Server vor dem Zurücksetzen eines Mod-Updates.")
            if transaction_id is None:
                backups = self.list_backups(profile)
                latest = backups[0] if backups else None
                if latest is None or latest["status"] not in {"complete", "in_progress"}:
                    raise ModUpdateError(
                        "Das neueste Mod-Update hat keinen zurücksetzbaren Stand. "
                        "Wähle bei Bedarf eine ältere Transaktion ausdrücklich aus 'mcsc mods backups'."
                    )
                transaction_id = latest["transaction_id"]
            self._restore_all(profile, transaction_id, allow_in_progress=True)
            return transaction_id

    @staticmethod
    def list_backups(profile: ServerProfile) -> list[dict[str, str]]:
        root = Path(profile.server_dir).resolve()
        backup_root = root / "backups"
        update_root = backup_root / "mod-updates"
        if backup_root.is_symlink() or update_root.is_symlink():
            raise ModUpdateError("backups/ und backups/mod-updates/ dürfen keine symbolischen Links sein.")
        if not update_root.exists():
            return []
        if not update_root.is_dir():
            raise ModUpdateError("backups/mod-updates ist kein Verzeichnis.")
        results: list[dict[str, str]] = []
        for directory in sorted(update_root.iterdir(), reverse=True):
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or directory.resolve().parent != update_root.resolve()
                or (directory / "transaction.json").is_symlink()
            ):
                continue
            try:
                metadata = json.loads((directory / "transaction.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
                continue
            results.append(
                {
                    "transaction_id": str(metadata.get("transaction_id") or directory.name),
                    "created_at": str(metadata.get("created_at") or "unbekannt"),
                    "status": str(metadata.get("status") or "unbekannt"),
                    "full_server_backup": str(metadata.get("full_server_backup") or ""),
                }
            )
        return results
