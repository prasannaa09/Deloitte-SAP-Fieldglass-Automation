"""SAP Fieldglass Timesheet PDF download module.

Downloads the printable PDF of every approved timesheet in a target month.

Rather than driving the UI (open resource -> open week -> Actions -> Print), this calls the
same endpoint the Print button calls. Two facts make that possible:

1. The filtered Time Sheet list page embeds a JSON payload holding every row in the month -
   status, worker, week ending, the DLTTS reference AND Fieldglass's internal timesheet id.
   One request yields the entire work list; no per-resource navigation is needed.
2. Print is a single GET to Document2PDFServlet whose only per-timesheet variable is that
   internal id. Everything else is either constant or the buyer's own login id.
"""

import asyncio
import csv
import re
import time
import urllib.parse as up
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page, Response

from config.settings import Settings
from utils.helpers import get_previous_month_date_range

# A timesheet is only filed once it has cleared approval. 'Invoiced' and 'Paid' are later
# stages of an already-approved timesheet, so they qualify too; 'Pending Approval' does not
# and is reported instead, to be picked up by a later re-run once it clears.
DOWNLOADABLE_STATUSES = frozenset({"Approved", "Invoiced", "Paid"})

# The grid payload escapes its markup, so the row data has to be unescaped before parsing
_ESCAPES = (("u003c", "<"), ("u003e", ">"), ("u003d", "="), ("u0026", "&"), ("u0027", "'"))
_BACKSLASH = chr(92)

# Fields carried by each row of the embedded grid payload
_ROW_FIELDS = ("status", "time_sheet_ref", "worker_name", "end_date", "st_hours")

_ROW_START = re.compile(r'\{"name":"status","value":"')
_DETAIL_LINK = re.compile(r"(https://[^/\"]+)/time_sheet_detail\.do\?id=([a-z0-9]+)")
_LOGIN_ID = re.compile(r"loginId(?:%3D|=)([a-z0-9]{10,40})", re.I)

# Constant query parameters of the Print endpoint, established from the portal's own request
_PDF_XSLT = "xslt/timesheet/timesheet.xsl"
_PDF_MODULE_ID = "70"


@dataclass
class TimesheetRow:
    """One row of the Time Sheet list, carrying everything needed to fetch its PDF."""

    status: str
    time_sheet_ref: str
    worker_name: str
    end_date: str
    st_hours: str
    internal_id: str
    host: str

    @property
    def is_downloadable(self) -> bool:
        return self.status in DOWNLOADABLE_STATUSES

    @property
    def pdf_filename(self) -> str:
        """Worker, then ISO week-ending, then the timesheet id.

        Worker first groups a person's weeks together; the ISO date keeps those weeks in
        order within the group; the DLTTS id makes the name unique and joins the file back
        to its row in InvoiceSheet.xlsx.
        """
        who = re.sub(r"[^A-Za-z0-9]+", "_", self.worker_name).strip("_")
        day, month, year = self.end_date.split("/")
        return f"{who}__{year}-{month}-{day}__{self.time_sheet_ref}.pdf"


@dataclass
class DownloadSummary:
    """Outcome of one month's download run."""

    downloaded: list[TimesheetRow] = field(default_factory=list)
    already_present: list[TimesheetRow] = field(default_factory=list)
    pending_approval: list[TimesheetRow] = field(default_factory=list)
    failed: list[tuple[TimesheetRow, str]] = field(default_factory=list)
    output_dir: Path | None = None
    manifest_path: Path | None = None
    elapsed_seconds: float = 0.0

    @property
    def total_rows(self) -> int:
        return (len(self.downloaded) + len(self.already_present)
                + len(self.pending_approval) + len(self.failed))


def _unescape(text: str) -> str:
    """Turn the grid payload's escaped markup back into real characters."""
    for escape, char in _ESCAPES:
        text = text.replace(_BACKSLASH + escape, char)
    return text.replace(_BACKSLASH + '"', '"')


