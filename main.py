"""Clean entry point for SAP Fieldglass Automation Bot - Development Mode & Login Module."""

import asyncio
import sys

from loguru import logger

from automation.browser import PlaywrightManager
from automation.login import authenticate_session
from config.settings import get_settings
from utils.helpers import get_previous_month_date_range
from utils.logger import setup_logger


async def main() -> int:
    """Main execution routine for SAP Fieldglass automation with session reuse and development mode.

    Returns:
        int: Exit status code (0 for success, non-zero for failure).
    """
    # 1. Load application settings
    settings = get_settings()

    # 2. Configure Loguru logger
    setup_logger(settings)
    logger.info("==================================================")
    logger.info("SAP Fieldglass Automation Bot - Session Execution")
    logger.info("==================================================")
    logger.info(f"Base Directory: {settings.BASE_DIR}")
    logger.info(f"Target SAP URL: {settings.SAP_URL}")
    logger.info(f"Browser: {settings.BROWSER_TYPE} (Headless: {settings.HEADLESS})")
    logger.info(f"Use Saved Session: {settings.USE_SAVED_SESSION} (Auth File: {settings.AUTH_FILE_PATH})")
    logger.info(f"Keep Browser Open: {settings.KEEP_BROWSER_OPEN}")

    # 3. Determine auth state to load
    storage_state_path = (
        settings.AUTH_FILE_PATH
        if settings.USE_SAVED_SESSION and settings.AUTH_FILE_PATH.exists()
        else None
    )

    # 4. Initialize Playwright Browser Session
    browser_manager = PlaywrightManager(settings=settings)
    try:
        await browser_manager.initialize()
        context = await browser_manager.create_context(storage_state=storage_state_path)
        page = await context.new_page()

        logger.info("Playwright session initialized successfully.")

        # 5. Authenticate session (reuse auth.json or fresh login)
        success, active_page = await authenticate_session(context=context, page=page, settings=settings)

        if not success:
            logger.error("Authentication failed. Cleaning up session.")
            await context.close()
            return 1

        # 6. Navigate to Time Sheet module and download report (previous calendar month)
        from automation.navigation import navigate_to_module
        from automation.downloads import download_timesheet_list_report, download_work_order_list_report
        from excel.cleaner import clean_timesheet_excel

        await navigate_to_module(active_page, "Timesheet")
        ts_downloaded_file = await download_timesheet_list_report(active_page, settings.DOWNLOAD_DIR)

        # 7. Navigate to Work Order module and download Work Order list for WO_ID mapping
        await navigate_to_module(active_page, "WorkOrder")
        wo_downloaded_file = await download_work_order_list_report(active_page, settings.DOWNLOAD_DIR)

        # 8. Clean Timesheet data, map WO_ID from Work Order list, and save InvoiceSheet.xlsx
        target_report_path = settings.REPORT_DIR / "InvoiceSheet.xlsx"

        if ts_downloaded_file and ts_downloaded_file.exists():
            logger.info("Processing Timesheet data and mapping Work Order IDs (WO_ID)...")
            cleaned_file = clean_timesheet_excel(
                input_path=ts_downloaded_file,
                output_path=target_report_path,
                wo_input_path=wo_downloaded_file,
            )
            logger.success(f"InvoiceSheet structure created at: {cleaned_file}")

        # 9. Process USI Invoice Billing Schedule to extract BillRate, Amount, Legal Entity, Comments into InvoiceSheet.xlsx
        from automation.invoice import process_invoice_billing_schedule
        if target_report_path.exists():
            logger.info("Enriching InvoiceSheet.xlsx with invoice detail metrics & comments...")
            final_report = await process_invoice_billing_schedule(active_page, target_report_path)
            logger.success(f"Final InvoiceSheet report successfully enriched at: {final_report}")

        # 10. Interactive Development REPL (keep browser open for live interaction)
        if settings.KEEP_BROWSER_OPEN:
            logger.info("==================================================")
            logger.info("  DEVELOPMENT MODE REPL (Browser Session Active)  ")
            logger.info("==================================================")
            logger.info("  Commands available in terminal:")
            logger.info("    run        (or 1) -> Execute full download & extraction pipeline")
            logger.info("    timesheet  (or 2) -> Navigate to Time Sheet list page")
            logger.info("    workorder  (or 3) -> Navigate to Work Order list page")
            logger.info("    invoice    (or 4) -> Run invoice billing schedule extraction")
            logger.info("    pdfs       (or 5) -> Download timesheet PDFs for previous month")
            logger.info("    data       (or 6) -> Extract timesheet data into PostgreSQL")
            logger.info("    both       (or 7) -> Extract data, then download PDFs (one list fetch)")
            logger.info("    reload     (or r) -> Reload page & verify home page readiness")
            logger.info("    pause      (or p) -> Open Playwright Inspector GUI live")
            logger.info("    exit       (or q) -> Close browser and exit session")
            logger.info("==================================================")

            while True:
                try:
                    cmd = await asyncio.to_thread(
                        input,
                        "\n[DEV MODE REPL] Enter action (run/timesheet/workorder/invoice/pdfs/reload/pause/exit): ",
                    )
                    cmd = cmd.strip().lower()

                    import importlib
                    import automation.navigation
                    import automation.downloads
                    import automation.invoice
                    import automation.timesheet_pdf
                    import automation.timesheet_data
                    importlib.reload(automation.navigation)
                    importlib.reload(automation.downloads)
                    importlib.reload(automation.invoice)
                    importlib.reload(automation.timesheet_pdf)
                    importlib.reload(automation.timesheet_data)

                    if cmd in ("exit", "q", "quit"):
                        logger.info("Closing development session...")
                        break
                    elif cmd in ("run", "download", "1"):
                        ts_file = await automation.downloads.download_timesheet_list_report(active_page, settings.DOWNLOAD_DIR)
                        wo_file = await automation.downloads.download_work_order_list_report(active_page, settings.DOWNLOAD_DIR)
                        if ts_file:
                            clean_timesheet_excel(ts_file, target_report_path, wo_input_path=wo_file)
                            await automation.invoice.process_invoice_billing_schedule(active_page, target_report_path)
                    elif cmd in ("invoice", "4"):
                        if target_report_path.exists():
                            await automation.invoice.process_invoice_billing_schedule(active_page, target_report_path)
                        else:
                            logger.warning(f"Target report {target_report_path} not found. Run pipeline first.")
                    elif cmd in ("pdfs", "pdf", "5"):
                        await automation.timesheet_pdf.download_month_timesheet_pdfs(
                            context=context, page=active_page, settings=settings
                        )
                    elif cmd in ("data", "6"):
                        await automation.timesheet_data.extract_month_timesheet_data(
                            context=context, page=active_page, settings=settings
                        )
                    elif cmd in ("both", "all", "7"):
                        # One list fetch feeds both jobs, and they run one after the other:
                        # in parallel they would double the load on Fieldglass, and the cheap
                        # data pass would be at the mercy of the long PDF run.
                        month_rows = await automation.timesheet_pdf.fetch_month_rows(
                            active_page, *get_previous_month_date_range()
                        )
                        await automation.timesheet_data.extract_month_timesheet_data(
                            context=context, page=active_page, settings=settings, rows=month_rows
                        )
                        await automation.timesheet_pdf.download_month_timesheet_pdfs(
                            context=context, page=active_page, settings=settings, rows=month_rows
                        )
                    elif cmd in ("timesheet", "timesheets", "2"):
                        await automation.navigation.navigate_to_module(active_page, "Timesheet")
                    elif cmd in ("workorder", "workorders", "3"):
                        await automation.navigation.navigate_to_module(active_page, "WorkOrder")
                    elif cmd in ("reload", "refresh", "r"):
                        logger.info("Reloading active page...")
                        await active_page.reload(wait_until="domcontentloaded")
                        await automation.navigation.ensure_home_page_ready(active_page)
                    elif cmd in ("pause", "p"):
                        logger.info("Launching Playwright Inspector live pause mode...")
                        await active_page.pause()
                    else:
                        logger.info(f"Attempting navigation to: {cmd}")
                        await automation.navigation.navigate_to_module(active_page, cmd)
                except (asyncio.CancelledError, KeyboardInterrupt, EOFError):
                    logger.info("Development session exited by user.")
                    break

    except KeyboardInterrupt:
        logger.info("Execution cancelled by user.")
    except Exception as exc:
        sanitized_msg = str(exc).encode("ascii", "replace").decode("ascii")
        logger.error(f"Unexpected error during automation execution: {sanitized_msg}")
        return 1
    finally:
        try:
            await browser_manager.close()
        except Exception as close_exc:
            logger.debug(f"Note during browser cleanup: {close_exc}")
        logger.info("Playwright session closed cleanly.")

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)
