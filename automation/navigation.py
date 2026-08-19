"""SAP Fieldglass Navigation and Page Readiness automation module."""

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# SAP FIELDGLASS INSPECTED SELECTORS
# ==============================================================================

# Inspected element ID for the "Worker" navigation menu header
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


async def open_worker_menu(page: Page, timeout: float = 30000.0) -> None:
    """Open the 'Worker' navigation menu item using recorded role locator with CSS fallback.

    Args:
        page: Active Playwright Page instance.
        timeout: Maximum timeout in milliseconds for explicit wait operations.
    """
    logger.info("Opening 'Worker' navigation menu...")
    worker_item = page.get_by_role("treeitem", name="Worker")

    if await worker_item.count() > 0 and await worker_item.first.is_visible():
        await worker_item.first.click(timeout=timeout)
        logger.success("Clicked 'Worker' treeitem menu.")
        return

    # Fallback to inspected CSS selector
    worker_header = page.locator(WORKER_MENU_HEADER_SELECTOR)
    await worker_header.wait_for(state="visible", timeout=timeout)
    await worker_header.click(timeout=timeout)
    logger.success("Clicked 'Worker' menu via selector fallback.")


# Inspected selector for the "Time Sheet" / "Timesheet" sub-menu item under Worker menu
TIMESHEET_MENU_SELECTOR = "#viewMenu_2_timesheet, a:has-text('Time Sheet'), a:has-text('Time Sheets'), a:has-text('Timesheets')"


async def open_timesheet_menu(page: Page, timeout: float = 30000.0) -> None:
    """Open the 'Time Sheet' sub-menu item under 'Worker' navigation menu.

    Args:
        page: Active Playwright Page instance.
        timeout: Maximum timeout in milliseconds for explicit wait operations.
    """
    logger.info("Navigating to 'Time Sheet' menu...")
    timesheet_treeitem = page.get_by_role("treeitem", name="Time Sheet")

    # 1. Try recorded treeitem locator directly if visible
    if await timesheet_treeitem.count() > 0 and await timesheet_treeitem.first.is_visible():
        await timesheet_treeitem.first.click(timeout=timeout)
        logger.success("Clicked 'Time Sheet' treeitem navigation menu item.")
        return

    # 2. Otherwise expand Worker menu first
    logger.info("'Time Sheet' menu item not yet visible; expanding 'Worker' menu header...")
    await open_worker_menu(page, timeout=timeout)

    # Re-check treeitem locator after expanding menu
    if await timesheet_treeitem.count() > 0 and await timesheet_treeitem.first.is_visible():
        await timesheet_treeitem.first.click(timeout=timeout)
        logger.success("Clicked 'Time Sheet' treeitem navigation menu item after expansion.")
        return

    # 3. Fallback to CSS selector or direct URL
    timesheet_locator = page.locator(TIMESHEET_MENU_SELECTOR).first
    if await timesheet_locator.count() > 0:
        await timesheet_locator.click(timeout=timeout)
        logger.success("Clicked 'Time Sheet' via CSS selector.")
    else:
        logger.info("Directly navigating to Time Sheet URL fallback...")
        await page.goto("https://www.us.fieldglass.cloud.sap/time_sheet_list.do?cf=1", wait_until="domcontentloaded")


async def open_work_order_menu(page: Page, timeout: float = 30000.0) -> None:
    """Open the 'Work Order' sub-menu / treeitem navigation item.

    Args:
        page: Active Playwright Page instance.
        timeout: Maximum timeout in milliseconds for explicit wait operations.
    """
    logger.info("Navigating to 'Work Order' menu...")
    wo_treeitem = page.get_by_role("treeitem", name="Work Order")

    if await wo_treeitem.count() > 0 and await wo_treeitem.first.is_visible():
        await wo_treeitem.first.click(timeout=timeout)
        logger.success("Clicked 'Work Order' treeitem navigation menu item.")
        return

    # Fallback if Worker menu needs expanding or direct URL navigation
    logger.info("'Work Order' treeitem not immediately visible; expanding 'Worker' menu...")
    await open_worker_menu(page, timeout=timeout)

    if await wo_treeitem.count() > 0 and await wo_treeitem.first.is_visible():
        await wo_treeitem.first.click(timeout=timeout)
        logger.success("Clicked 'Work Order' treeitem menu after expansion.")
        return

    logger.info("Navigating to Work Order list URL fallback...")
    await page.goto("https://www.us.fieldglass.cloud.sap/work_order_list.do?cf=1", wait_until="domcontentloaded")


async def navigate_to_module(page: Page, module_name: str) -> None:
    """Navigate to a specific target module within SAP Fieldglass.

    Ensures the Home Page is fully initialized before attempting navigation.

    Args:
        page: Playwright Page instance.
        module_name: Target module identifier (e.g., 'Worker', 'Timesheets', 'WorkOrder').
    """
    await ensure_home_page_ready(page)
    logger.info(f"Navigating to SAP Fieldglass module: {module_name}")

    normalized_name = module_name.strip().lower()

    if normalized_name in ("worker", "1"):
        await open_worker_menu(page)
    elif normalized_name in ("timesheet", "timesheets", "time sheet", "time sheets", "2"):
        await open_timesheet_menu(page)
    elif normalized_name in ("workorder", "work order", "work orders", "3"):
        await open_work_order_menu(page)
    else:
        logger.warning(f"Navigation handler for module '{module_name}' is not yet implemented.")