def parse_timesheet_rows(html: str) -> list[TimesheetRow]:
    """Extract every timesheet row from the list page's embedded grid payload.

    Args:
        html: Raw HTML of a Time Sheet list response.

    Returns:
        list[TimesheetRow]: One entry per timesheet, including rows the grid has not rendered.
    """
    clean = _unescape(html)
    starts = [m.start() for m in _ROW_START.finditer(clean)]
    rows: list[TimesheetRow] = []

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else start + 6000
        segment = clean[start:end]

        values = {}
        for field_name in _ROW_FIELDS:
            match = re.search(r'\{"name":"' + field_name + r'","value":"(.*?)"', segment)
            values[field_name] = match.group(1).strip() if match else ""

        link = _DETAIL_LINK.search(segment)
        if not link or not values["time_sheet_ref"]:
            continue

        rows.append(TimesheetRow(
            status=values["status"],
            time_sheet_ref=values["time_sheet_ref"],
            worker_name=values["worker_name"],
            end_date=values["end_date"],
            st_hours=values["st_hours"],
            # The host comes off the row's own link, so a different tenant needs no code change
            host=link.group(1),
            internal_id=link.group(2),
        ))

    return rows


def _rows_match_range(rows: list[TimesheetRow], start_date: str, end_date: str) -> bool:
    """Do these rows plausibly belong to the requested range?

    Guards the failure that looks like success: the grid's embedded payload is written once,
    on first render, and never refreshed when filters change, so the wrong response yields a
    full, well-formed set of rows for the *previous* range.

    The check is deliberately loose. Fieldglass includes the week that starts inside the range
    but ends just after it - filtering July returns the week ending 01/08 - so a strict range
    test would reject a correct result.
    """
    try:
        start = datetime.strptime(start_date, "%d/%m/%Y").date()
        end = datetime.strptime(end_date, "%d/%m/%Y").date() + timedelta(days=7)
    except ValueError:
        return True  # cannot judge; do not block the run

    dated = []
    for row in rows:
        try:
            dated.append(datetime.strptime(row.end_date, "%d/%m/%Y").date())
        except ValueError:
            continue

    if not dated:
        return False
    in_range = sum(1 for d in dated if start <= d <= end)
    return in_range / len(dated) > 0.5


