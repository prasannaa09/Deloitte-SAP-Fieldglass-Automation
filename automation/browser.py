"""Playwright browser management module providing configurable context creation and lifecycle control."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from config.settings import Settings, get_settings


class PlaywrightManager:
    """Manager for Playwright browser initialization, context configuration, and cleanup."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize PlaywrightManager with application settings.

        Args:
            settings: Settings instance. Defaults to global settings if None.
        """
        self.settings: Settings = settings or get_settings()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def initialize(self) -> None:
        """Initialize Playwright and launch the configured browser instance."""
        logger.info(
            f"Initializing Playwright browser: {self.settings.BROWSER_TYPE} (headless={self.settings.HEADLESS})"
        )
        self._playwright = await async_playwright().start()

        browser_type_name = self.settings.BROWSER_TYPE.lower()
        if browser_type_name == "firefox":
            browser_type = self._playwright.firefox
        elif browser_type_name == "webkit":
            browser_type = self._playwright.webkit
        else:
            browser_type = self._playwright.chromium

        download_path = self.settings.DOWNLOAD_DIR.resolve()
        download_path.mkdir(parents=True, exist_ok=True)

        launch_args = ["--start-maximized"]
        if browser_type_name == "chromium":
            launch_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])

        self._browser = await browser_type.launch(
            headless=self.settings.HEADLESS,
            slow_mo=self.settings.SLOW_MO,
            downloads_path=str(download_path),
            args=launch_args,
        )
        logger.info(f"Browser {self.settings.BROWSER_TYPE} launched successfully (maximized).")

    async def create_context(
        self, storage_state: Path | str | None = None
    ) -> BrowserContext:
        """Create a new browser context configured with download directory, default timeouts, and optional auth storage state.

        Args:
            storage_state: Optional file path to saved auth storage state JSON.

        Returns:
            BrowserContext: Configured browser context instance.
        """
        if not self._browser:
            raise RuntimeError("Browser is not initialized. Call initialize() first.")

        kwargs = {
            "accept_downloads": True,
            "no_viewport": True,
        }

        if storage_state:
            state_path = Path(storage_state)
            if state_path.exists():
                kwargs["storage_state"] = str(state_path)
                logger.info(f"Using saved session state from: {state_path}")

        context = await self._browser.new_context(**kwargs)
        context.set_default_timeout(self.settings.DEFAULT_TIMEOUT)
        logger.info("Browser context created successfully.")
        return context

    async def close(self) -> None:
        """Close browser instance and stop Playwright engine cleanly."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            logger.info("Browser instance closed.")
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
            logger.info("Playwright engine stopped.")


@asynccontextmanager
async def get_browser_session(
    settings: Settings | None = None,
) -> AsyncGenerator[tuple[BrowserContext, Page], None]:
    """Async context manager to provide a managed browser context and page session.

    Args:
        settings: Application settings.

    Yields:
        tuple[BrowserContext, Page]: Active context and primary page.
    """
    manager = PlaywrightManager(settings=settings)
    await manager.initialize()
    context = await manager.create_context()
    page = await context.new_page()
    try:
        yield context, page
    finally:
        await page.close()
        await context.close()
        await manager.close()
