from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
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


@dataclass(frozen=True)
class PreparedPerformancePackage:
    plans: tuple[PlannedPerformanceMod, ...]
    already_installed: tuple[str, ...]


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


def _installed_project_ids(catalog: ContentCatalog, profile: ServerProfile) -> set[str]:
    root = Path(profile.server_dir).resolve()
    installed: set[str] = set()
    for record in catalog.installed(profile):
        if record.get("kind") != "mod":
            continue
        project_id = record.get("project_id")
        relative = record.get("path")
        if not isinstance(project_id, str) or not isinstance(relative, str):
            continue
        parts = PurePosixPath(relative).parts
        if len(parts) != 2 or parts[0] != "mods" or parts[1] in {"", ".", ".."}:
            continue
        path = root / parts[0] / parts[1]
        if path.is_file() and not path.is_symlink():
            installed.add(project_id)
    return installed


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


def _require_safe_plan(plan: InstallPlan, *, context: str) -> None:
    if plan.source != "modrinth" or plan.kind != "mod":
        raise PerformancePackError(f"{context}: erwartet wurde eine Modrinth-Mod-Datei.")
    if plan.release_channel != "release":
        raise PerformancePackError(f"{context}: nur stabile Releases sind im Performance-Paket erlaubt.")
    if not _plan_is_server_only_or_client_optional(plan):
        raise PerformancePackError(
            f"{context}: Modrinth weist Client-Seite={plan.client_side or 'unbekannt'} und "
            f"Server-Seite={plan.server_side or 'unbekannt'} aus. Die Datei wird nicht installiert, "
            "weil Spieler sie möglicherweise ebenfalls benötigen."
        )


def prepare_performance_package(
    catalog: ContentCatalog,
    profile: ServerProfile,
) -> PreparedPerformancePackage:
    """Resolve stable server-safe Fabric mods and their required dependencies."""
    if profile.server_type != "fabric":
        raise PerformancePackError("Das Performance-Paket ist ausschließlich für reine Fabric-Serverprofile vorgesehen.")

    managed_projects = _installed_project_ids(catalog, profile)
    installed_mod_ids = _installed_fabric_mod_ids(profile)
    planned: list[PlannedPerformanceMod] = []
    already_installed: list[str] = []
    planned_project_ids: set[str] = set()
    dependency_queue: list[str] = []

    for spec in PERFORMANCE_MODS:
        if spec.project_id in managed_projects or _matches_mod_ids(spec.mod_ids, spec.mod_id_prefixes, installed_mod_ids):
            raise PerformancePackError(
                f"{spec.name} ist bereits in diesem Profil vorhanden. Die vorhandene Datei wird nicht "
                "überschrieben; das gesamte Performance-Paket wurde abgelehnt, bevor Dateien geändert wurden. "
                "Prüfe die vorhandene Version zuerst und verwende für spätere Updates den Mod-Updater."
            )
        try:
            plan = catalog.prepare_install("modrinth", "mod", spec.project_id, profile)
        except NoCompatibleVersionError as exc:
            raise PerformancePackError(
                f"{spec.name} hat kein passendes stabiles Fabric-Release für Minecraft "
                f"{profile.minecraft_version}: {exc}. Das gesamte Performance-Paket wurde "
                "abgelehnt; es wurde nichts installiert."
            ) from exc
        _require_safe_plan(plan, context=spec.name)
        unresolved = _recorded_unresolvable_dependencies(plan)
        if unresolved:
            raise PerformancePackError(
                f"{spec.name} nennt Abhängigkeiten, die nicht automatisch über eine Modrinth-Projekt-ID "
                f"aufgelöst werden können: {', '.join(unresolved)}. Es wurde noch nichts installiert."
            )
        planned.append(PlannedPerformanceMod(plan))
        planned_project_ids.add(plan.project_id)
        dependency_queue.extend(plan.required_dependency_project_ids)

    processed_dependencies: set[str] = set()
    while dependency_queue:
        dependency_id = dependency_queue.pop(0)
        if dependency_id in processed_dependencies:
            continue
        processed_dependencies.add(dependency_id)
        if dependency_id in planned_project_ids or dependency_id in managed_projects:
            continue
        try:
            dependency_plan = catalog.prepare_install("modrinth", "mod", dependency_id, profile)
        except NoCompatibleVersionError as exc:
            raise PerformancePackError(
                f"Eine erforderliche Abhängigkeit ({dependency_id}) hat kein stabiles, passendes "
                f"Fabric-Server-Release für Minecraft {profile.minecraft_version}: {exc}. Es wurde noch nichts installiert."
            ) from exc
        _require_safe_plan(dependency_plan, context=f"Erforderliche Abhängigkeit {dependency_plan.project_title}")
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
        planned.append(PlannedPerformanceMod(dependency_plan, is_dependency=True))
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

    return PreparedPerformancePackage(tuple(planned), tuple(already_installed))


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
        _require_safe_plan(item.plan, context=item.plan.project_title)
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
            installed.append(catalog.install(item.plan, profile, progress=download_progress))
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
