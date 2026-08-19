"""SAP Fieldglass Download automation module for exporting Timesheets and reports."""

from pathlib import Path
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from utils.helpers import get_previous_month_date_range, generate_timestamp_filename


async def download_timesheet_list_report(
    page: Page,
    output_dir: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Path | None:
    """Filter time sheet list by date range (defaults to previous calendar month) and download list data.

    Args:
        page: Active Playwright Page instance.
        output_dir: Destination directory for downloaded files.
        start_date: Optional start date in 'DD/MM/YYYY' format. Calculated automatically if None.
        end_date: Optional end date in 'DD/MM/YYYY' format. Calculated automatically if None.

    Returns:
        Path | None: Filepath of the downloaded document if successful, None otherwise.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Compute dynamic previous month date range if not explicitly provided
    if not start_date or not end_date:
        calc_start, calc_end = get_previous_month_date_range()
        start_date = start_date or calc_start
        end_date = end_date or calc_end

    logger.info(f"Initiating Timesheet list export for date range: {start_date} to {end_date}")

    try:
        # Ensure we are on the Timesheet list page
        if "time_sheet_list.do" not in page.url:
            logger.info("Navigating directly to Time Sheet list page...")
            await page.goto("https://www.us.fieldglass.cloud.sap/time_sheet_list.do?cf=1", wait_until="domcontentloaded")

        # Ensure page and JavaScript filters UI are fully settled before filling dates
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2500)

        # 2. Fill Start Date input
        logger.info(f"Setting Start Date filter: {start_date}")
        start_box = page.get_by_role("textbox", name="Start")
        try:
            await start_box.first.wait_for(state="visible", timeout=15000)
        except Exception:
            start_box = page.locator("input[title*='Start'], input[aria-label*='Start'], input[name*='start'], input[id*='start']")
            await start_box.first.wait_for(state="visible", timeout=15000)

        await start_box.first.click()
        await start_box.first.fill("")
        await start_box.first.type(start_date, delay=40)
        await start_box.first.dispatch_event("change")
        await start_box.first.dispatch_event("blur")
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(800)

        # 3. Fill End Date input
        logger.info(f"Setting End Date filter: {end_date}")
        end_box = page.get_by_role("textbox", name="End")
        try:
            await end_box.first.wait_for(state="visible", timeout=10000)
        except Exception:
            end_box = page.locator("input[title*='End'], input[aria-label*='End'], input[name*='end'], input[id*='end']")
            await end_box.first.wait_for(state="visible", timeout=10000)

        await end_box.first.click()
        await end_box.first.fill("")
        await end_box.first.type(end_date, delay=30)
        await end_box.first.dispatch_event("change")
        await end_box.first.dispatch_event("blur")
        await page.keyboard.press("Tab")

        # 4. Click Apply Filters button
        logger.info("Applying date filters...")
        apply_btn = page.get_by_role("button", name="Apply Filters")
        try:
            await apply_btn.first.wait_for(state="visible", timeout=5000)
        except Exception:
            apply_btn = page.locator("input[value='Apply Filters'], button:has-text('Apply Filters'), [data-fgid*='applyfilter']")

        # If button is still disabled, force trigger form submit via Enter key
        if await apply_btn.first.is_disabled():
            logger.info("Apply Filters button is disabled; submitting form via Enter key...")
            await end_box.first.press("Enter")
        else:
            await apply_btn.first.click(force=True)

        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        logger.info("Filters applied successfully. Triggering download...")

        # 5. Click Download List Data link and capture download event
        download_link = page.get_by_role("link", name="Download List Data")
        if await download_link.count() == 0:
            download_link = page.locator("a:has-text('Download List Data'), a[title*='Download']")

        async with page.expect_download(timeout=60000) as download_info:
            await download_link.first.click()

        download = await download_info.value
        suggested_name = download.suggested_filename or generate_timestamp_filename("timesheet_export", "xlsx")
        target_path = output_dir / suggested_name

        await download.save_as(str(target_path))
        logger.success(f"Timesheet report downloaded successfully: {target_path}")
        return target_path

    except PlaywrightTimeoutError as exc:
        logger.error(f"Timeout occurred while downloading timesheet report: {exc}")
        return None
    except Exception as exc:
        logger.error(f"Failed to download timesheet report: {exc}")
        return None


async def download_work_order_list_report(page: Page, output_dir: Path) -> Path | None:
    """Navigate to Work Order list and download list data for mapping resource IDs.

    Args:
        page: Active Playwright Page instance.
        output_dir: Destination directory for downloaded files.

    Returns:
        Path | None: Filepath of the downloaded Work Order document if successful, None otherwise.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Initiating Work Order list export...")

    try:
        # 1. Click Work Order treeitem menu item
        wo_treeitem = page.get_by_role("treeitem", name="Work Order")
        if await wo_treeitem.count() > 0 and await wo_treeitem.first.is_visible():
            await wo_treeitem.first.click()
        else:
            from automation.navigation import open_work_order_menu
            await open_work_order_menu(page)

        await page.wait_for_load_state("domcontentloaded", timeout=15000)

        # 2. Click Download List Data link and capture download event
        download_link = page.get_by_role("link", name="Download List Data")
        if await download_link.count() == 0:
            download_link = page.locator("a:has-text('Download List Data'), a[title*='Download']")

        logger.info("Triggering Work Order list download...")
        async with page.expect_download(timeout=60000) as download_info:
            await download_link.first.click()

        download = await download_info.value
        suggested_name = download.suggested_filename or "work_order.supplier.list.csv"
        target_path = output_dir / suggested_name

        await download.save_as(str(target_path))
        logger.success(f"Work Order list report downloaded successfully: {target_path}")
        return target_path

    except PlaywrightTimeoutError as exc:
        logger.error(f"Timeout occurred while downloading Work Order list report: {exc}")
        return None
    except Exception as exc:
        logger.error(f"Failed to download Work Order list report: {exc}")
        return None


