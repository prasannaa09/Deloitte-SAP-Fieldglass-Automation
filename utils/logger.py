"""Logging module using Loguru for daily rotating log files, error logs, and console output."""

import sys
from pathlib import Path

from loguru import logger

from config.settings import Settings, get_settings


def setup_logger(settings: Settings | None = None) -> None:
    """Configure Loguru logging handlers for console output, daily rotating logs, and error log files.

    Args:
        settings: Application settings instance. If None, retrieves global settings.
    """
    if settings is None:
        settings = get_settings()

    log_dir: Path = settings.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Console log handler (colorized, clean output)
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
        colorize=True,
    )

    # Daily rotating general log handler
    general_log_path = log_dir / "app_{time:YYYY-MM-DD}.log"
    logger.add(
        str(general_log_path),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )

    # Daily rotating error-only log handler
    error_log_path = log_dir / "error_{time:YYYY-MM-DD}.log"
    logger.add(
        str(error_log_path),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}\n{exception}",
        level="ERROR",
        rotation="00:00",
        retention="60 days",
        compression="zip",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )

    logger.info("Loguru logging configured successfully.")
