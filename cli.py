"""Command line entry point for the SAP Fieldglass timesheet pipeline.

    python cli.py check                 verify environment before a run
    python cli.py data                  extract a month into PostgreSQL
    python cli.py pdfs                  download approved timesheet PDFs
    python cli.py both                  data then PDFs, sharing one list fetch
    python cli.py merge                 combine weekly PDFs into one per resource
    python cli.py export                write the Excel workbook
    python cli.py study                 data quality report over what is stored

Every command defaults to the previous calendar month; pass --month MM/YYYY for another.
"""

import argparse
import asyncio
import calendar
import sys
from datetime import date
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page

from automation.browser import PlaywrightManager
from automation.login import authenticate_session
from automation.timesheet_data import extract_month_timesheet_data
from automation.timesheet_merge import merge_month_by_worker
from automation.timesheet_pdf import (
    DownloadSummary,
    TimesheetRow,
    download_month_timesheet_pdfs,
    fetch_month_rows,
)
from config.settings import Settings, get_settings
from db import postgres
from utils.helpers import get_previous_month_date_range
from utils.logger import setup_logger


def resolve_month(month: str | None) -> tuple[str, str]:
    """Turn 'MM/YYYY' into the range 'DD/MM/YYYY' .. 'DD/MM/YYYY'; default previous month."""
    if not month:
        return get_previous_month_date_range()
    try:
        month_num, year = (int(part) for part in month.split("/"))
        last_day = calendar.monthrange(year, month_num)[1]
        return f"01/{month_num:02d}/{year}", f"{last_day}/{month_num:02d}/{year}"
    except (ValueError, calendar.IllegalMonthError) as exc:
        raise SystemExit(f"--month must look like 07/2026 (got {month!r})") from exc


async def _session(settings: Settings) -> tuple[PlaywrightManager, BrowserContext, Page] | None:
    """Open a browser context and authenticate. Returns (manager, context, page)."""
    manager = PlaywrightManager(settings=settings)
    await manager.initialize()
    storage = settings.AUTH_FILE_PATH if settings.AUTH_FILE_PATH.exists() else None
    context = await manager.create_context(storage_state=storage)
    page = await context.new_page()

    ok, page = await authenticate_session(context=context, page=page, settings=settings)
    if not ok:
        logger.error("Authentication failed - check SAP_USERNAME / SAP_PASSWORD in .env")
        await manager.close()
        return None
    return manager, context, page


# --------------------------------------------------------------------------- check
async def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    """Verify database, schema and portal access before committing to a long run."""
    failures = 0

    logger.info("1/3  PostgreSQL connection...")
    ok, info = postgres.check_connection(settings)
    if ok:
        logger.success(f"     connected: {info[:60]}")
    else:
        # A missing database is not a failure - it is created on first run
        if "does not exist" in info:
            logger.warning(f"     database '{settings.PG_DATABASE}' not created yet "
                           f"(it will be created on the first run)")
        else:
            logger.error(f"     {info}")
            failures += 1

    logger.info("2/3  Schema...")
    try:
        postgres.ensure_schema(settings)
        import psycopg
        with psycopg.connect(**postgres.connection_kwargs(settings)) as conn, conn.cursor() as cur:
            for table in ("timesheets", "timesheet_days", "timesheet_rates", "timesheet_comments"):
                cur.execute(f"SELECT count(*) FROM {table}")
                row = cur.fetchone()
                logger.success(f"     {table:<20} {(row[0] if row else 0):>6} rows")
    except Exception as exc:
        logger.error(f"     {str(exc).splitlines()[0]}")
        failures += 1

    logger.info("3/3  SAP Fieldglass session...")
    session = await _session(settings)
    if session is None:
        failures += 1
    else:
        manager, _, page = session
        logger.success(f"     signed in: {await page.title()}")
        await manager.close()

    if failures:
        logger.error(f"{failures} check(s) failed.")
    else:
        logger.success("All checks passed - ready to run.")
    return 1 if failures else 0


# --------------------------------------------------------------------------- data
async def cmd_data(args: argparse.Namespace, settings: Settings) -> int:
    start_date, end_date = resolve_month(args.month)
    session = await _session(settings)
    if session is None:
        return 1
    manager, context, page = session
    try:
        summary = await extract_month_timesheet_data(
            context=context, page=page, settings=settings,
            start_date=start_date, end_date=end_date,
            concurrency=args.concurrency, limit=args.limit,
        )
        return 1 if summary.failed else 0
    finally:
        await manager.close()


