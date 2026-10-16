from __future__ import annotations

import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .launch import LaunchConfigurationError, build_java_command
from .models import ServerProfile


class SystemdError(RuntimeError):
    pass


def _quote_systemd(value: str | Path) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise SystemdError("Pfade für systemd dürfen keine Zeilenumbrüche enthalten.")
    text = text.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


class SystemdUserService:
    """Manage one systemd --user unit per profile on Linux hosts."""

    def __init__(self, unit_dir: str | Path | None = None) -> None:
        self.unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"

    @staticmethod
    def supported() -> bool:
        return sys.platform.startswith("linux") and shutil.which("systemctl") is not None

    @staticmethod
    def _run(args: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemdError(f"Befehl konnte nicht ausgeführt werden ({' '.join(args)}): {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SystemdError(detail or f"Befehl fehlgeschlagen: {' '.join(args)}")
        return result

    @staticmethod
    def unit_name(profile: ServerProfile) -> str:
        return f"mcsc-{profile.slug}.service"

    def unit_path(self, profile: ServerProfile) -> Path:
        return self.unit_dir / self.unit_name(profile)

    def install_unit(self, profile: ServerProfile, java_path: str | Path) -> None:
        if not self.supported():
            raise SystemdError("Der Hintergrunddienst benötigt Linux mit systemd.")
        server_dir = Path(profile.server_dir).resolve()
        try:
            command = build_java_command(profile, java_path)
        except LaunchConfigurationError as exc:
            raise SystemdError(str(exc)) from exc
        exec_start = " ".join(_quote_systemd(part) for part in command)
        safe_name = profile.name.replace("\n", " ").replace("\r", " ")
        content = "\n".join(
            [
                "[Unit]",
                f"Description=Minecraft server managed by MCSC ({safe_name})",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={_quote_systemd(server_dir)}",
                f"ExecStart={exec_start}",
                "KillSignal=SIGINT",
                "TimeoutStopSec=90",
                "Restart=no",
                "UMask=0077",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        try:
            self.unit_dir.mkdir(parents=True, exist_ok=True)
            unit = self.unit_path(profile)
            unit.write_text(content, encoding="utf-8")
            os.chmod(unit, 0o600)
        except OSError as exc:
            raise SystemdError(f"systemd-Benutzerdienst konnte nicht gespeichert werden: {exc}") from exc
        self._run(["systemctl", "--user", "daemon-reload"])

    def linger_enabled(self) -> bool:
        if not shutil.which("loginctl"):
            return False
        try:
            result = subprocess.run(
                ["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip().lower() in {"yes", "1", "true"}

    def ensure_linger(self, confirm: bool) -> None:
        if self.linger_enabled():
            return
        if not confirm:
            raise SystemdError(
                "Damit der Server nach dem SSH-Abmelden weiterläuft, muss systemd-Lingering aktiviert werden. "
                "Starte erneut und bestätige die einmalige sudo-Abfrage."
            )
        if not shutil.which("loginctl") or not shutil.which("sudo"):
            raise SystemdError("loginctl oder sudo fehlt; systemd-Lingering kann nicht aktiviert werden.")
        self._run(["sudo", "loginctl", "enable-linger", getpass.getuser()], timeout=120.0)
        if not self.linger_enabled():
            raise SystemdError("systemd-Lingering konnte nicht verifiziert werden.")

    def is_active(self, profile: ServerProfile) -> bool:
        if not self.supported():
            return False
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", self.unit_name(profile)],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "active"

    def start(self, profile: ServerProfile, java_path: str | Path, confirm_linger: bool) -> None:
        self.ensure_linger(confirm_linger)
        self.install_unit(profile, java_path)
        if profile.auto_start_on_boot:
            self._run(["systemctl", "--user", "enable", self.unit_name(profile)])
        else:
            self._run(["systemctl", "--user", "disable", self.unit_name(profile)], timeout=10.0) if self._is_enabled(profile) else None
        self._run(["systemctl", "--user", "start", self.unit_name(profile)], timeout=30.0)

    def stop(self, profile: ServerProfile) -> None:
        if not self.is_active(profile):
            return
        self._run(["systemctl", "--user", "stop", self.unit_name(profile)], timeout=120.0)

    def set_autostart(self, profile: ServerProfile, enabled: bool) -> None:
        if not self.supported():
            raise SystemdError("Autostart wird in dieser ersten Version nur unter Linux/systemd unterstützt.")
        if not self.unit_path(profile).exists():
            return
        self._run(["systemctl", "--user", "daemon-reload"])
        if enabled:
            self._run(["systemctl", "--user", "enable", self.unit_name(profile)])
        else:
            self._run(["systemctl", "--user", "disable", self.unit_name(profile)])

    def _is_enabled(self, profile: ServerProfile) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", self.unit_name(profile)],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "enabled"

    def logs(self, profile: ServerProfile, *, follow: bool = False) -> int:
        if not shutil.which("journalctl"):
            raise SystemdError("journalctl wurde nicht gefunden.")
        args = ["journalctl", "--user", "-u", self.unit_name(profile), "-n", "100", "--no-pager"]
        if follow:
            args = ["journalctl", "--user", "-u", self.unit_name(profile), "-f"]
        try:
            return subprocess.run(args, check=False).returncode
        except OSError as exc:
            raise SystemdError(f"Logs konnten nicht angezeigt werden: {exc}") from exc
