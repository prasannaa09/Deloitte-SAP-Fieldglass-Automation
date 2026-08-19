"""SAP Fieldglass Invoice Extraction & InvoiceSheet.xlsx Enrichment Module."""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import List, Optional, Tuple, Union

import pandas as pd
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from utils.helpers import get_previous_month_date_range


async def navigate_to_invoice_schedule(page: Page) -> None:
    """Navigate to Create -> Invoice -> USI Invoice Billing Schedule list page."""
    logger.info("Navigating to Create menu -> Invoice...")

    # 1. Click 'Create' treeitem menu
    create_tree = page.get_by_role("treeitem", name="Create")
    if await create_tree.count() > 0 and await create_tree.first.is_visible():
        await create_tree.first.click()
    else:
        await page.locator("a:has-text('Create')").first.click()

    # 2. Click 'Invoice' link
    invoice_link = page.locator("a").filter(has_text=re.compile(r"^Invoice$"))
    if await invoice_link.count() > 0 and await invoice_link.first.is_visible():
        await invoice_link.first.click()
    else:
        await page.locator("a:has-text('Invoice')").first.click()

    # 3. Click 'USI Invoice Billing Schedule Time...' link
    logger.info("Opening USI Invoice Billing Schedule list...")
    usi_schedule_link = page.get_by_role("cell", name=re.compile(r"USI Invoice Billing Schedule Time", re.I)).get_by_role("link")
    if await usi_schedule_link.count() > 0 and await usi_schedule_link.first.is_visible():
        await usi_schedule_link.first.click()
    else:
        await page.locator("a:has-text('USI Invoice Billing Schedule Time')").first.click()

    await page.wait_for_load_state("domcontentloaded", timeout=15000)


# Attribute patterns that mark the table's 'select all' toggle rather than a resource row
_SELECT_ALL_PATTERN = re.compile(r"select.?all|check.?all|toggle.?all|all.?check|master", re.I)

# A timesheet week cell holds a bare date. '17/07/2026 12:44 PM' is an audit stamp, not a week.
_DATE_ONLY_PATTERN = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")

# Legal Entity reads 'Deloitte Support Services India Private Limited (1802)' - name plus a 4-digit code
_LEGAL_ENTITY_PATTERN = re.compile(r"^.{3,120}\(\d{4}\)$")

_MONEY_PATTERN = re.compile(r"-?[\d,]+\.\d{2}")

_CHECKBOX_SCAN_JS = r"""() => Array.from(document.querySelectorAll("input[type='checkbox']")).map((el, idx) => ({
    idx,
    id: el.id || '',
    name: el.getAttribute('name') || '',
    inHeader: !!(el.closest('thead') || el.closest('th')),
    disabled: !!el.disabled,
    visible: el.getClientRects().length > 0,
    rowText: ((el.closest('tr') || {}).innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
}))"""

_CELL_TEXT_SCAN_JS = r"""() => Array.from(document.querySelectorAll('td, th, a, span, div'))
    .map(el => (el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(t => t.length > 0 && t.length <= 160)"""

# One comment is one row. The text is rebuilt from the row's individual text nodes joined by a space,
# because Fieldglass splits the label from the body ('Corrections' + 'Reason: ...') into adjacent
# nodes with no whitespace between them - textContent glued them into 'CorrectionsReason:'.
_COMMENT_SCAN_JS = r"""() => {
    const pattern = /Reason\s*:/i;

    const textOf = (el) => {
        const parts = [];
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const t = (node.textContent || '').replace(/\s+/g, ' ').trim();
            if (t) parts.push(t);
        }
        return parts.join(' ').replace(/\s+/g, ' ').trim();
    };

    const collect = (selector) => {
        const hits = Array.from(document.querySelectorAll(selector))
            .filter(el => pattern.test(el.textContent || ''));
        // Keep the innermost match only - Fieldglass nests tables inside table cells, so an outer
        // row would otherwise repeat every comment it wraps
        const innermost = hits.filter(el => !hits.some(other => other !== el && el.contains(other)));
        return innermost.map(textOf).filter(t => pattern.test(t));
    };

    return { rows: collect('tr'), cells: collect('td, li, p') };
}"""

