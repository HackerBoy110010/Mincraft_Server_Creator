from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import tarfile
import tempfile
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile

from .minecraft import USER_AGENT, download_file


class RuntimeErrorMCSC(RuntimeError):
    pass


def host_platform(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_name = {"linux": "linux", "darwin": "mac", "windows": "windows"}.get(system)
    if not os_name:
        raise RuntimeErrorMCSC(f"Betriebssystem '{system}' wird für verwaltete Java-Laufzeiten noch nicht unterstützt.")
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "x64": "x64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine)
    if not arch:
        raise RuntimeErrorMCSC(f"Prozessorarchitektur '{machine}' wird für verwaltete Java-Laufzeiten nicht unterstützt.")
    return os_name, arch


def _safe_member_path(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", normalized) or any(part == ".." for part in pure.parts):
        raise RuntimeErrorMCSC(f"Unsicherer Pfad in Java-Archiv: {name}")
    candidate = root.joinpath(*[part for part in pure.parts if part not in ("", ".")])
    root_resolved = root.resolve()
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise RuntimeErrorMCSC(f"Archivpfad verlässt das Zielverzeichnis: {name}")
    return candidate


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            target = _safe_member_path(destination, info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise RuntimeErrorMCSC("Das Java-ZIP enthält einen unerwarteten symbolischen Link.")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            permissions = (info.external_attr >> 16) & 0o777
            if permissions and os.name != "nt":
                os.chmod(target, permissions & 0o755)


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    symlinks: list[tuple[Path, str]] = []
    hardlinks: list[tuple[Path, Path]] = []
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            target = _safe_member_path(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeErrorMCSC(f"Datei im Java-Archiv kann nicht gelesen werden: {member.name}")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
                if os.name != "nt":
                    os.chmod(target, member.mode & 0o755)
            elif member.issym():
                link_name = member.linkname.replace("\\", "/")
                if PurePosixPath(link_name).is_absolute():
                    raise RuntimeErrorMCSC("Das Java-Archiv enthält einen absoluten symbolischen Link.")
                resolved_link = (target.parent / link_name).resolve(strict=False)
                if resolved_link != root and root not in resolved_link.parents:
                    raise RuntimeErrorMCSC("Ein symbolischer Link im Java-Archiv verlässt das Zielverzeichnis.")
                symlinks.append((target, link_name))
            elif member.islnk():
                source = _safe_member_path(destination, member.linkname)
                hardlinks.append((target, source))
            else:
                raise RuntimeErrorMCSC(f"Nicht unterstützter Dateityp im Java-Archiv: {member.name}")

    for target, link_name in symlinks:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and not target.is_symlink():
            os.symlink(link_name, target)
    for target, source in hardlinks:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            raise RuntimeErrorMCSC("Ein Hardlink im Java-Archiv verweist auf eine nicht vorhandene Datei.")
        if not target.exists():
            os.link(source, target)


def find_java(root: Path) -> Path | None:
    executable = "java.exe" if os.name == "nt" else "java"
    if not root.exists():
        return None
    for candidate in root.rglob(executable):
        if candidate.is_file() and candidate.parent.name.lower() == "bin":
            return candidate
    return None


class RuntimeManager:
    """Downloads a checksum-verified Temurin JRE into the user's data directory."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.runtime_root = self.data_dir / "runtimes"

    def runtime_path(self, feature: int) -> Path:
        os_name, arch = host_platform()
        return self.runtime_root / f"temurin-{int(feature)}-{os_name}-{arch}"

    def ensure(self, feature: int) -> Path:
        os_name, arch = host_platform()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        target = self.runtime_root / f"temurin-{int(feature)}-{os_name}-{arch}"
        existing = find_java(target)
        if existing:
            return existing
        if target.exists():
            raise RuntimeErrorMCSC(f"Unvollständige Java-Laufzeit liegt bereits unter {target}; bitte prüfen/entfernen.")

        query = urlencode({"architecture": arch, "image_type": "jre", "os": os_name, "vendor": "eclipse"})
        url = f"https://api.adoptium.net/v3/assets/latest/{int(feature)}/hotspot?{query}"
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=25) as response:
                assets: Any = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeErrorMCSC(f"Java-Laufzeitinformationen konnten nicht geladen werden: {exc}") from exc
        if not isinstance(assets, list) or not assets:
            raise RuntimeErrorMCSC(f"Temurin-JRE {feature} ist für {os_name}/{arch} nicht verfügbar.")
        package = assets[0].get("binary", {}).get("package", {})
        package_url = package.get("link")
        checksum = package.get("checksum")
        if not package_url or not checksum:
            raise RuntimeErrorMCSC("Adoptium lieferte keinen Download mit SHA-256-Prüfsumme.")

        staging = self.runtime_root / f".{target.name}.install-{os.getpid()}"
        try:
            with tempfile.TemporaryDirectory(prefix="mcsc-java-", dir=self.runtime_root) as temp_name:
                temp = Path(temp_name)
                archive_path = temp / str(package.get("name") or "runtime-package")
                download_file(str(package_url), archive_path, checksum=str(checksum), algorithm="sha256", timeout=180.0)
                extracted = temp / "extracted"
                try:
                    if zipfile.is_zipfile(archive_path):
                        safe_extract_zip(archive_path, extracted)
                    else:
                        safe_extract_tar(archive_path, extracted)
                except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
                    raise RuntimeErrorMCSC(f"Java-Archiv konnte nicht sicher entpackt werden: {exc}") from exc
                java_in_extract = find_java(extracted)
                if java_in_extract is None:
                    raise RuntimeErrorMCSC("Im Java-Archiv wurde keine ausführbare bin/java gefunden.")
                if staging.exists():
                    raise RuntimeErrorMCSC(f"Temporärer Installationsordner existiert bereits: {staging}")
                shutil.move(str(extracted), str(staging))
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

        installed_java = find_java(target)
        if installed_java is None:
            raise RuntimeErrorMCSC("Die installierte Java-Laufzeit konnte nicht gefunden werden.")
        if os.name != "nt":
            installed_java.chmod(installed_java.stat().st_mode | 0o111)
        return installed_java
