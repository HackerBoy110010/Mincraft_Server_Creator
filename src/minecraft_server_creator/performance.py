from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Callable
import zipfile

from .catalog import CatalogError, ContentCatalog, InstallPlan, NoCompatibleVersionError
from .models import ServerProfile


class PerformancePackError(CatalogError):
    """Raised when the curated Fabric performance pack cannot be installed safely."""


class PerformanceReleaseUnavailable(PerformancePackError):
    """Raised when a project has neither a compatible stable nor alpha release."""


@dataclass(frozen=True)
class PerformanceMod:
    name: str
    project_id: str
    mod_ids: tuple[str, ...]
    mod_id_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedPerformanceMod:
    plan: InstallPlan
    is_dependency: bool = False
    alpha_fallback: bool = False


@dataclass(frozen=True)
class UnavailablePerformanceMod:
    name: str
    reason: str


@dataclass(frozen=True)
class PreparedPerformancePackage:
    plans: tuple[PlannedPerformanceMod, ...]
    already_installed: tuple[str, ...]
    unavailable: tuple[UnavailablePerformanceMod, ...] = ()


# Modrinth project IDs are stable. Version selection is intentionally dynamic,
# but only stable, server-capable Fabric releases are eligible by default.
PERFORMANCE_MODS = (
    PerformanceMod("Lithium", "gvQqBUqZ", ("lithium",)),
    PerformanceMod("FerriteCore", "uXXizFIs", ("ferritecore",)),
    PerformanceMod("C2ME", "VSNURh3q", ("c2me",), ("c2me",)),
    PerformanceMod("Krypton", "fQEb0iXm", ("krypton",)),
    PerformanceMod("ModernFix", "nmDcB62a", ("modernfix",)),
    PerformanceMod("Chunky", "fALzjamp", ("chunky",)),
)

_CLIENT_OPTIONAL = {"optional", "unsupported"}
_SERVER_SUPPORTED = {"required", "optional"}
_MAX_MOD_METADATA = 1_000_000
_MAX_NESTED_JAR_SIZE = 32 * 1024 * 1024


def _normalise_mod_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _plan_is_server_only_or_client_optional(plan: InstallPlan) -> bool:
    return plan.server_side in _SERVER_SUPPORTED and plan.client_side in _CLIENT_OPTIONAL


