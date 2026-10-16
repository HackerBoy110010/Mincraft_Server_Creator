from __future__ import annotations

from pathlib import Path

from .models import ServerProfile


class LaunchConfigurationError(RuntimeError):
    """Raised when a profile's launch metadata does not match its server type."""


def expected_launch(profile: ServerProfile) -> tuple[str, str]:
    if profile.server_type in {"vanilla", "fabric", "arclight"}:
        return "jar", "server.jar"
    if profile.server_type == "forge":
        if not profile.loader_version:
            raise LaunchConfigurationError("Für dieses Forge-Profil fehlt die installierte Loader-Version.")
        coordinate = f"{profile.minecraft_version}-{profile.loader_version}"
        return "args", f"libraries/net/minecraftforge/forge/{coordinate}/unix_args.txt"
    if profile.server_type == "neoforge":
        if not profile.loader_version:
            raise LaunchConfigurationError("Für dieses NeoForge-Profil fehlt die installierte Loader-Version.")
        return "args", f"libraries/net/neoforged/neoforge/{profile.loader_version}/unix_args.txt"
    raise LaunchConfigurationError(f"Für den Servertyp '{profile.server_type}' ist kein Startverfahren hinterlegt.")


def validate_profile_launch(profile: ServerProfile) -> Path:
    mode, target = expected_launch(profile)
    if profile.launch_mode != mode or profile.launch_target != target:
        raise LaunchConfigurationError(
            f"Die Startkonfiguration für {profile.server_type} stimmt nicht mit den installierten Metadaten überein."
        )
    root = Path(profile.server_dir).resolve()
    launch_file = root / target
    resolved_file = launch_file.resolve(strict=False)
    if (
        not launch_file.is_file()
        or launch_file.is_symlink()
        or resolved_file == root
        or root not in resolved_file.parents
    ):
        raise LaunchConfigurationError(f"Startdatei fehlt oder ist unsicher: {launch_file}")
    return launch_file


def build_java_command(profile: ServerProfile, java_path: str | Path) -> list[str]:
    validate_profile_launch(profile)
    root = Path(profile.server_dir).resolve()
    xms = min(1024, max(512, profile.memory_mb // 2))
    command = [str(java_path)]
    if profile.launch_mode == "args":
        user_args = root / "user_jvm_args.txt"
        resolved_user_args = user_args.resolve(strict=False)
        if user_args.exists():
            if (
                not user_args.is_file()
                or user_args.is_symlink()
                or root not in resolved_user_args.parents
            ):
                raise LaunchConfigurationError("user_jvm_args.txt fehlt oder ist ein unsicherer Pfad.")
            command.append("@user_jvm_args.txt")
    command.extend([f"-Xms{xms}M", f"-Xmx{profile.memory_mb}M"])
    if profile.launch_mode == "jar":
        command.extend(["-jar", str(root / profile.launch_target), "nogui"])
    else:
        command.extend([f"@{profile.launch_target}", "nogui"])
    return command
