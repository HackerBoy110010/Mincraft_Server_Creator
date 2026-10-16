from __future__ import annotations

from pathlib import Path
import tempfile
import os


class PropertiesError(RuntimeError):
    pass


def read_properties(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    file_path = Path(path)
    if not file_path.exists():
        return values
    for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def update_properties(path: str | Path, updates: dict[str, object]) -> None:
    """Update selected server.properties keys while preserving unrelated lines."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {str(k): str(v) for k, v in updates.items()}
    if any("\n" in key or "\r" in key or "\n" in value or "\r" in value for key, value in normalized.items()):
        raise PropertiesError("Eigenschaftsnamen und -werte dürfen keine Zeilenumbrüche enthalten.")

    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines() if file_path.exists() else []
    found: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "!")) and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in normalized:
                output.append(f"{key}={normalized[key]}")
                found.add(key)
                continue
        output.append(line)
    for key, value in normalized.items():
        if key not in found:
            output.append(f"{key}={value}")

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=file_path.parent, prefix="properties-", delete=False) as handle:
            temp_name = handle.name
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, file_path)
    except OSError as exc:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise PropertiesError(f"server.properties konnte nicht gespeichert werden: {exc}") from exc
