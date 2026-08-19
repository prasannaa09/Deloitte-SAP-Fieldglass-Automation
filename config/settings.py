"""Configuration settings module for SAP Fieldglass Automation Bot using Pydantic Settings."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # SAP Fieldglass Credentials & Core Configuration
    SAP_URL: str = Field(
        default="https://www.us.fieldglass.cloud.sap/", description="SAP Fieldglass login URL"
    )
    SAP_USERNAME: str = Field(default="", description="SAP Fieldglass username")
    SAP_PASSWORD: str = Field(default="", description="SAP Fieldglass password")

    # Directory Paths
    BASE_DIR: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    DOWNLOAD_DIR: Path = Field(
        default=Path("downloads"), description="Directory for downloaded files"
    )
    REPORT_DIR: Path = Field(default=Path("reports"), description="Directory for generated reports")
    SCREENSHOT_DIR: Path = Field(
        default=Path("screenshots"), description="Directory for failure screenshots"
    )
    LOG_DIR: Path = Field(default=Path("logs"), description="Directory for application log files")

    # Playwright Settings
    BROWSER_TYPE: str = Field(
        default="chromium", description="Browser type: chromium, firefox, or webkit"
    )
    HEADLESS: bool = Field(default=False, description="Run browser in headless mode")
    SLOW_MO: float = Field(
        default=1000.0, description="Slow motion delay in ms between Playwright actions for visual inspection"
    )
    DEFAULT_TIMEOUT: float = Field(
        default=30000.0, description="Default timeout for Playwright operations in ms"
    )

    # PostgreSQL Connection Settings
    PG_HOST: str = Field(default="localhost", description="PostgreSQL host")
    PG_PORT: int = Field(default=5432, description="PostgreSQL port")
    PG_USER: str = Field(default="postgres", description="PostgreSQL user")
    PG_PASSWORD: str = Field(default="", description="PostgreSQL password")
    PG_DATABASE: str = Field(default="fieldglass", description="PostgreSQL database name")

    # Session & Development Settings
    USE_SAVED_SESSION: bool = Field(
        default=True, description="Reuse saved auth.json session state if valid"
    )
    AUTH_FILE_PATH: Path = Field(
        default=Path("auth.json"), description="File path for stored authentication state"
    )
    KEEP_BROWSER_OPEN: bool = Field(
        default=True, description="Keep browser open until Enter key is pressed in terminal"
    )

    def resolve_paths(self) -> None:
        """Ensure path attributes are absolute paths resolved relative to BASE_DIR."""
        if not self.AUTH_FILE_PATH.is_absolute():
            object.__setattr__(self, "AUTH_FILE_PATH", (self.BASE_DIR / self.AUTH_FILE_PATH).resolve())
        if not self.DOWNLOAD_DIR.is_absolute():
            object.__setattr__(self, "DOWNLOAD_DIR", (self.BASE_DIR / self.DOWNLOAD_DIR).resolve())
        if not self.REPORT_DIR.is_absolute():
            object.__setattr__(self, "REPORT_DIR", (self.BASE_DIR / self.REPORT_DIR).resolve())
        if not self.SCREENSHOT_DIR.is_absolute():
            object.__setattr__(
                self, "SCREENSHOT_DIR", (self.BASE_DIR / self.SCREENSHOT_DIR).resolve()
            )
        if not self.LOG_DIR.is_absolute():
            object.__setattr__(self, "LOG_DIR", (self.BASE_DIR / self.LOG_DIR).resolve())

    def create_required_directories(self) -> None:
        """Create output directories if they do not exist."""
        self.resolve_paths()
        for directory in [self.DOWNLOAD_DIR, self.REPORT_DIR, self.SCREENSHOT_DIR, self.LOG_DIR]:
            directory.mkdir(parents=True, exist_ok=True)


_settings_instance: Settings | None = None


def get_settings() -> Settings:
    """Retrieve singleton Settings instance with resolved paths and directories created."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
        _settings_instance.create_required_directories()
    return _settings_instance
