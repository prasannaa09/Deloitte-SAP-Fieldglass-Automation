"""SAP Fieldglass home page readiness checks.

Sign-in lands on a home page that reports itself loaded before it is usable, so every session
verifies it here before doing anything else. What remains is only that check: the menu-driven
navigation this module used to carry belonged to the retired list-export flow, which drove the
UI to click Export and download a spreadsheet. The pipeline now addresses Fieldglass's own
endpoints directly and never touches the navigation menus.
"""

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

# Inspected element ID for the "Worker" navigation menu header. The home page is only ready
# once this has rendered, which is what it is checked for here.
WORKER_MENU_HEADER_SELECTOR = "#viewMenu_2_worker_header"

# Selector for loading overlays, spinners, or UI blocking layers
LOADING_OVERLAY_SELECTOR = ".sapUiBlockLayer, .loading-overlay, .busy-indicator, .spinner"

# Selector for the "No Available Card found" error/uninitialized message container
NO_CARD_FOUND_SELECTOR = "text='No Available Card found', :has-text('No Available Card found')"


class HomePageNotReadyError(Exception):
    """Custom exception raised when SAP Fieldglass home page fails to fully initialize."""

    pass


async def _check_home_page_state(page: Page, timeout: float) -> tuple[bool, str]:
    """Perform explicit validations to verify SAP Fieldglass home page initialization state.

    Args:
        page: Active Playwright Page instance.
        timeout: Timeout in milliseconds for explicit wait operations.

    Returns:
        tuple[bool, str]: (True, "") if ready, or (False, failure_reason) if incomplete.
    """
    try:
        # Step 1: Verify DOM content load state
        logger.info("Validation Step 1/4: Verifying page load state (domcontentloaded)...")
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)

        # Step 2: Check for uninitialized 'No Available Card found' indicator
        logger.info("Validation Step 2/4: Checking for 'No Available Card found' uninitialized state...")
        no_card_locator = page.locator(NO_CARD_FOUND_SELECTOR)
        if await no_card_locator.is_visible():
            return False, "'No Available Card found' message detected on home page"

        # Step 3: Verify presence, visibility, and interactability of 'Worker' menu header ID
        logger.info(f"Validation Step 3/4: Verifying 'Worker' menu header visibility ({WORKER_MENU_HEADER_SELECTOR})...")
        worker_header_locator = page.locator(WORKER_MENU_HEADER_SELECTOR)
        await worker_header_locator.wait_for(state="visible", timeout=timeout)
        if not await worker_header_locator.is_enabled():
            return False, f"'Worker' menu header ({WORKER_MENU_HEADER_SELECTOR}) is visible but disabled/not interactable"

        # Step 4: Verify no loading overlays or busy indicators are blocking interaction
        logger.info(f"Validation Step 4/4: Verifying absence of blocking loading overlays ({LOADING_OVERLAY_SELECTOR})...")
        overlay_locator = page.locator(LOADING_OVERLAY_SELECTOR)
        if await overlay_locator.count() > 0:
            logger.info("Loading overlay detected, waiting for overlay to disappear...")
            await overlay_locator.wait_for(state="hidden", timeout=timeout)

        return True, ""

    except PlaywrightTimeoutError as exc:
        sanitized_msg = str(exc).encode("ascii", "replace").decode("ascii")
        return False, f"Timeout during home page validation step: {sanitized_msg}"
    except Exception as exc:
        sanitized_msg = str(exc).encode("ascii", "replace").decode("ascii")
        return False, f"Unexpected error during home page validation: {sanitized_msg}"


async def ensure_home_page_ready(page: Page, timeout: float = 30000.0) -> None:
    """Verify that SAP Fieldglass Home page is fully loaded, initialized, and ready for navigation.

    If initialization checks fail (e.g. 'No Available Card found' or missing navigation elements),
    it performs exactly ONE page reload retry. If validation fails after retry, an exception is raised.

    Args:
        page: Active Playwright Page instance.
        timeout: Maximum timeout in milliseconds per validation check.

    Raises:
        HomePageNotReadyError: If the page remains uninitialized after one retry.
    """
    logger.info("Initiating SAP Fieldglass Home Page readiness check...")

    # Attempt 1: Initial check
    is_ready, reason = await _check_home_page_state(page, timeout=timeout)
    if is_ready:
        logger.success("SAP Fieldglass Home Page verified fully ready for navigation.")
        return

    # Handle retry logic: Perform exactly ONE page.reload()
    logger.warning(f"Home Page not ready on initial check: {reason}. Triggering page.reload() retry...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=timeout)
        logger.info("Page reloaded successfully. Re-running Home Page readiness validations...")
    except Exception as reload_exc:
        sanitized_msg = str(reload_exc).encode("ascii", "replace").decode("ascii")
        logger.error(f"Failed to reload page: {sanitized_msg}")

    # Attempt 2: Post-reload check
    is_ready_after_retry, retry_reason = await _check_home_page_state(page, timeout=timeout)
    if is_ready_after_retry:
        logger.success("SAP Fieldglass Home Page verified fully ready after page reload retry.")
        return

    # Validation failed after retry
    error_msg = (
        f"SAP Fieldglass Home Page failed to initialize after reload retry. "
        f"Failure reason: {retry_reason}"
    )
    logger.error(error_msg)
    raise HomePageNotReadyError(error_msg)