async def fetch_month_rows(page: Page, start_date: str, end_date: str) -> list[TimesheetRow]:
    """Filter the Time Sheet list to a date range and return every row in it.

    Rows are read from the list responses themselves, not from the live page: the grid's
    embedded payload is written once, on first render, and is never refreshed when filters
    change, so reading the page after filtering silently returns the *previous* range.

    Applying the filter normally triggers a POST. It does not always - Fieldglass remembers a
    user's last filter, so re-requesting the same range can leave the values unchanged and fire
    nothing. Every list response is therefore captured, and the newest one that actually matches
    the requested range is used.

    Args:
        page: Active Playwright Page, already authenticated.
        start_date: Range start in 'DD/MM/YYYY' format.
        end_date: Range end in 'DD/MM/YYYY' format.

    Returns:
        list[TimesheetRow]: All timesheets in the range, whatever their status.

    Raises:
        RuntimeError: If the filter would not accept the dates, or no response matched the range.
    """
    host = re.match(r"https://[^/]+", page.url)
    base = host.group(0) if host else "https://www.us.fieldglass.cloud.sap"

    logger.info(f"Filtering Time Sheet list to {start_date} - {end_date}...")

    captured: list[bytes] = []

    async def capture(response: Response) -> None:
        if "time_sheet_list.do" not in response.url:
            return
        try:
            captured.append(await response.body())
        except Exception:  # a redirected or aborted response has no body
            pass

    handler = lambda response: asyncio.create_task(capture(response))  # noqa: E731
    page.on("response", handler)

    try:
        await page.goto(f"{base}/time_sheet_list.do?cf=1", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        # Target the inputs by id: role/label lookups match a neighbouring element, and the
        # typed value is then silently discarded, leaving the portal's own range in place
        for selector, value in (("#filterStartDate", start_date), ("#filterEndDate", end_date)):
            box = page.locator(selector)
            await box.wait_for(state="visible", timeout=20000)
            await box.click()
            await box.press("Control+a")
            await box.press("Delete")
            await box.type(value, delay=60)
            await box.dispatch_event("change")
            await box.dispatch_event("blur")
            await page.keyboard.press("Escape")  # dismiss the calendar overlay
            await page.wait_for_timeout(400)

        applied = (await page.locator("#filterStartDate").input_value(),
                   await page.locator("#filterEndDate").input_value())
        if applied != (start_date, end_date):
            raise RuntimeError(f"Date filter did not take: wanted {(start_date, end_date)}, got {applied}")

        seen_before = len(captured)
        apply_button = page.get_by_role("button", name="Apply Filters").first
        if await apply_button.count():
            await apply_button.click(force=True)

        # Wait for a fresh response, but do not insist on one: if the portal already held this
        # range, the click changes nothing and the initial page load is the correct payload
        for _ in range(60):
            if len(captured) > seen_before:
                break
            await page.wait_for_timeout(500)
        else:
            logger.info("No new list response after applying filters - the portal already "
                        "held this range; using the response from page load.")
        await page.wait_for_timeout(1500)
    finally:
        page.remove_listener("response", handler)

    # Newest first: prefer the most recent response that actually covers the requested range
    for body in reversed(captured):
        rows = parse_timesheet_rows(body.decode("utf-8", "replace"))
        if rows and _rows_match_range(rows, start_date, end_date):
            logger.success(f"Time Sheet list returned {len(rows)} rows "
                           f"across {len({r.worker_name for r in rows})} workers.")
            return rows

    raise RuntimeError(
        f"No Time Sheet list response matched {start_date} - {end_date} "
        f"({len(captured)} response(s) captured). The filter may not have been applied."
    )


async def discover_login_id(context: BrowserContext, row: TimesheetRow) -> str:
    """Read the buyer's login id out of a timesheet detail page.

    The value is stable for the user, but it is scraped rather than hard-coded so that a
    rotated id surfaces as a clear failure here instead of as a run of HTTP 500s later.

    Args:
        context: Authenticated Playwright BrowserContext.
        row: Any row of the list, used to open one detail page.

    Returns:
        str: The login id embedded in the page's Print action.

    Raises:
        RuntimeError: If no login id could be found.
    """
    detail_url = (f"{row.host}/time_sheet_detail.do?id={row.internal_id}"
                  f"&buyerCode=DLT&sjkName=DLT&dataBaseType=sql&startFlow=true")
    response = await context.request.get(detail_url, timeout=90000)
    match = _LOGIN_ID.search(await response.text())
    if not match:
        raise RuntimeError("Could not find loginId on the timesheet detail page.")

    logger.info(f"Resolved Fieldglass loginId: {match.group(1)}")
    return match.group(1)


def build_pdf_url(row: TimesheetRow, login_id: str) -> str:
    """Build the Print endpoint URL for one timesheet.

    docSource is itself a query string and must be passed as a single percent-encoded value.
    Left unencoded, its parameters are parsed as top-level ones, and the servlet spends ~20s
    before returning an HTML page instead of a PDF.
    """
    doc_source = up.quote(f"TimeSheetXMLServlet?loginId={login_id}&timeSheetId={row.internal_id}", safe="")
    return (
        f"{row.host}/Document2PDFServlet?processMode=xml2pdf"
        f"&docSource={doc_source}"
        f"&docXslt={up.quote(_PDF_XSLT, safe='')}"
        f"&filename=timesheet_{row.time_sheet_ref}.pdf"
        f"&moduleId={_PDF_MODULE_ID}&cf=1"
    )


async def _is_session_alive(context: BrowserContext, host: str) -> bool:
    """Check whether the stored session still authenticates against the portal."""
    try:
        response = await context.request.get(f"{host}/desktop.do", timeout=45000)
        return "Sign In" not in (await response.text())[:4000]
    except Exception as exc:
        logger.warning(f"Could not verify session state: {exc}")
        return False


async def _download_one(
    context: BrowserContext,
    row: TimesheetRow,
    login_id: str,
    target: Path,
    max_attempts: int,
    reauthenticate: Callable[[], Awaitable[bool]],
) -> tuple[bool, str]:
    """Fetch one timesheet PDF, retrying transient failures.

    Every response is checked for the %PDF signature. An expired session answers with HTTP 200
    and a sign-in page, so trusting the status code alone would write hundreds of HTML files
    named .pdf and report the run as a success.
    """
    reason = "no attempt made"
    for attempt in range(1, max_attempts + 1):
        try:
            response = await context.request.get(build_pdf_url(row, login_id), timeout=300000)
            body = await response.body()

            if body[:4] == b"%PDF":
                target.write_bytes(body)
                return True, f"{len(body)} bytes"

            reason = f"HTTP {response.status}, {len(body)} bytes, not a PDF"

            # A sign-in page means the session lapsed mid-run; recover once and retry
            if b"Sign In" in body[:4000] or not await _is_session_alive(context, row.host):
                logger.warning(f"{row.time_sheet_ref}: session appears to have expired; re-authenticating...")
                if await reauthenticate():
                    continue
                return False, "session expired and re-authentication failed"

        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:90]}"

        if attempt < max_attempts:
            backoff = 3 * attempt
            logger.warning(f"{row.time_sheet_ref}: attempt {attempt}/{max_attempts} failed "
                           f"({reason}); retrying in {backoff}s...")
            await asyncio.sleep(backoff)

    return False, reason