def _read_mod_ids_from_archive(archive: zipfile.ZipFile, *, depth: int = 0) -> set[str]:
    ids: set[str] = set()
    try:
        metadata_info = archive.getinfo("fabric.mod.json")
    except KeyError:
        return ids
    if metadata_info.file_size > _MAX_MOD_METADATA:
        raise PerformancePackError("fabric.mod.json in einem vorhandenen Mod-JAR ist unerwartet groß.")
    try:
        metadata = json.loads(archive.read(metadata_info).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise PerformancePackError(f"fabric.mod.json in einem vorhandenen Mod-JAR ist ungültig: {exc}") from exc
    if not isinstance(metadata, dict):
        raise PerformancePackError("fabric.mod.json in einem vorhandenen Mod-JAR hat ein ungültiges Format.")
    mod_id = metadata.get("id")
    if isinstance(mod_id, str) and mod_id:
        ids.add(_normalise_mod_id(mod_id))

    # Fabric's jar-in-jar metadata can contain the individual C2ME module IDs.
    # Inspect only a small, bounded set of safe nested JAR paths.
    if depth >= 2:
        return ids
    nested_entries = metadata.get("jars")
    if not isinstance(nested_entries, list):
        return ids
    for item in nested_entries[:64]:
        nested_name = item.get("file") if isinstance(item, dict) else None
        if not isinstance(nested_name, str):
            continue
        nested_path = PurePosixPath(nested_name)
        if nested_path.is_absolute() or ".." in nested_path.parts or "\\" in nested_name:
            continue
        try:
            nested_info = archive.getinfo(nested_name)
        except KeyError:
            continue
        if nested_info.file_size > _MAX_NESTED_JAR_SIZE:
            continue
        nested_bytes = archive.read(nested_info)
        try:
            with zipfile.ZipFile(BytesIO(nested_bytes)) as nested_archive:
                ids.update(_read_mod_ids_from_archive(nested_archive, depth=depth + 1))
        except (OSError, zipfile.BadZipFile):
            continue
    return ids


def _installed_fabric_mod_ids(profile: ServerProfile) -> set[str]:
    root = Path(profile.server_dir).resolve()
    mods_dir = root / "mods"
    if mods_dir.is_symlink():
        raise PerformancePackError("Der mods-Ordner darf kein symbolischer Link sein.")
    if not mods_dir.exists():
        return set()
    if not mods_dir.is_dir() or mods_dir.resolve().parent != root:
        raise PerformancePackError("Der mods-Ordner liegt nicht sicher innerhalb des Serverprofils.")

    mod_ids: set[str] = set()
    for path in sorted(mods_dir.iterdir(), key=lambda item: item.name.lower()):
        if path.is_symlink():
            raise PerformancePackError(f"Symbolischer Link im mods-Ordner wird nicht automatisch ausgewertet: {path.name}")
        if not path.is_file() or path.suffix.lower() != ".jar":
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                mod_ids.update(_read_mod_ids_from_archive(archive))
        except (OSError, zipfile.BadZipFile) as exc:
            raise PerformancePackError(f"Vorhandene Mod-Datei kann nicht sicher geprüft werden: {path.name}: {exc}") from exc
    return mod_ids


def _installed_project_ids(records: list[dict[str, object]], profile: ServerProfile) -> set[str]:
    root = Path(profile.server_dir).resolve()
    installed: set[str] = set()
    for record in records:
        if record.get("kind") != "mod":
            continue
        project_id = record.get("project_id")
        relative = record.get("path")
        if not isinstance(project_id, str) or not isinstance(relative, str):
            continue
        parts = PurePosixPath(relative).parts
        if len(parts) != 2 or parts[0] != "mods" or parts[1] in {"", ".", ".."} or "\\" in parts[1]:
            continue
        path = root / parts[0] / parts[1]
        if path.is_file() and not path.is_symlink():
            installed.add(project_id)
    return installed


def _existing_managed_main_mod(
    records: list[dict[str, object]],
    profile: ServerProfile,
    spec: PerformanceMod,
    plan: InstallPlan,
) -> str | None:
    """Verify and return an existing, unchanged catalog-managed copy of a pack mod."""
    root = Path(profile.server_dir).resolve()
    matches = [
        record
        for record in records
        if record.get("kind") == "mod" and record.get("project_id") == spec.project_id
    ]
    active: list[tuple[dict[str, object], Path]] = []
    for record in matches:
        relative = record.get("path")
        if not isinstance(relative, str):
            raise PerformancePackError(f"{spec.name}: der vorhandene Katalogeintrag enthält keinen sicheren Dateipfad.")
        parts = PurePosixPath(relative).parts
        if len(parts) != 2 or parts[0] != "mods" or parts[1] in {"", ".", ".."} or "\\" in parts[1]:
            raise PerformancePackError(f"{spec.name}: der vorhandene Katalogeintrag verweist nicht sicher auf eine Mod-Datei.")
        path = root / parts[0] / parts[1]
        if path.is_symlink():
            raise PerformancePackError(f"{spec.name}: die vorhandene Mod-Datei darf kein symbolischer Link sein.")
        if path.exists():
            if not path.is_file() or path.resolve().parent != (root / "mods").resolve():
                raise PerformancePackError(f"{spec.name}: die vorhandene Mod-Datei liegt nicht sicher im mods-Ordner.")
            active.append((record, path))

    if len(active) > 1:
        raise PerformancePackError(f"{spec.name}: mehrere aktive Katalogdateien gefunden; das Paket wurde abgelehnt.")
    if not active:
        if matches:
            raise PerformancePackError(
                f"{spec.name}: ein veralteter Katalogeintrag existiert, dessen Datei fehlt. "
                "Bereinige diesen Eintrag, bevor du das Performance-Paket installierst."
            )
        return None

    record, path = active[0]
    expected_fields = {
        "source": "modrinth",
        "kind": "mod",
        "project_id": spec.project_id,
        "file_name": path.name,
        "file_version": plan.file_version,
        "release_channel": plan.release_channel,
        "minecraft_version": profile.minecraft_version,
        "loader": "fabric",
    }
    mismatches = [key for key, value in expected_fields.items() if record.get(key) != value]
    algorithm = record.get("algorithm")
    checksum = record.get("checksum")
    if algorithm != plan.algorithm or not isinstance(checksum, str) or checksum.lower() != plan.checksum.lower():
        mismatches.append("Prüfsumme/Version")
    if mismatches:
        raise PerformancePackError(
            f"{spec.name} ist vorhanden, entspricht aber nicht exakt dem aktuell ausgewählten stabilen, "
            f"serverfähigen Release ({', '.join(mismatches)}). Das gesamte Paket wurde vor Änderungen abgelehnt."
        )
    if not isinstance(algorithm, str) or algorithm not in {"sha1", "sha512"}:
        raise PerformancePackError(f"{spec.name}: der Prüfsummenalgorithmus des vorhandenen Mods ist ungültig.")

    digest = hashlib.new(algorithm)
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PerformancePackError(f"{spec.name}: vorhandene Datei kann nicht geprüft werden: {exc}") from exc
    if digest.hexdigest().lower() != checksum.lower():
        raise PerformancePackError(
            f"{spec.name}: die vorhandene Datei wurde seit der Kataloginstallation verändert. "
            "Das gesamte Paket wurde vor Änderungen abgelehnt."
        )

    try:
        with zipfile.ZipFile(path) as archive:
            actual_ids = _read_mod_ids_from_archive(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PerformancePackError(f"{spec.name}: vorhandene JAR kann nicht sicher geprüft werden: {exc}") from exc
    if not _matches_mod_ids(spec.mod_ids, spec.mod_id_prefixes, actual_ids):
        raise PerformancePackError(f"{spec.name}: die vorhandene JAR enthält nicht die erwartete Fabric-Mod-ID.")
    return plan.file_version


def _matches_mod_ids(expected: tuple[str, ...], prefixes: tuple[str, ...], installed: set[str]) -> bool:
    normalized = {_normalise_mod_id(item) for item in expected}
    normalized_prefixes = tuple(_normalise_mod_id(item) for item in prefixes)
    return bool(installed.intersection(normalized)) or any(
        mod_id.startswith(prefix)
        for mod_id in installed
        for prefix in normalized_prefixes
    )


def _recorded_unresolvable_dependencies(plan: InstallPlan) -> tuple[str, ...]:
    project_ids = set(plan.required_dependency_project_ids)
    return tuple(item for item in plan.required_dependencies if item not in project_ids)


def _prepare_stable_or_alpha(
    catalog: ContentCatalog,
    project_id: str,
    profile: ServerProfile,
    *,
    context: str,
) -> tuple[InstallPlan, bool]:
    """Prefer a stable build; use alpha only when no compatible stable build exists."""
    try:
        return catalog.prepare_install("modrinth", "mod", project_id, profile), False
    except NoCompatibleVersionError as stable_error:
        try:
            plan = catalog.prepare_install("modrinth", "mod", project_id, profile, include_alpha=True)
        except NoCompatibleVersionError as alpha_error:
            raise PerformanceReleaseUnavailable(
                f"{context} hat weder ein passendes stabiles noch ein passendes Alpha-Fabric-Release "
                f"für Minecraft {profile.minecraft_version} (Stabil: {stable_error}; Alpha: {alpha_error})"
            ) from alpha_error
        if plan.release_channel not in {"release", "alpha"}:
            raise PerformancePackError(
                f"{context}: die Fallback-Suche lieferte unerwartet den Kanal "
                f"{plan.release_channel!r}; Beta-Dateien werden im Performance-Paket nicht verwendet."
            )
        return plan, plan.release_channel == "alpha"


def _require_safe_plan(plan: InstallPlan, *, context: str, allow_alpha: bool = False) -> None:
    if plan.source != "modrinth" or plan.kind != "mod":
        raise PerformancePackError(f"{context}: erwartet wurde eine Modrinth-Mod-Datei.")
    if plan.release_channel != "release" and not (allow_alpha and plan.release_channel == "alpha"):
        raise PerformancePackError(
            f"{context}: erlaubt sind stabile Releases und Alpha ausschließlich als Fallback ohne passendes stabiles Release."
        )
    if not _plan_is_server_only_or_client_optional(plan):
        raise PerformancePackError(
            f"{context}: Modrinth weist Client-Seite={plan.client_side or 'unbekannt'} und "
            f"Server-Seite={plan.server_side or 'unbekannt'} aus. Die Datei wird nicht installiert, "
            "weil Spieler sie möglicherweise ebenfalls benötigen."
        )


def prepare_performance_package(
    catalog: ContentCatalog,
    profile: ServerProfile,
    *,
    allow_partial: bool = False,
) -> PreparedPerformancePackage:
    """Resolve the package; optionally defer unavailable primary mods for explicit partial installs."""
    if profile.server_type != "fabric":
        raise PerformancePackError("Das Performance-Paket ist ausschließlich für reine Fabric-Serverprofile vorgesehen.")

    installed_records = catalog.installed(profile)
    managed_projects = _installed_project_ids(installed_records, profile)
    installed_mod_ids = _installed_fabric_mod_ids(profile)
    planned: list[PlannedPerformanceMod] = []
    already_installed: list[str] = []
    unavailable: list[UnavailablePerformanceMod] = []
    planned_project_ids: set[str] = set()
    dependency_queue: list[str] = []

    for spec in PERFORMANCE_MODS:
        try:
            plan, alpha_fallback = _prepare_stable_or_alpha(
                catalog,
                spec.project_id,
                profile,
                context=spec.name,
            )
        except PerformanceReleaseUnavailable as exc:
            if spec.project_id in managed_projects or _matches_mod_ids(
                spec.mod_ids,
                spec.mod_id_prefixes,
                installed_mod_ids,
            ):
                raise PerformancePackError(
                    f"{spec.name} ist bereits vorhanden, kann aber mangels passendem stabilem oder Alpha-Release "
                    "nicht sicher als Teilpaket verifiziert werden. Das Performance-Paket wurde vor Änderungen abgelehnt."
                ) from exc
            if not allow_partial:
                raise PerformancePackError(
                    f"{exc}. Das gesamte Performance-Paket wurde abgelehnt; es wurde nichts installiert."
                ) from exc
            unavailable.append(UnavailablePerformanceMod(spec.name, str(exc)))
            continue
        _require_safe_plan(plan, context=spec.name, allow_alpha=alpha_fallback)
        existing_version = _existing_managed_main_mod(installed_records, profile, spec, plan)
        if existing_version is not None:
            already_installed.append(
                f"{spec.name} {existing_version} ({plan.release_channel}; Datei und Katalogrelease geprüft)"
            )
            planned_project_ids.add(plan.project_id)
        elif _matches_mod_ids(spec.mod_ids, spec.mod_id_prefixes, installed_mod_ids):
            raise PerformancePackError(
                f"{spec.name} ist bereits vorhanden, aber nicht als unverändertes, katalogverwaltetes "
                "Modrinth-Release verifiziert. Das gesamte Performance-Paket wurde vor Änderungen "
                "abgelehnt; entferne oder aktualisiere den Konflikt erst nach einer Sicherung."
            )
        else:
            planned.append(PlannedPerformanceMod(plan, alpha_fallback=alpha_fallback))
            planned_project_ids.add(plan.project_id)
        unresolved = _recorded_unresolvable_dependencies(plan)
        if unresolved:
            raise PerformancePackError(
                f"{spec.name} nennt Abhängigkeiten, die nicht automatisch über eine Modrinth-Projekt-ID "
                f"aufgelöst werden können: {', '.join(unresolved)}. Es wurde noch nichts installiert."
            )
        dependency_queue.extend(plan.required_dependency_project_ids)

    processed_dependencies: set[str] = set()
    while dependency_queue:
        dependency_id = dependency_queue.pop(0)
        if dependency_id in processed_dependencies:
            continue
        processed_dependencies.add(dependency_id)
        if dependency_id in planned_project_ids or dependency_id in managed_projects:
            continue
        dependency_plan, dependency_alpha_fallback = _prepare_stable_or_alpha(
            catalog,
            dependency_id,
            profile,
            context=f"Erforderliche Abhängigkeit {dependency_id}",
        )
        _require_safe_plan(
            dependency_plan,
            context=f"Erforderliche Abhängigkeit {dependency_plan.project_title}",
            allow_alpha=dependency_alpha_fallback,
        )
        unresolved = _recorded_unresolvable_dependencies(dependency_plan)
        if unresolved:
            raise PerformancePackError(
                f"Die erforderliche Abhängigkeit {dependency_plan.project_title} nennt nicht auflösbare "
                f"Abhängigkeiten: {', '.join(unresolved)}. Es wurde noch nichts installiert."
            )
        if _matches_mod_ids((dependency_plan.project_slug, dependency_plan.project_id), (), installed_mod_ids):
            managed_projects.add(dependency_plan.project_id)
            already_installed.append(f"Abhängigkeit {dependency_plan.project_title}")
            continue
        planned.append(
            PlannedPerformanceMod(
                dependency_plan,
                is_dependency=True,
                alpha_fallback=dependency_alpha_fallback,
            )
        )
        planned_project_ids.add(dependency_plan.project_id)
        dependency_queue.extend(dependency_plan.required_dependency_project_ids)

    filenames: set[str] = set()
    root = Path(profile.server_dir).resolve()
    mods_dir = root / "mods"
    if mods_dir.is_symlink():
        raise PerformancePackError("Der mods-Ordner darf kein symbolischer Link sein.")
    for item in planned:
        filename_key = item.plan.file_name.casefold()
        if filename_key in filenames:
            raise PerformancePackError(f"Mehrere Paketmods würden dieselbe Datei installieren: {item.plan.file_name}")
        filenames.add(filename_key)
        target = mods_dir / item.plan.file_name
        if target.exists() or target.is_symlink():
            raise PerformancePackError(
                f"Die Zieldatei existiert bereits und wird nicht überschrieben: {target}. "
                "Prüfe zuerst die vorhandene Mod-Version."
            )

    return PreparedPerformancePackage(tuple(planned), tuple(already_installed), tuple(unavailable))


def _snapshot_manifest(profile: ServerProfile) -> tuple[Path, Path, bytes | None]:
    root = Path(profile.server_dir).resolve()
    config_dir = root / "config"
    manifest_path = config_dir / "catalog-installed.json"
    if config_dir.is_symlink() or manifest_path.is_symlink():
        raise PerformancePackError("Die Katalog-Installationsliste und ihr Ordner dürfen keine symbolischen Links sein.")
    if config_dir.exists() and not config_dir.is_dir():
        raise PerformancePackError("Der config-Ordner ist kein regulärer Ordner.")
    if manifest_path.exists() and not manifest_path.is_file():
        raise PerformancePackError("Die Katalog-Installationsliste ist keine reguläre Datei.")
    try:
        return config_dir, manifest_path, manifest_path.read_bytes() if manifest_path.exists() else None
    except OSError as exc:
        raise PerformancePackError(f"Katalog-Installationsliste konnte nicht gesichert werden: {exc}") from exc


def _restore_manifest(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".catalog-rollback-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)


def install_performance_package(
    catalog: ContentCatalog,
    profile: ServerProfile,
    prepared: PreparedPerformancePackage,
    *,
    progress: Callable[[int, int, InstallPlan, int, int | None], None] | None = None,
) -> list[Path]:
    """Install all preflighted files, rolling back this package if any download fails."""
    if not prepared.plans:
        return []
    if profile.server_type != "fabric":
        raise PerformancePackError("Das Performance-Paket ist ausschließlich für reine Fabric-Serverprofile vorgesehen.")

    root = Path(profile.server_dir).resolve()
    mods_dir = root / "mods"
    if mods_dir.is_symlink() or (mods_dir.exists() and (not mods_dir.is_dir() or mods_dir.resolve().parent != root)):
        raise PerformancePackError("Der mods-Ordner liegt nicht sicher innerhalb des Serverprofils.")
    filenames: set[str] = set()
    for item in prepared.plans:
        _require_safe_plan(
            item.plan,
            context=item.plan.project_title,
            allow_alpha=item.alpha_fallback,
        )
        key = item.plan.file_name.casefold()
        if key in filenames:
            raise PerformancePackError(f"Mehrere Paketmods würden dieselbe Datei installieren: {item.plan.file_name}")
        filenames.add(key)
        target = mods_dir / item.plan.file_name
        if target.exists() or target.is_symlink():
            raise PerformancePackError(f"Die Zieldatei existiert bereits und wird nicht überschrieben: {target}")

    _config_dir, manifest_path, manifest_before = _snapshot_manifest(profile)
    installed: list[Path] = []
    total = len(prepared.plans)
    try:
        for index, item in enumerate(prepared.plans, start=1):
            if progress is None:
                download_progress = None
            else:
                download_progress = lambda downloaded, expected, i=index, p=item.plan: progress(
                    i, total, p, downloaded, expected
                )
            installed.append(
                catalog.install(
                    item.plan,
                    profile,
                    allow_alpha=item.alpha_fallback,
                    progress=download_progress,
                )
            )
    except BaseException as exc:
        cleanup_errors: list[str] = []
        for path in reversed(installed):
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{path}: {cleanup_exc}")
        try:
            _restore_manifest(manifest_path, manifest_before)
        except OSError as cleanup_exc:
            cleanup_errors.append(f"Katalog-Installationsliste: {cleanup_exc}")
        if isinstance(exc, KeyboardInterrupt):
            if cleanup_errors:
                raise PerformancePackError(
                    "Abbruch: Paketdateien wurden zurückgerollt, aber die Bereinigung war unvollständig: "
                    + "; ".join(cleanup_errors)
                ) from exc
            raise
        message = f"Performance-Paket fehlgeschlagen; bereits installierte Paketdateien wurden zurückgerollt: {exc}"
        if cleanup_errors:
            message += " Bereinigung prüfen: " + "; ".join(cleanup_errors)
        raise PerformancePackError(message) from exc
    return installed
