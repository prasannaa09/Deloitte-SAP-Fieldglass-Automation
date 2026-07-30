"""Helper utilities module for general file, path, and formatting operations."""

from datetime import datetime
from pathlib import Path
from typing import Union


def ensure_directory(path: Union[str, Path]) -> Path:
    """Ensure that the given directory path exists, creating parents if necessary.

    Args:
        path: Path string or Path object.

    Returns:
        Path: Resolved absolute Path object.
    """
    target_path = Path(path).resolve()
    target_path.mkdir(parents=True, exist_ok=True)
    return target_path


def generate_timestamp_filename(prefix: str, extension: str) -> str:
    """Generate a file name with a formatted timestamp suffix.

    Args:
        prefix: File prefix string (e.g., 'invoice_report').
        extension: File extension without or with leading dot (e.g., 'xlsx').

    Returns:
        str: Timestamped filename (e.g., 'invoice_report_20260730_143000.xlsx').
    """
    clean_ext = extension.lstrip(".")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.{clean_ext}"