def _write_manifest(summary: DownloadSummary, path: Path) -> None:
    """Record the fate of every row, so a re-run and an audit both have a starting point."""
    outcomes: list[tuple[TimesheetRow, str, str]] = []
    outcomes += [(r, "downloaded", "") for r in summary.downloaded]
    outcomes += [(r, "already_present", "") for r in summary.already_present]
    outcomes += [(r, "skipped_pending_approval", "") for r in summary.pending_approval]
    outcomes += [(r, "failed", reason) for r, reason in summary.failed]
    outcomes.sort(key=lambda item: (item[0].worker_name, item[0].end_date))

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Timesheet ID", "Worker", "Week Ending", "Status", "ST Hours",
                         "Outcome", "File Name", "Detail"])
        for row, outcome, detail in outcomes:
            writer.writerow([row.time_sheet_ref, row.worker_name, row.end_date, row.status,
                             row.st_hours, outcome, row.pdf_filename, detail])


async def download_month_timesheet_pdfs(
    context: BrowserContext,
    page: Page,
    settings: Settings,
    start_date: str | None = None,
    end_date: str | None = None,
    output_root: Path | None = None,
    rows: list[TimesheetRow] | None = None,
    concurrency: int = 3,
    max_attempts: int = 3,
    limit: int | None = None,
) -> DownloadSummary:
    """Download the PDF of every approved timesheet in a month.

    Files land in <output_root>/<YYYY>/<MM>/ and are named for their worker, week ending and
    timesheet id. Anything already on disk is left alone, so a re-run a few days later fetches
    only the timesheets that have since cleared approval.

    Args:
        context: Authenticated Playwright BrowserContext.
        page: Active Page within that context.
        settings: Application settings, used for the download root and re-authentication.
        start_date: Range start 'DD/MM/YYYY'. Defaults to the previous calendar month.
        end_date: Range end 'DD/MM/YYYY'. Defaults to the previous calendar month.
        output_root: Base directory. Defaults to <DOWNLOAD_DIR>/timesheets.
        rows: Pre-fetched month rows. Pass the ones the data extraction already loaded so the
            list is filtered once per run rather than once per job.
        concurrency: Simultaneous downloads. Fieldglass spends ~20s building each PDF, so this
            is what sets the run time: 3 covers a month in ~45 min, 5 in ~25 min. It is kept at
            3 deliberately - the extra 20 minutes cost nothing on an unattended overnight run,
            and 5 produced an occasional HTTP 500 on this shared production tenant.
        max_attempts: Attempts per timesheet before giving up.
        limit: Stop after this many new downloads. For smoke tests only; leave as None for
            a real run, which must cover the whole month.

    Returns:
        DownloadSummary: What was downloaded, skipped, and what failed.
    """
    if not start_date or not end_date:
        computed_start, computed_end = get_previous_month_date_range()
        start_date = start_date or computed_start
        end_date = end_date or computed_end

    day, month, year = start_date.split("/")
    root = output_root or (settings.DOWNLOAD_DIR / "timesheets")
    output_dir = root / year / month
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info(f"Timesheet PDF download | {start_date} - {end_date} | concurrency {concurrency}")
    logger.info(f"Destination: {output_dir}")
    logger.info("=" * 60)

    started = time.time()
    summary = DownloadSummary(output_dir=output_dir)

    if rows is None:
        rows = await fetch_month_rows(page, start_date, end_date)
    else:
        logger.info(f"Re-using {len(rows)} row(s) already loaded from the Time Sheet list.")

    if not rows:
        logger.warning("No timesheets found for the requested range.")
        return summary

    summary.pending_approval = [r for r in rows if not r.is_downloadable]
    candidates = [r for r in rows if r.is_downloadable]

    pending_statuses = {r.status for r in summary.pending_approval}
    logger.info(f"{len(candidates)} timesheet(s) eligible for download; "
                f"{len(summary.pending_approval)} skipped {sorted(pending_statuses) or ''}")

    # Anything already on disk is done - this is what makes the run resumable. Matching is by
    # file name, never by content: Fieldglass stamps a fresh creation time into each PDF, so
    # the same timesheet downloaded twice differs byte-for-byte.
    todo = []
    for row in candidates:
        if (output_dir / row.pdf_filename).exists():
            summary.already_present.append(row)
        else:
            todo.append(row)

    if summary.already_present:
        logger.info(f"{len(summary.already_present)} PDF(s) already present; skipping them.")

    if limit is not None and len(todo) > limit:
        logger.warning(f"limit={limit} set - downloading only {limit} of {len(todo)} outstanding "
                       f"PDF(s). This is a smoke test, not a full run.")
        todo = todo[:limit]

    if not todo:
        logger.success("Nothing left to download - every eligible timesheet is already on disk.")
        summary.elapsed_seconds = time.time() - started
        summary.manifest_path = output_dir / f"manifest_{year}_{month}.csv"
        _write_manifest(summary, summary.manifest_path)
        return summary

    login_id = await discover_login_id(context, todo[0])

    # One re-authentication at a time; without the lock, a lapsed session would send every
    # in-flight download into its own login at once
    auth_lock = asyncio.Lock()

    async def reauthenticate() -> bool:
        async with auth_lock:
            if await _is_session_alive(context, todo[0].host):
                return True  # another task already recovered it
            from automation.login import authenticate_session
            success, _ = await authenticate_session(context=context, page=page, settings=settings)
            if success:
                try:
                    await context.storage_state(path=str(settings.AUTH_FILE_PATH))
                except Exception as exc:
                    logger.warning(f"Could not persist refreshed session: {exc}")
                logger.success("Session re-established; resuming downloads.")
            return success

    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    total = len(todo)

    async def worker(row: TimesheetRow) -> None:
        nonlocal completed
        async with semaphore:
            ok, detail = await _download_one(
                context, row, login_id, output_dir / row.pdf_filename, max_attempts, reauthenticate)
            completed += 1
            if ok:
                summary.downloaded.append(row)
                logger.info(f"[{completed}/{total}] {row.time_sheet_ref} "
                            f"{row.worker_name} {row.end_date} -> {row.pdf_filename} ({detail})")
            else:
                summary.failed.append((row, detail))
                logger.error(f"[{completed}/{total}] {row.time_sheet_ref} FAILED: {detail}")

    logger.info(f"Downloading {total} PDF(s)...")
    await asyncio.gather(*(worker(row) for row in todo))

    summary.elapsed_seconds = time.time() - started
    summary.manifest_path = output_dir / f"manifest_{year}_{month}.csv"
    _write_manifest(summary, summary.manifest_path)

    logger.info("=" * 60)
    logger.success(f"Downloaded {len(summary.downloaded)} PDF(s) in {summary.elapsed_seconds/60:.1f} min")
    if summary.already_present:
        logger.info(f"Already present : {len(summary.already_present)}")
    if summary.pending_approval:
        logger.warning(f"Pending approval: {len(summary.pending_approval)} "
                       f"- re-run once these are approved to collect them")
    if summary.failed:
        logger.error(f"Failed          : {len(summary.failed)}")
        for row, reason in summary.failed[:10]:
            logger.error(f"   {row.time_sheet_ref} ({row.worker_name}): {reason}")
    logger.info(f"Manifest: {summary.manifest_path}")
    logger.info("=" * 60)

    return summary