# --------------------------------------------------------------------------- pdfs
async def cmd_pdfs(args: argparse.Namespace, settings: Settings) -> int:
    start_date, end_date = resolve_month(args.month)
    session = await _session(settings)
    if session is None:
        return 1
    manager, context, page = session
    try:
        summary = await download_month_timesheet_pdfs(
            context=context, page=page, settings=settings,
            start_date=start_date, end_date=end_date,
            concurrency=args.concurrency, limit=args.limit,
        )
        failed = 1 if summary.failed else 0
    finally:
        await manager.close()

    # After the browser is closed: the merge is offline, and holding a session open through it
    # buys nothing.
    if args.merge:
        failed |= _merge_after_download(summary, settings, args)
    return failed


# --------------------------------------------------------------------------- both
async def cmd_both(args: argparse.Namespace, settings: Settings) -> int:
    """Data first, then PDFs, over a single list fetch.

    Sequential rather than parallel: running both at once doubles the load on Fieldglass, and
    the cheap data pass should not be held hostage to the long document run.
    """
    start_date, end_date = resolve_month(args.month)
    session = await _session(settings)
    if session is None:
        return 1
    manager, context, page = session
    try:
        rows = await fetch_month_rows(page, start_date, end_date)

        data_summary = await extract_month_timesheet_data(
            context=context, page=page, settings=settings, rows=rows,
            start_date=start_date, end_date=end_date,
            concurrency=args.concurrency, limit=args.limit,
        )
        pdf_summary = await download_month_timesheet_pdfs(
            context=context, page=page, settings=settings, rows=rows,
            start_date=start_date, end_date=end_date,
            concurrency=args.concurrency, limit=args.limit,
        )
        failed = 1 if (data_summary.failed or pdf_summary.failed) else 0
    finally:
        await manager.close()

    if args.merge:
        failed |= _merge_after_download(pdf_summary, settings, args, rows=rows)
    return failed


def _merge_after_download(summary: DownloadSummary, settings: Settings,
                          args: argparse.Namespace,
                          rows: list[TimesheetRow] | None = None) -> int:
    """Fold the freshly downloaded weekly PDFs into one document per resource."""
    output_dir = summary.output_dir
    if output_dir is None:
        logger.warning("No download directory to merge.")
        return 0
    result = merge_month_by_worker(
        month_dir=output_dir, settings=settings, rows=rows,
        add_bookmarks=not args.no_bookmarks)
    return 1 if result.failed else 0


# --------------------------------------------------------------------------- merge
async def cmd_merge(args: argparse.Namespace, settings: Settings) -> int:
    """Merge a month of weekly PDFs into one document per resource.

    Purely local: no browser and no portal session, so it can be re-run against a month that is
    already on disk - which is exactly what you want after a re-run has collected timesheets
    that have since cleared approval.
    """
    start_date, _ = resolve_month(args.month)
    _, month_num, year = start_date.split("/")
    month_dir = args.dir or (settings.DOWNLOAD_DIR / "timesheets" / year / month_num)

    if not month_dir.is_dir():
        logger.error(f"No such month directory: {month_dir}")
        return 1

    summary = merge_month_by_worker(
        month_dir=month_dir, settings=settings, add_bookmarks=not args.no_bookmarks)
    return 1 if summary.failed else 0


# --------------------------------------------------------------------------- export
async def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    from db.export import export_month_to_excel

    start_date, _ = resolve_month(args.month)
    day, month_num, year = start_date.split("/")
    period_start = date(int(year), int(month_num), 1)
    last_day = calendar.monthrange(int(year), int(month_num))[1]
    period_end = date.fromordinal(date(int(year), int(month_num), last_day).toordinal() + 7)

    path = export_month_to_excel(
        settings, output_path=args.out, period_start=period_start, period_end=period_end)
    logger.success(f"Workbook: {path}")
    return 0


