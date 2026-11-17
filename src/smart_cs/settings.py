"""Project paths, independent of the process working directory."""
import os
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("SMART_CS_PROJECT_ROOT") or Path(__file__).resolve().parents[2]).resolve()
ENV_PATH = PROJECT_ROOT / ".env"


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
