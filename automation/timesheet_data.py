"""SAP Fieldglass timesheet data extraction module.

Reads each timesheet's detail page and pulls out the billing figures, daily hours, legal
entity and comments, then stores them in PostgreSQL.

This replaces walking the Create -> Invoice flow. That flow only lists what is queued for
invoicing - a shifting subset of the month - and reaching each timesheet costs a chain of
stateful clicks. Here the month's timesheets come from the Time Sheet list in one request,
and each timesheet is then a single independent GET, so one failure cannot disturb the rest.

Fieldglass renders the timesheet as XML and transforms it into the printable PDF, but that XML
servlet is reachable only from inside their own servers - every external call is answered with
a sign-in page. The detail page's HTML is therefore the source, and it carries every field.
"""

import asyncio
import html as htmllib
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from playwright.async_api import BrowserContext, Page

from automation.timesheet_pdf import TimesheetRow, fetch_month_rows
from config.settings import Settings
from db import postgres
from utils.helpers import get_previous_month_date_range

# 'ST /Hr', 'OT /Hr' and the like carry the billable daily hours. Rows such as 'Shift' repeat
# the same 'Xh Ym' shape but are always zero, so the rate category is what identifies them.
_RATE_ROW_LABEL = re.compile(r"/\s*Hr", re.I)
_DAY_HEADER = re.compile(r"^(\d{1,2})/(\d{1,2})\s+(\w+)$")
_HOURS_CELL = re.compile(r"^(\d+)h\s+(\d+)m$")
_CURRENCY_HEADER = re.compile(r"Amount\s*\((\w+)\)", re.I)