# Columns holding money, written to 2dp so 11775.2 reads as the 11,775.20 shown on screen
_MONEY_COLUMNS = ("BillRate", "Amount")

_CENTS = Decimal("0.01")


def _parse_money(text: Optional[str]) -> List[Decimal]:
    """Pull every currency-style number out of a blob of table text, in reading order.

    Decimal rather than float, so 294.38 x 40 is exactly 11775.20 and the figures written to the
    sheet are the ones shown on screen rather than a binary approximation of them.
    """
    return [Decimal(v.replace(",", "")) for v in _MONEY_PATTERN.findall(text or "")]


async def _discover_resource_checkboxes(page: Page) -> Tuple[Optional[int], List[int]]:
    """Return (select-all index, resource-row indices) within all input[type=checkbox] on the list.

    The header 'Select All' toggle is kept separate: it is what clears the list in one action, but
    leaving it among the resource rows would select every resource at once and 'Continue' would
    never open a single resource detail page.
    """
    all_boxes = page.locator("input[type='checkbox']")
    try:
        await all_boxes.first.wait_for(state="visible", timeout=15000)
    except Exception as wait_err:
        logger.warning(f"Note while waiting for resource checkboxes: {wait_err}")

    boxes = await page.evaluate(_CHECKBOX_SCAN_JS)

    select_all_idx = None
    for b in boxes:
        if b["visible"] and (b["inHeader"] or _SELECT_ALL_PATTERN.search(f"{b['id']} {b['name']}")):
            select_all_idx = b["idx"]
            break

    candidates = [
        b
        for b in boxes
        if b["visible"]
        and not b["disabled"]
        and not b["inHeader"]
        and not _SELECT_ALL_PATTERN.search(f"{b['id']} {b['name']}")
        and b["rowText"]
        and "select all" not in b["rowText"].lower()
    ]

    # When the list carries worker IDs, trust only those rows - it rules out any stray toggle
    worker_rows = [b for b in candidates if "DLTWK" in b["rowText"].upper()]
    if worker_rows:
        candidates = worker_rows

    ignored = len(boxes) - len(candidates)
    if ignored:
        logger.info(f"Ignored {ignored} non-resource checkbox(es) (header 'Select All' / hidden inputs).")

    return select_all_idx, [b["idx"] for b in candidates]


async def _reset_selection_with_select_all(page: Page, select_all_idx: Optional[int]) -> None:
    """Clear the list the way it is done by hand: tick 'Select All', then untick it.

    Ticking it selects every row and unticking it clears every row in one action. Unticking rows
    individually would crawl down the list one box at a time, which is not the flow.
    """
    select_all = page.get_by_role("checkbox", name="Select All")
    if await select_all.count() == 0 or not await select_all.first.is_visible():
        if select_all_idx is None:
            logger.warning("No 'Select All' checkbox found; leaving the list selection as-is.")
            return
        select_all = page.locator("input[type='checkbox']").nth(select_all_idx)

    toggle = select_all.first
    try:
        await toggle.check()
        await page.wait_for_timeout(400)
        await toggle.uncheck()
        await page.wait_for_timeout(400)
    except Exception as reset_err:
        logger.warning(f"Could not reset the list selection via 'Select All': {reset_err}")


