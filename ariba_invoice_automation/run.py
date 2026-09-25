"""Create a Cognizant SAP Ariba Standard Invoice as a DRAFT from the Excel tracker.

    python run.py --invoice DI-27-4909 --suffix a            # creates draft DI-27-4909a
    python run.py --invoice DI-27-4909 --suffix a --dry-run  # full run, then discards instead of saving
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

from ariba_invoice.aribaweb import AribaWebSession
from ariba_invoice.config import Settings
from ariba_invoice.errors import FlowAbort, SessionExpired, clean_error, is_transient
from ariba_invoice.excel_input import find_pdf, load_invoice
from ariba_invoice.flow import Run, create_draft_invoice
from ariba_invoice.portal import PortalApi, login_with_retries

MAX_ATTEMPTS = 3  # whole-flow attempts (fresh login each) after an expired session or unrecovered transient error


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--invoice", required=True, help="Tax invoice number as in the Excel tracker (column E)")
    p.add_argument("--suffix", default="", help="append to the invoice number on the portal (e.g. 'a' for test drafts)")
    p.add_argument("--pdf", help="explicit PDF path (default: <INVOICE_PDF_DIR>/<invoice><suffix>.pdf, else <invoice>.pdf)")
    p.add_argument("--month", help="override the billing month from the Excel, e.g. \"Sep'26\" (testing / corrections)")
    p.add_argument("--dry-run", action="store_true", help="go through every step, then discard instead of saving")
    p.add_argument("--keep-open", type=int, default=0, help="seconds to keep the browser open on the PO afterwards")
    return p.parse_args()


def setup_logging(s: Settings, stamp: str) -> None:
    # diagnose/backtrace off: tracebacks must never print local variables (credentials, tokens, cookies)
    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
               level="INFO", diagnose=False, backtrace=False)
    logger.add(s.log_dir / f"run_{stamp}.log", level="DEBUG", encoding="utf-8", diagnose=False, backtrace=False)


def log_failure(e: BaseException) -> None:
    """Secret-free failure log: cleaned message + stack frames only (Playwright messages carry cookies)."""
    logger.error(f"FAILED: {clean_error(e)}")
    logger.debug("".join(traceback.format_tb(e.__traceback__)))


async def main() -> int:
    args = parse_args()
    s = Settings()
    s.log_dir.mkdir(parents=True, exist_ok=True)
    s.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(s, stamp)

    try:
        inv = load_invoice(s.excel_path, s.excel_sheet, args.invoice)
        inv.gst_plan  # validates the CGST/SGST/IGST combination up front
        if args.month:
            from dataclasses import replace
            from ariba_invoice.excel_input import _month_start
            inv = replace(inv, month_label=args.month, month_start=_month_start(args.month))
        invoice_number = f"{args.invoice}{args.suffix}"
        pdf = Path(args.pdf) if args.pdf else find_pdf(s.pdf_dir, invoice_number, args.invoice)
        if not pdf.is_file():
            raise FileNotFoundError(pdf)
    except (ValueError, FileNotFoundError, KeyError) as e:
        logger.error(f"Input problem: {e}")
        return 2
    comment = s.comment_template.format(month=inv.month_label)
    logger.info(f"Invoice {invoice_number} | PO {inv.po_number} | month {inv.month_label} ({inv.month_start}) | "
                f"amount {inv.amount:,.2f} | tax {inv.expected_tax:,.2f} → {inv.gst_category} | PDF {pdf.name}"
                f"{' | DRY RUN' if args.dry_run else ''}")

    t0 = time.perf_counter()
    report: dict = {"started": datetime.now().isoformat(timespec="seconds"),
                    "input": inv.__dict__ | {"gst_category": inv.gst_category},
                    "invoice_number": invoice_number, "pdf": str(pdf), "dry_run": args.dry_run, "attempts": []}
    code, run, web = 1, Run(), None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=s.headless, args=["--start-maximized"])
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                existing_ok = bool(run.result.get("save_attempted"))  # a previous attempt may already have saved
                run, web = Run(), None
                att: dict = {"attempt": attempt}
                report["attempts"].append(att)
                context = await browser.new_context(no_viewport=True)
                try:
                    t_login = time.perf_counter()
                    logger.info(f"▶ 0. Login (browser){f' — attempt {attempt}/{MAX_ATTEMPTS}' if attempt > 1 else ''}")
                    page = await login_with_retries(context, s)
                    att["login_seconds"] = round(time.perf_counter() - t_login, 2)
                    logger.success(f"✔ 0. Login ({att['login_seconds']}s)")

                    api = PortalApi(context, page, s)
                    web = AribaWebSession(context.request, s.timeout_ms, s.client_timezone)
                    await create_draft_invoice(web, api, context, inv, invoice_number, pdf, s.service_description,
                                               s.sac_code, comment, run, save=not args.dry_run, existing_ok=existing_ok)
                    att |= {"steps": run.steps, "aribaweb_requests": web.requests}
                    report["result"] = run.result
                    code = 0
                    if args.keep_open and not s.headless:
                        await page.goto(api.document_url(run.result["payload_id"]), wait_until="domcontentloaded")
                        await asyncio.sleep(args.keep_open)
                    break
                except FlowAbort as e:
                    logger.error(f"ABORTED (nothing submitted): {e}")
                    if web is not None and web.page is not None:
                        (s.log_dir / f"last_page_{stamp}_a{attempt}.html").write_text(web.page.html, encoding="utf-8")
                        logger.info(f"page at abort saved to logs/last_page_{stamp}_a{attempt}.html")
                    att |= {"steps": run.steps, "aborted": str(e)}
                    report["result"] = run.result | {"status": f"aborted: {e}"}
                    break
                except Exception as e:  # noqa: BLE001
                    log_failure(e)
                    att |= {"steps": run.steps, "error": clean_error(e)}
                    report["result"] = run.result | {"status": f"error: {clean_error(e)}"}
                    if web is not None and web.page is not None:
                        (s.log_dir / f"last_page_{stamp}_a{attempt}.html").write_text(web.page.html, encoding="utf-8")
                    if not (isinstance(e, SessionExpired) or is_transient(e)) or attempt == MAX_ATTEMPTS:
                        break
                    logger.warning(f"{'Session expired' if isinstance(e, SessionExpired) else 'Transient failure'} — "
                                   f"logging in again and resuming (attempt {attempt + 1}/{MAX_ATTEMPTS})")
                finally:
                    await context.close()
        finally:
            await browser.close()

    report["total_seconds"] = round(time.perf_counter() - t0, 2)
    last = report["attempts"][-1] if report["attempts"] else {}
    report["invoice_seconds"] = round(sum(x["seconds"] for x in last.get("steps", [])), 2)
    out = s.report_dir / f"run_{invoice_number}_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    logger.info("-" * 78)
    for a in report["attempts"]:
        if len(report["attempts"]) > 1:
            logger.info(f"  attempt {a['attempt']}: login {a.get('login_seconds', '-')}s")
        for st in a.get("steps", []):
            logger.info(f"  {st['seconds']:8.2f}s  {'OK ' if st['ok'] else 'ERR'}  {st['step']}"
                        f"{'  [' + st['note'] + ']' if st.get('note') else ''}")
    logger.info(f"  login {last.get('login_seconds', '-')}s | invoice steps {report['invoice_seconds']}s | "
                f"total {report['total_seconds']}s | status: {report.get('result', {}).get('status', 'failed')}")
    logger.info(f"Report: {out}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
