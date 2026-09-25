"""Settings, read from this folder's .env (falls back to the parent repo's .env during development)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv

PKG_ROOT = Path(__file__).resolve().parent.parent

for candidate in (PKG_ROOT / ".env", PKG_ROOT.parent / ".env"):
    if candidate.exists():
        load_dotenv(candidate, override=False)
        break


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    username: str = field(default_factory=lambda: _env("COGNIZANT_ARIBA_USERNAME"))
    password: str = field(default_factory=lambda: _env("COGNIZANT_ARIBA_PASSWORD"), repr=False)
    login_url: str = field(default_factory=lambda: _env("ARIBA_LOGIN_URL", "https://service.ariba.com/Supplier.aw"))
    excel_path: Path = field(default_factory=lambda: Path(_env("INVOICE_EXCEL_PATH", r"D:\cognizant\Excel of cognizant.xlsx")))
    excel_sheet: str = field(default_factory=lambda: _env("INVOICE_EXCEL_SHEET", "Excel tracker"))
    pdf_dir: Path = field(default_factory=lambda: Path(_env("INVOICE_PDF_DIR", r"D:\cognizant\SupportingPDFs")))
    service_description: str = field(default_factory=lambda: _env("SERVICE_DESCRIPTION", "Man Power Industry"))
    sac_code: str = field(default_factory=lambda: _env("SAC_CODE", "998313"))
    comment_template: str = field(default_factory=lambda: _env("COMMENT_TEMPLATE", "Being invoiced Raised for the month of {month}"))
    client_timezone: str = field(default_factory=lambda: _env("CLIENT_TIMEZONE", "Asia/Calcutta"))
    headless: bool = field(default_factory=lambda: _env("HEADLESS", "false").lower() == "true")
    timeout_ms: float = field(default_factory=lambda: float(_env("ARIBA_TIMEOUT_MS", "240000")))
    report_dir: Path = field(default_factory=lambda: PKG_ROOT / "reports")
    log_dir: Path = field(default_factory=lambda: PKG_ROOT / "logs")

    def require_credentials(self) -> None:
        if not self.username or not self.password:
            raise SystemExit("Set COGNIZANT_ARIBA_USERNAME and COGNIZANT_ARIBA_PASSWORD in .env")