async def _wait_for_resource_detail(page: Page, list_checkbox_count: int, timeout_ms: int = 25000) -> bool:
    """Wait until the resource detail page has actually replaced the selection list.

    'Continue' fires a postback that leaves the old list in the DOM for a moment. The detail page
    is confirmed by the checkbox count moving away from the list's count, plus a full dd/mm/yyyy
    timesheet date being present.
    """
    date_pattern = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
    remaining = timeout_ms
    consecutive_hits = 0

    while remaining > 0:
        try:
            current_cb_count = await page.locator("input[type='checkbox']").count()
            body_text = await page.locator("body").inner_text() if current_cb_count != list_checkbox_count else ""
            # Two clean polls in a row, so a half-rendered postback cannot pass as the detail page
            if body_text and date_pattern.search(body_text):
                consecutive_hits += 1
                if consecutive_hits >= 2:
                    return True
            else:
                consecutive_hits = 0
        except Exception as probe_err:
            logger.warning(f"Note while waiting for Resource Detail Page: {probe_err}")
            consecutive_hits = 0

        await page.wait_for_timeout(500)
        remaining -= 500

    return False


async def _collect_week_dates(page: Page, target_month_str: str) -> List[str]:
    """Return the resource's in-scope week-ending dates, oldest first.

    Only cells that are *nothing but* a date qualify, so approval stamps such as
    '17/07/2026 12:44 PM' cannot be mistaken for a timesheet week.
    """
    texts = await page.evaluate(_CELL_TEXT_SCAN_JS)
    next_month_num = int(target_month_str) % 12 + 1

    sortable = {}
    for txt in texts:
        match_date = _DATE_ONLY_PATTERN.match(txt)
        if not match_date:
            continue

        day_str, month_str, year_str = match_date.group(1), match_date.group(2), match_date.group(3)
        # The target previous month, plus the 1st of the next month (the spill-over week)
        if month_str == target_month_str or (day_str == "01" and int(month_str) == next_month_num):
            sortable[txt] = f"{year_str}{month_str}{day_str}"

    return sorted(sortable, key=lambda date_text: sortable[date_text])


async def _click_week_cell(page: Page, date_text: str) -> bool:
    """Open one timesheet week by clicking its date cell, matched on text rather than position.

    Positional (nth) handles go stale the moment the first week is opened and the panel re-renders,
    which is what previously cut a resource short after two or three weeks.
    """
    strategies = [
        page.get_by_role("cell", name=date_text, exact=True),
        page.get_by_role("cell", name=date_text),
        page.locator("td, a").filter(has_text=date_text),
    ]

    for candidate in strategies:
        try:
            for idx in range(min(await candidate.count(), 10)):
                target = candidate.nth(idx)
                if (await target.text_content() or "").strip() != date_text:
                    continue
                await target.scroll_into_view_if_needed()
                await target.click()
                return True
        except Exception as click_err:
            logger.warning(f"Could not click week cell '{date_text}': {click_err}")

    return False


async def _extract_timesheet_id(page: Page) -> str:
    """Read the DLTTS... timesheet ID off the badge on the open week."""
    badge_loc = page.locator("#timeSheetBadge")
    if await badge_loc.count() > 0:
        id_match = re.search(r"DLTTS\d+", (await badge_loc.first.text_content() or "").strip())
        if id_match:
            return id_match.group(0)

    # Fallback if the badge is not rendered as expected
    id_matches = re.findall(r"DLTTS\d+", await page.locator("body").inner_text())
    return id_matches[-1] if id_matches else ""


