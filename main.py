"""Clean entry point for SAP Fieldglass Automation Bot.

Initializes logging configuration, application settings, and Playwright browser,
then cleanly releases resources and exits.
"""

import asyncio
import sys

from loguru import logger

from automation.browser import PlaywrightManager
from config.settings import get_settings
from utils.logger import setup_logger


async def main() -> int:
    """Main application execution routine.

    Returns:
        int: Exit status code (0 for success, non-zero for failure).
    """
    # 1. Load application settings
    settings = get_settings()

    # 2. Configure Loguru logger
    setup_logger(settings)
    logger.info("==================================================")
    logger.info("SAP Fieldglass Automation Bot Initialization")
    logger.info("==================================================")
    logger.info(f"Base Directory: {settings.BASE_DIR}")
    logger.info(f"Configured SAP URL: {settings.SAP_URL}")
    logger.info(f"Browser Engine: {settings.BROWSER_TYPE} (Headless: {settings.HEADLESS})")
    logger.info(f"Download Directory: {settings.DOWNLOAD_DIR}")
    logger.info(f"Report Directory: {settings.REPORT_DIR}")

    # 3. Initialize and test Playwright Browser Session
    browser_manager = PlaywrightManager(settings=settings)
    try:
        await browser_manager.initialize()
        context = await browser_manager.create_context()
        page = await context.new_page()

        logger.info("Playwright browser, context, and page initialized successfully.")

        # Clean teardown of page and context
        await page.close()
        await context.close()
        logger.info("Playwright context and page closed cleanly.")

    except Exception as exc:
        logger.error(f"Failed to initialize Playwright browser session: {exc}")
        return 1
    finally:
        await browser_manager.close()

    logger.info("SAP Fieldglass Automation Bot foundation initialized and shutdown cleanly.")
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
