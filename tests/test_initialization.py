"""Unit test module verifying project configuration and directory creation."""

from pathlib import Path

from config.settings import Settings, get_settings


def test_settings_initialization() -> None:
    """Verify Settings loads with default values and creates output directories."""
    settings: Settings = get_settings()

    assert settings.SAP_URL is not None
    assert settings.BROWSER_TYPE in ["chromium", "firefox", "webkit"]
    assert isinstance(settings.HEADLESS, bool)
    assert settings.DEFAULT_TIMEOUT > 0

    assert settings.DOWNLOAD_DIR.exists()
    assert settings.REPORT_DIR.exists()
    assert settings.SCREENSHOT_DIR.exists()
    assert settings.LOG_DIR.exists()