@dataclass
class ExtractionSummary:
    """Outcome of one month's data extraction."""

    extracted: list[dict[str, Any]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stored: int = 0
    elapsed_seconds: float = 0.0


def _unescape_json(value: str) -> str:
    """Undo the escaping used inside the page's embedded JSON (e.g. '28\\/06\\/2026')."""
    return value.replace(chr(92) + "/", "/").replace(chr(92) + '"', '"').strip()


def _clean(fragment: str) -> str:
    """Flatten an HTML fragment to its visible text."""
    text = re.sub(r"<br\s*/?>", " ", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def _to_decimal(text: str | None) -> Decimal | None:
    """Parse '8,928.67' into a Decimal. '-' and blanks mean 'not applicable'."""
    if not text or text.strip() in ("-", ""):
        return None
    try:
        return Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None


def _to_minutes(hours_text: str | None) -> int | None:
    """Parse '7h 28m' into 448 minutes."""
    if not hours_text:
        return None
    match = _HOURS_CELL.match(hours_text.strip())
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def _parse_ddmmyyyy(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _parse_entered_at(text: str | None) -> datetime | None:
    """Parse a comment stamp such as '11/08/2026 09:02 AM'."""
    try:
        return datetime.strptime((text or "").strip(), "%d/%m/%Y %I:%M %p")
    except ValueError:
        return None


def _parse_accounting_section(
    page_html: str, section: str
) -> tuple[list[dict[str, Any]], Decimal | None]:
    """Read every rate line of one accounting block, plus its Total.

    A timesheet can carry more than one line - a rate revision part-way through the week bills
    the two halves separately - so all lines are returned and the Total is read from the block's
    own Total row rather than assumed to equal the first line.

    Returns:
        tuple: (rate lines in page order, the block's Total amount).
    """
    start = page_html.find(section)
    if start < 0:
        return [], None

    segment = page_html[start:]
    table_end = segment.find("</table>")
    if table_end > 0:
        segment = segment[:table_end]

    lines: list[dict[str, Any]] = []
    total: Decimal | None = None

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", segment, re.S):
        header = re.search(r"<th[^>]*>(.*?)</th>", row, re.S)
        label = _clean(header.group(1)) if header else ""
        cells = [_clean(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]

        # The Total row closes the block; anything after it belongs to the next section
        if label.lower() == "total" or "totalCell" in row:
            if cells:
                total = _to_decimal(cells[-1])
            break

        # A rate line is 'Category | Rate | Quantity | Days | Amount'
        if len(cells) == 4 and label and "Rate Category" not in label:
            lines.append({
                "category": label,
                "rate": _to_decimal(cells[0]),
                "quantity": _to_decimal(cells[1]),
                "days": _to_decimal(cells[2]),
                "amount": _to_decimal(cells[3]),
            })

    return lines, total


def parse_timesheet_detail(page_html: str, fallback_period_end: date | None = None) -> dict[str, Any]:
    """Extract every stored field from one timesheet detail page.

    Args:
        page_html: Raw HTML of a time_sheet_detail.do page.
        fallback_period_end: Week-ending date to fall back on. Approved timesheets show a
            'Period' field in the header badge, but invoiced ones drop it, so the week-ending
            date from the list row is used instead and the start is counted back from it.

    Returns:
        dict: Parsed record, shaped for db.postgres.upsert_timesheet.
    """
    record: dict[str, Any] = {}

    # --- Header badge.
    # The badge is drawn by JavaScript from a JSON array embedded in the page, so a plain HTTP
    # fetch (which runs no JavaScript) sees only that array, while a browser-rendered capture
    # also has the label/value markup. Read the JSON first and treat the markup as a fallback,
    # so the same parser handles both.
    badge = {
        key: _unescape_json(value)
        for value, key in re.findall(r'"value":"([^"]*)","key":"([^"]*)"', page_html)
    }
    for label, value in re.findall(
        r'<div class="label[^"]*">(.*?)</div>\s*<div class="values">(.*?)</div>', page_html, re.S
    ):
        badge.setdefault(_clean(label), _clean(value))
    record["timesheet_id"] = badge.get("Time Sheet ID", "")
    record["status"] = badge.get("Status", "")
    record["buyer"] = badge.get("Buyer", "")

    period = badge.get("Period", "")
    if " to " in period:
        start_text, end_text = (part.strip() for part in period.split(" to ", 1))
        record["period_start"] = _parse_ddmmyyyy(start_text)
        record["period_end"] = _parse_ddmmyyyy(end_text)
    else:
        record["period_start"] = None
        record["period_end"] = None

    if not record["timesheet_id"]:
        found = re.search(r"DLTTS\d+", page_html)
        record["timesheet_id"] = found.group(0) if found else ""

    record["worker_id"] = badge.get("Worker ID") or None
    if not record["worker_id"]:
        worker = re.search(r"DLTWK\d+", page_html)
        record["worker_id"] = worker.group(0) if worker else None

    # --- Daily hours: a header row of dates, then one row per rate category
    day_labels: list[str] = []
    header = re.search(r'<tr class="subheaders">(.*?)</tr>', page_html, re.S)
    if header:
        # The same CSS class also marks the trailing 'Total Worked' header, which is not a day
        day_labels = [
            label for label in
            (_clean(cell) for cell in
             re.findall(r'<th class="dateAndDay[^"]*"[^>]*>(.*?)</th>', header.group(1), re.S))
            if _DAY_HEADER.match(label)
        ]

    # Candidate rows: the daily grid renders a non-working day as '-' rather than '0h 0m', so
    # cells are accepted if most of them are durations rather than every one.
    candidates: list[tuple[str, list[str]]] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S):
        label_match = re.search(r"<th[^>]*>(.*?)</th>", row, re.S)
        if not label_match:
            continue
        label = _clean(label_match.group(1))
        cells = [_clean(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < len(day_labels) or not day_labels:
            continue
        durations = sum(1 for c in cells if _HOURS_CELL.match(c))
        if durations >= len(day_labels) - 1 and durations >= 6:
            candidates.append((label, cells))

    # 'Total' is the per-day total across every rate category, so it is the right source when a
    # timesheet carries more than one (a rate change mid-week splits the hours across two rows).
    hour_cells: list[str] = []
    for wanted in (lambda lbl: lbl.lower() == "total", lambda lbl: bool(_RATE_ROW_LABEL.search(lbl))):
        match = next((cells for label, cells in candidates if wanted(label)), None)
        if match:
            hour_cells = match
            break
    if not hour_cells and candidates:
        hour_cells = candidates[0][1]
    record["total_worked"] = hour_cells[len(day_labels)] if len(hour_cells) > len(day_labels) else None
    record["total_minutes"] = _to_minutes(record["total_worked"])

    # Invoiced timesheets drop the 'Period' field, so fall back to the list's week-ending date
    # and count the start back across the columns the timesheet actually shows
    if not record["period_end"] and fallback_period_end:
        record["period_end"] = fallback_period_end
    if not record["period_start"] and record["period_end"] and day_labels:
        record["period_start"] = record["period_end"] - timedelta(days=len(day_labels) - 1)

    period_end: date | None = record.get("period_end")
    days = []
    for index, label in enumerate(day_labels):
        parts = _DAY_HEADER.match(label)
        day_date = None
        if parts and period_end:
            # The header shows only day/month; the year comes from the timesheet's own period,
            # and a December-into-January week rolls back a year rather than jumping forward
            day_num, month_num = int(parts.group(1)), int(parts.group(2))
            year = period_end.year - 1 if month_num == 12 and period_end.month == 1 else period_end.year
            try:
                day_date = date(year, month_num, day_num)
            except ValueError:
                day_date = None
        hours_text = hour_cells[index] if index < len(hour_cells) else None
        days.append({
            "label": label,
            "name": parts.group(3) if parts else None,
            "date": day_date,
            "hours_text": hours_text,
            "minutes": _to_minutes(hours_text),
        })
    record["days"] = days

    # --- Accounting: 'Bill to Buyer' is what is charged; 'Pay to Worker' sits above it
    currency = _CURRENCY_HEADER.search(page_html)
    record["currency"] = currency.group(1) if currency else None

    rate_lines: list[dict[str, Any]] = []
    for section, party in (("Bill to Buyer", "bill"), ("Pay to Worker", "pay")):
        lines, total = _parse_accounting_section(page_html, section)
        for index, line in enumerate(lines):
            rate_lines.append({"party": party, "line_index": index, **line})

        quantities = [line["quantity"] for line in lines if line["quantity"] is not None]
        if party == "bill":
            record["rate_category"] = lines[0]["category"] if lines else None
            record["rate_line_count"] = len(lines)
            # The billed figure is the Total, not the first line: a rate change mid-week splits
            # the week across two lines, and taking only the first silently drops the rest.
            record["amount"] = total if total is not None else (lines[0]["amount"] if lines else None)
            record["quantity"] = sum(quantities) if quantities else None
            # A single rate is unambiguous; with several, report the one covering most of the week
            if len(lines) == 1:
                record["bill_rate"] = lines[0]["rate"]
            elif lines:
                dominant = max(lines, key=lambda line: line["quantity"] or 0)
                record["bill_rate"] = dominant["rate"]
            else:
                record["bill_rate"] = None
        else:
            record["pay_amount"] = total if total is not None else (lines[0]["amount"] if lines else None)
            record["pay_rate"] = lines[0]["rate"] if len(lines) == 1 else (
                max(lines, key=lambda line: line["quantity"] or 0)["rate"] if lines else None)

    record["rate_lines"] = rate_lines

    # --- Posting Information: plain label/value rows
    posting = {
        _clean(label): _clean(value)
        for label, value in re.findall(
            r"<tr>\s*<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>\s*</tr>", page_html, re.S)
    }
    record["legal_entity"] = posting.get("Legal Entity") or None
    record["site"] = posting.get("Site") or None
    record["business_unit"] = posting.get("Business Unit") or None

    # --- Comments: one row each, kept in page order with author and timestamp
    comments = []
    # Attribute spacing differs between statuses ('summary="Comments" >' on invoiced pages),
    # so match the table loosely rather than on an exact tag
    block = re.search(r'<table[^>]*summary="Comments"[^>]*>(.*?)</table>', page_html, re.S)
    if block:
        for row in re.findall(r"<tr>(.*?)</tr>", block.group(1), re.S):
            cells = [_clean(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) == 3:
                comments.append({
                    "entered": cells[0],
                    "entered_at": _parse_entered_at(cells[0]),
                    "name": cells[1],
                    "comment": cells[2],
                })
    record["comments"] = comments
    # Every comment is kept, including repeats - two identical comments are two comments
    record["comments_joined"] = "; ".join(str(c["comment"]) for c in comments)

    return record


async def _extract_one(
    context: BrowserContext,
    row: TimesheetRow,
    max_attempts: int,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch and parse one timesheet's detail page."""
    detail_url = (f"{row.host}/time_sheet_detail.do?id={row.internal_id}"
                  f"&buyerCode=DLT&sjkName=DLT&dataBaseType=sql&startFlow=true")
    reason = "no attempt made"

    for attempt in range(1, max_attempts + 1):
        try:
            response = await context.request.get(detail_url, timeout=120000)
            page_html = await response.text()

            if "SAP Fieldglass Sign In" in page_html[:6000]:
                reason = "session expired (sign-in page returned)"
            else:
                record = parse_timesheet_detail(page_html, _parse_ddmmyyyy(row.end_date))
                # A page that yields no id was not a timesheet page; better to fail loudly
                # than to store a row of nulls
                if not record.get("timesheet_id"):
                    reason = "no timesheet id found on page"
                elif record["timesheet_id"] != row.time_sheet_ref:
                    reason = (f"page is for {record['timesheet_id']}, expected {row.time_sheet_ref}")
                else:
                    record["worker_name"] = row.worker_name
                    record["internal_id"] = row.internal_id
                    record["pdf_filename"] = row.pdf_filename
                    record["source_url"] = detail_url
                    if not record.get("status"):
                        record["status"] = row.status
                    return record, "ok"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:90]}"

        if attempt < max_attempts:
            await asyncio.sleep(2 * attempt)

    return None, reason


async def extract_month_timesheet_data(
    context: BrowserContext,
    page: Page,
    settings: Settings,
    start_date: str | None = None,
    end_date: str | None = None,
    rows: list[TimesheetRow] | None = None,
    concurrency: int = 3,
    max_attempts: int = 3,
    limit: int | None = None,
    store: bool = True,
) -> ExtractionSummary:
    """Extract a month of timesheet data and store it in PostgreSQL.

    Args:
        context: Authenticated Playwright BrowserContext.
        page: Active Page within that context.
        settings: Application settings (database connection, paths).
        start_date: Range start 'DD/MM/YYYY'. Defaults to the previous calendar month.
        end_date: Range end 'DD/MM/YYYY'. Defaults to the previous calendar month.
        rows: Pre-fetched month rows. Pass the ones the PDF run already loaded to avoid
            filtering the list a second time.
        concurrency: Simultaneous detail-page fetches. A detail page costs Fieldglass about a
            tenth of what a PDF costs, so this is light either way.
        max_attempts: Attempts per timesheet before giving up.
        limit: Stop after this many timesheets. For smoke tests only.
        store: Write to PostgreSQL. Set False to parse without a database.

    Returns:
        ExtractionSummary: What was extracted, stored, and what failed.
    """
    if not start_date or not end_date:
        computed_start, computed_end = get_previous_month_date_range()
        start_date = start_date or computed_start
        end_date = end_date or computed_end

    started = time.time()
    summary = ExtractionSummary()

    if rows is None:
        rows = await fetch_month_rows(page, start_date, end_date)
    else:
        logger.info(f"Re-using {len(rows)} row(s) already loaded from the Time Sheet list.")

    # Every timesheet is worth recording, including ones still awaiting approval - the data is
    # useful before the PDF exists, and the status column shows where each one stands
    targets = rows[:limit] if limit else rows
    if limit:
        logger.warning(f"limit={limit} set - extracting only {len(targets)} of {len(rows)} rows.")

    if store:
        postgres.ensure_schema(settings)

    logger.info(f"Extracting {len(targets)} timesheet(s) at concurrency {concurrency}...")
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0

    async def worker(row: TimesheetRow) -> None:
        nonlocal completed
        async with semaphore:
            record, reason = await _extract_one(context, row, max_attempts)
            completed += 1
            if record:
                summary.extracted.append(record)
                logger.info(f"[{completed}/{len(targets)}] {row.time_sheet_ref} "
                            f"{row.worker_name} | rate={record.get('bill_rate')} "
                            f"qty={record.get('quantity')} amount={record.get('amount')} "
                            f"comments={len(record.get('comments') or [])}")
            else:
                summary.failed.append((row.time_sheet_ref, reason))
                logger.error(f"[{completed}/{len(targets)}] {row.time_sheet_ref} FAILED: {reason}")

    await asyncio.gather(*(worker(row) for row in targets))

    if store and summary.extracted:
        summary.stored = postgres.upsert_many(settings, summary.extracted)
        logger.success(f"Stored {summary.stored} timesheet(s) in "
                       f"'{settings.PG_DATABASE}' on {settings.PG_HOST}:{settings.PG_PORT}.")

    summary.elapsed_seconds = time.time() - started
    logger.info("=" * 60)
    logger.success(f"Extracted {len(summary.extracted)} timesheet(s) in "
                   f"{summary.elapsed_seconds/60:.1f} min")
    if summary.failed:
        logger.error(f"Failed: {len(summary.failed)}")
        for timesheet_id, reason in summary.failed[:10]:
            logger.error(f"   {timesheet_id}: {reason}")
    logger.info("=" * 60)

    return summary