async def _extract_bill_rate_and_amount(page: Page, st_hours: Optional[Decimal]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """Read BillRate and Amount out of the 'Bill to Buyer' block.

    The Total row supplies the Amount (what gets read off the screen by hand). The rate is then the
    value that multiplies the timesheet's ST hours back up to that Amount - searched across the whole
    block rather than one guessed row, because 'tr:has-text(...)' can land on a wrapper row whose only
    other numbers are 0.00 columns, which is how BillRate came out as zero.
    """
    # Tolerate hours arriving as a float/str - mixing those with Decimal raises, and one bad type
    # would take out the whole resource
    if st_hours is not None and not isinstance(st_hours, Decimal):
        try:
            st_hours = Decimal(str(st_hours))
        except (TypeError, ValueError, InvalidOperation):
            st_hours = None

    buyer_table = page.locator("table:has-text('Bill to Buyer'), div:has-text('Bill to Buyer')").last
    if await buyer_table.count() == 0:
        return None, None

    # Amount: the 'Total' row
    amount_val = None
    total_row = buyer_table.locator("tr:has-text('Total')").last
    if await total_row.count() > 0:
        totals = _parse_money(await total_row.text_content())
        if totals:
            amount_val = totals[-1]

    # Rate row: 'ST /Hr' where present, otherwise any per-hour line
    rate_row = buyer_table.locator("tr:has-text('ST /Hr')").first
    if await rate_row.count() == 0:
        rate_row = buyer_table.locator("tr:has-text('/Hr')").first

    row_numbers = _parse_money(await rate_row.text_content()) if await rate_row.count() > 0 else []
    block_numbers = _parse_money(await buyer_table.text_content())

    if amount_val is None and row_numbers:
        amount_val = max(row_numbers)

    bill_rate_val = None

    # 1. Read it off the screen: the value that reconstructs the Amount from the known ST hours.
    #    Both figures are shown rounded to 2dp, so allow the error that rounding can accumulate
    #    across the hours (0.005 x hours), never a bare 0.05.
    if st_hours and amount_val:
        tolerance = Decimal("0.05") + Decimal("0.01") * st_hours
        for value in [v for v in row_numbers if v > 0] + [v for v in block_numbers if v > 0]:
            if abs(value * st_hours - amount_val) <= tolerance:
                bill_rate_val = value
                break

    # 2. Same number, just derived rather than read - only reachable if it is not on screen
    if bill_rate_val is None and st_hours and amount_val:
        bill_rate_val = (amount_val / st_hours).quantize(_CENTS, rounding=ROUND_HALF_UP)

    # 3. No usable hours: the largest rate-row value that is not the Amount itself
    if bill_rate_val is None:
        others = [v for v in row_numbers if v > 0 and (amount_val is None or v != amount_val)]
        if others:
            bill_rate_val = max(others)

    if not bill_rate_val:
        # Never overwrite a real rate with 0 - leave the cell alone and say so
        logger.warning(f"Could not resolve BillRate (ST {st_hours}, Amount {amount_val}, row {row_numbers}).")
        bill_rate_val = None

    if bill_rate_val is not None:
        bill_rate_val = bill_rate_val.quantize(_CENTS, rounding=ROUND_HALF_UP)
    if amount_val is not None:
        amount_val = amount_val.quantize(_CENTS, rounding=ROUND_HALF_UP)

    return bill_rate_val, amount_val


async def _extract_legal_entity(page: Page) -> Optional[str]:
    """Find the 'Deloitte ... (1802)' legal entity cell.

    The worker header carries the entity name too, but wrapped up with the DLTWK worker ID and the
    site address - matching on the trailing 4-digit entity code keeps that header out.
    """
    for txt in await page.evaluate(_CELL_TEXT_SCAN_JS):
        if "Deloitte" not in txt or "DLTWK" in txt:
            continue
        if _LEGAL_ENTITY_PATTERN.match(txt):
            return txt
    return None


async def _extract_comments(page: Page) -> Optional[str]:
    """Join every comment on the open week into one cell value.

    Every comment is kept, including repeats: two identical comments on a week are two comments.
    The previous pass deduplicated on text and then dropped any entry containing another, which
    silently threw away the second and third comment on a week.
    """
    scan = await page.evaluate(_COMMENT_SCAN_JS)

    # Prefer one comment per row; fall back to cell level if the rows came back as layout blocks
    for level in ("rows", "cells"):
        comments = [txt for txt in scan.get(level) or [] if 0 < len(txt) <= 400]
        if comments:
            logger.debug(f"Comments found at '{level}' level: {comments}")
            return "; ".join(comments)

    return None


def _lookup_st_hours(df: pd.DataFrame, mask: pd.Series) -> Optional[Decimal]:
    """The sheet already knows the week's ST hours - used to tell rate from quantity."""
    if "ST" not in df.columns or not mask.any():
        return None
    try:
        return Decimal(str(df.loc[mask, "ST"].iloc[0]).replace(",", ""))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _write_report(df: pd.DataFrame, target: Path) -> None:
    """Write the sheet, showing BillRate and Amount to 2dp so 11775.2 reads as 11775.20."""
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Sheet1", index=False)
        sheet = writer.book["Sheet1"]
        for col_name in _MONEY_COLUMNS:
            if col_name not in df.columns:
                continue
            col_idx = df.columns.get_loc(col_name) + 1
            for row_idx in range(2, len(df) + 2):
                sheet.cell(row=row_idx, column=col_idx).number_format = "0.00"


async def process_invoice_billing_schedule(
    page: Page,
    report_excel_path: Union[str, Path],
    target_month_num: int | None = None,
) -> Path:
    """Loop through resource checkboxes in USI Invoice Billing Schedule, extract timesheet detail metrics

    (BillRate, Amount, Legal Entity, Comments), match by Timesheet ID (DLTTS...), and update InvoiceSheet.xlsx.

    Args:
        page: Active Playwright Page instance.
        report_excel_path: Path to InvoiceSheet.xlsx file to update.
        target_month_num: Target previous month number (1-12). If None, calculated automatically.

    Returns:
        Path: Path to updated InvoiceSheet.xlsx.
    """
    excel_file = Path(report_excel_path).resolve()
    if not excel_file.exists():
        logger.error(f"Report file not found: {excel_file}")
        return excel_file

    # Load existing InvoiceSheet.xlsx into DataFrame and convert columns to object dtype to allow flexible updates
    df = pd.read_excel(excel_file)
    df = df.astype(object)
    logger.info(f"Loaded {len(df)} rows from {excel_file.name} for enrichment.")

    # 1. Navigate to USI Invoice Billing Schedule
    await navigate_to_invoice_schedule(page)

    # Calculate target previous month string if needed (e.g. '07' for July)
    if not target_month_num:
        calc_start, _ = get_previous_month_date_range()
        target_month_str = calc_start.split("/")[1]  # e.g. '07'
    else:
        target_month_str = f"{target_month_num:02d}"

    logger.info(f"Targeting previous month filter string: '/{target_month_str}/'")

    # 2. Get the 'Select All' toggle and the resource-row checkboxes in the schedule table
    select_all_idx, cb_indices = await _discover_resource_checkboxes(page)
    resource_count = len(cb_indices)
    logger.info(f"Found {resource_count} resource items in Invoice Billing Schedule list.")

    for i in range(resource_count):
        try:
            logger.info(f"Processing Resource item {i+1} of {resource_count}...")

            # 1. Clear the list: tick 'Select All' to select every row, untick it to clear them all
            await _reset_selection_with_select_all(page, select_all_idx)

            # 2. Tick this resource's checkbox only, and confirm it took
            cb = page.locator("input[type='checkbox']").nth(cb_indices[i])
            await cb.scroll_into_view_if_needed()
            await cb.check()
            await page.wait_for_timeout(300)
            if not await cb.is_checked():
                await cb.click(force=True)
                await page.wait_for_timeout(300)
            if not await cb.is_checked():
                logger.warning(f"Could not tick the checkbox for Resource item {i+1}; skipping it.")
                continue

            # 3. Click Continue, then wait for the detail page to really replace the list.
            #    Without this wait the first (cold) postback is still in flight, so we scan the list
            #    page instead, find no timesheet dates, and fall straight through to Cancel/Discard.
            logger.info("Clicking 'Continue' to load Resource Timesheet Detail Page...")
            list_cb_count = await page.locator("input[type='checkbox']").count()
            continue_btn = page.get_by_role("button", name="Continue")
            await continue_btn.first.wait_for(state="visible", timeout=10000)
            await continue_btn.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)

            if not await _wait_for_resource_detail(page, list_cb_count):
                logger.error(f"Resource item {i+1}: detail page never opened after 'Continue'; skipping resource.")
                await navigate_to_invoice_schedule(page)
                continue

            # 4. Walk this resource's timesheet weeks oldest -> newest, as done by hand
            week_dates = await _collect_week_dates(page, target_month_str)
            logger.info(f"Timesheet weeks queued for this resource ({len(week_dates)}): {week_dates}")

            for date_text in week_dates:
                logger.info(f"Opening timesheet week '{date_text}'...")
                if not await _click_week_cell(page, date_text):
                    logger.warning(f"Could not open timesheet week '{date_text}'; skipping it.")
                    continue
                await page.wait_for_timeout(1500)

                ts_id_text = await _extract_timesheet_id(page)
                if not ts_id_text:
                    logger.warning(f"Could not extract Timesheet ID for week '{date_text}'.")
                    continue

                mask = df["ID"].astype(str).str.strip() == ts_id_text
                if not mask.any():
                    logger.warning(f"Timesheet ID {ts_id_text} ({date_text}) not found in InvoiceSheet DataFrame.")
                    continue

                st_hours = _lookup_st_hours(df, mask)
                bill_rate_val, amount_val = await _extract_bill_rate_and_amount(page, st_hours)
                legal_entity_val = await _extract_legal_entity(page)
                combined_comments = await _extract_comments(page)

                logger.success(
                    f"Matched Timesheet ID {ts_id_text} ({date_text}, ST {st_hours}). "
                    f"Updating -> BillRate: {bill_rate_val}, Amount: {amount_val}, "
                    f"Legal Entity: {legal_entity_val}, Comment: {combined_comments}"
                )

                # float() only at the point of writing - the 2dp figure is already exact, and the
                # column's number format keeps the trailing zero visible in Excel
                if bill_rate_val is not None:
                    df.loc[mask, "BillRate"] = float(bill_rate_val)
                if amount_val is not None:
                    df.loc[mask, "Amount"] = float(amount_val)
                if legal_entity_val:
                    df.loc[mask, "Legal Entity"] = legal_entity_val
                if combined_comments:
                    df.loc[mask, "Comment"] = combined_comments

            # 5. Finish Resource: Click Cancel, then confirm on the Discard dialog
            logger.info("Finishing Resource detail view: clicking Cancel -> Discard...")
            cancel_btn = page.get_by_role("button", name="Cancel")
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()

                discard_btn = page.get_by_role("button", name="Discard")
                try:
                    await discard_btn.first.wait_for(state="visible", timeout=10000)
                    await discard_btn.first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except PlaywrightTimeoutError:
                    logger.warning("No 'Discard' confirmation appeared after 'Cancel'; continuing.")

            # Re-open USI Invoice Billing Schedule list page for next resource
            await navigate_to_invoice_schedule(page)

        except Exception as res_err:
            logger.error(f"Error processing Resource item {i+1}: {res_err}")
            try:
                await navigate_to_invoice_schedule(page)
            except Exception:
                pass

    # Save updated InvoiceSheet.xlsx (handling PermissionError if file is open in Excel)
    try:
        _write_report(df, excel_file)
        logger.success(f"Successfully updated InvoiceSheet.xlsx with extracted invoice data: {excel_file}")
        return excel_file
    except PermissionError:
        alt_file = excel_file.parent / "InvoiceSheet_Updated.xlsx"
        logger.warning(f"Could not overwrite {excel_file.name} because it is open in Microsoft Excel. Saving to {alt_file.name} instead...")
        _write_report(df, alt_file)
        logger.success(f"Saved updated report to: {alt_file}")
        return alt_file