# --------------------------------------------------------------------------- study
async def cmd_study(args: argparse.Namespace, settings: Settings) -> int:
    """Report coverage, completeness and integrity over what is currently stored."""
    import psycopg

    with psycopg.connect(**postgres.connection_kwargs(settings)) as conn, conn.cursor() as cur:
        def scalar(sql: str) -> object:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

        print("\n== COVERAGE ==")
        for label, sql in (
            ("timesheets", "SELECT count(*) FROM timesheets"),
            ("workers", "SELECT count(DISTINCT worker_id) FROM timesheets"),
            ("day rows", "SELECT count(*) FROM timesheet_days"),
            ("rate lines", "SELECT count(*) FROM timesheet_rates"),
            ("comments", "SELECT count(*) FROM timesheet_comments"),
        ):
            print(f"  {label:<14} {scalar(sql)}")

        print("\n== STATUS ==")
        cur.execute("SELECT status, count(*) FROM timesheets GROUP BY status ORDER BY 2 DESC")
        for status, count in cur.fetchall():
            print(f"  {str(status):<18} {count}")

        print("\n== WEEKS ==")
        cur.execute("SELECT period_end, count(*) FROM timesheets GROUP BY period_end ORDER BY 1")
        for period_end, count in cur.fetchall():
            print(f"  week ending {period_end}   {count}")

        print("\n== COMPLETENESS (missing) ==")
        for column in ("worker_id", "status", "period_start", "period_end", "bill_rate",
                       "quantity", "amount", "currency", "total_worked", "legal_entity"):
            missing = scalar(f"SELECT count(*) FROM timesheets WHERE {column} IS NULL")
            print(f"  {'OK ' if missing == 0 else '(!)'} {column:<15} {missing}")

        print("\n== FINANCIALS ==")
        print(f"  total billed   {scalar('SELECT sum(amount) FROM timesheets')}")
        print(f"  total quantity {scalar('SELECT round(sum(quantity),2) FROM timesheets')}")
        print(f"  logged hours   {scalar('SELECT round(sum(minutes)/60.0,2) FROM timesheet_days')}")

        print("\n== INTEGRITY (all should be 0) ==")
        checks = (
            ("quantity <> logged hours", """
                SELECT count(*) FROM (
                  SELECT t.timesheet_id FROM timesheets t JOIN timesheet_days d USING (timesheet_id)
                  GROUP BY t.timesheet_id, t.quantity
                  HAVING abs(t.quantity - sum(d.minutes)/60.0) > 0.02) x"""),
            ("amount <> rate x qty", """
                SELECT count(*) FROM timesheets
                WHERE rate_line_count = 1 AND abs(amount - bill_rate*quantity) > 1.20"""),
            ("multi-line <> sum of lines", """
                SELECT count(*) FROM timesheets t WHERE rate_line_count > 1 AND abs(t.amount -
                  (SELECT sum(amount) FROM timesheet_rates r
                   WHERE r.timesheet_id = t.timesheet_id AND r.party='bill')) > 0.02"""),
            ("incomplete day grids", """
                SELECT count(*) FROM (SELECT timesheet_id FROM timesheet_days
                  GROUP BY timesheet_id HAVING count(*) <> 7) x"""),
            ("negative amounts", "SELECT count(*) FROM timesheets WHERE amount < 0"),
        )
        failures = 0
        for label, sql in checks:
            cur.execute(sql)
            row = cur.fetchone()
            count = row[0] if row else 0
            failures += bool(count)
            print(f"  {'OK ' if count == 0 else '(!)'} {label:<28} {count}")

        print("\n== MULTI-RATE (flagged for review) ==")
        cur.execute("""SELECT timesheet_id, worker_name, period_start, period_end, amount
                       FROM timesheets WHERE rate_line_count > 1 ORDER BY worker_name""")
        rows = cur.fetchall()
        if not rows:
            print("  none")
        for r in rows:
            print(f"  {r[0]} {str(r[1])[:24]:<24} {r[2]}..{r[3]}  total={r[4]}")
            cur.execute("""SELECT line_index, rate, quantity, amount FROM timesheet_rates
                           WHERE timesheet_id=%s AND party='bill' ORDER BY line_index""", (r[0],))
            for line in cur.fetchall():
                print(f"       line {line[0]}: rate={line[1]} qty={line[2]} amount={line[3]}")
        print()

    return 1 if failures else 0


COMMANDS = {
    "check": cmd_check,
    "data": cmd_data,
    "pdfs": cmd_pdfs,
    "both": cmd_both,
    "merge": cmd_merge,
    "export": cmd_export,
    "study": cmd_study,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="SAP Fieldglass timesheet extraction and document retrieval.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("check", "verify database, schema and portal access"),
        ("data", "extract a month of timesheet data into PostgreSQL"),
        ("pdfs", "download PDFs for approved timesheets"),
        ("both", "extract data, then download PDFs, over one list fetch"),
        ("merge", "combine weekly PDFs into one document per resource"),
        ("export", "write the Excel workbook from stored data"),
        ("study", "coverage, completeness and integrity report"),
    ):
        p = sub.add_parser(name, help=help_text)
        if name in ("data", "pdfs", "both", "export", "merge"):
            p.add_argument("--month", help="target month as MM/YYYY (default: previous month)")
        if name in ("data", "pdfs", "both"):
            p.add_argument("--concurrency", type=int, default=3,
                           help="simultaneous requests (default 3)")
            p.add_argument("--limit", type=int, default=None,
                           help="stop after N items - smoke tests only")
        if name in ("pdfs", "both"):
            p.add_argument("--merge", action="store_true",
                           help="after downloading, merge each resource's weeks into one PDF")
        if name in ("pdfs", "both", "merge"):
            p.add_argument("--no-bookmarks", action="store_true",
                           help="do not add a per-week outline entry to merged PDFs")
        if name == "merge":
            p.add_argument("--dir", type=Path, default=None,
                           help="month directory to merge (default: the month's download dir)")
        if name == "export":
            p.add_argument("--out", type=Path, default=None, help="output .xlsx path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    setup_logger(settings)
    return asyncio.run(COMMANDS[args.command](args, settings))


if __name__ == "__main__":
    sys.exit(main())
