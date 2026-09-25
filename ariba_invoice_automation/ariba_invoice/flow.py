"""Create a Standard Invoice for a PO and SAVE IT AS A DRAFT — over HTTP after a single browser login.

Never submits: the AribaWeb client refuses any control labelled Submit/Send, and this flow only ever
activates the review page's `_a="save"` button.

Resilience model (Ariba is slow and occasionally drops requests):
  * every step runs under a hard timeout;
  * every action first looks at the current page and only does what is still missing, so it can be re-run;
  * after a transient failure the page is re-read; if the step's check already passes, the flow moves on,
    otherwise the step is retried; the PDF upload finally falls back to a real browser tab;
  * business/safety check failures (FlowAbort) and expired sessions (SessionExpired) are never retried here —
    the caller decides (re-login and resume, or stop).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger
from playwright.async_api import BrowserContext

from .aribaweb import AribaWebError, AribaWebSession, browser_origin_tab
from .errors import FlowAbort, SessionExpired, clean_error
from .excel_input import InvoiceInput
from .html_tree import Node
from .portal import PortalApi

_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
# service period in a line description; seen so far:
#   "01-Aug-2026 to 31-Aug-2026" | "21 July 2026 to 31 July 2026" | "21-Aug-2026 - 31-Aug-2026"
PERIOD = re.compile(rf"(\d{{1,2}})[-\s]({_MON})[-\s](\d{{4}})\s*(?:to|-|–)\s*\d{{1,2}}[-\s]{_MON}[-\s]\d{{4}}", re.I)
LINE_COUNT = re.compile(r"(\d+) Line Items?,")
AMOUNT = r"([\d,]+\.\d{2})\s*INR"
Check = Callable[[], "tuple[bool, str]"]


@dataclass
class Run:
    steps: list[dict] = field(default_factory=list)
    result: dict = field(default_factory=dict)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


# ---------------------------------------------------------------------------------------------- page readers
def _line_rows(web: AribaWebSession) -> list[dict]:
    """Invoice line items: checkbox name, service-period month, subtotal field."""
    lines, seen = [], set()
    for cb in web.root.iter():
        if cb.tag != "input" or (cb.get("type") or "").lower() != "checkbox" or not cb.get("name"):
            continue
        tr = cb.closest("tr")
        if tr is None:
            continue
        m = PERIOD.search(tr.text())
        if not m or cb.get("name") in seen:
            continue
        seen.add(cb.get("name"))
        subtotal = tr.find(lambda n: n.tag == "input" and (n.get("value") or "").endswith(" INR"))
        lines.append({"checkbox": cb.get("name"), "description": tr.text()[:120],
                      "month": f"{m.group(2)[:3].title()}-{m.group(3)}",
                      "subtotal_field": subtotal.get("name") if subtotal is not None else None})
    return lines


def _checkbox_in_row(web: AribaWebSession, row_prefix: str) -> Node:
    for cb in web.root.iter():
        if cb.tag == "input" and (cb.get("type") or "").lower() == "checkbox" and cb.get("name"):
            tr = cb.closest("tr")
            if tr is not None and tr.text().startswith(row_prefix):
                return cb
    raise AribaWebError(f"checkbox in row {row_prefix!r} not found")


def _has_field(web: AribaWebSession, label: str, tag: str = "input") -> bool:
    try:
        web.field_by_label(label, tag)
        return True
    except AribaWebError:
        return False


def _field_value(web: AribaWebSession, label: str, tag: str = "input") -> str | None:
    try:
        n = web.field_by_label(label, tag)
        return n.text() if tag == "textarea" else n.get("value")
    except AribaWebError:
        return None


def _validation_messages(web: AribaWebSession) -> list[str]:
    """Sentences Ariba adds next to fields when it refuses a step (skips the hidden upload-error template)."""
    noise = ("attachments cannot exceed", "file paths for all file uploads", "Indicates required field", "DO NOT use")
    msgs = []
    for m in re.finditer(r"[^.:]{0,90}(must|cannot|exceeds?|not allowed|invalid|duplicate|greater than|less than|required)[^.]{0,120}\.?",
                         web.text(), re.I):
        t = m.group(0).strip()
        if t and not any(n in t for n in noise) and t not in msgs:
            msgs.append(t[:200])
    return msgs


def _on_review_page(web: AribaWebSession) -> bool:
    return "Confirm and submit this document" in web.text()


def _exit_dialog_open(web: AribaWebSession) -> bool:
    return "Save the invoice" in web.text() and "Delete the invoice" in web.text()


# ---------------------------------------------------------------------------------------------- step engine
class StepRunner:
    def __init__(self, run: Run, web: AribaWebSession) -> None:
        self.run = run
        self.web = web

    async def resync(self) -> None:
        if self.web.page is not None:
            await self.web.refresh()  # retried internally; raises SessionExpired if the session is gone

    async def __call__(self, name: str, action: Callable[[], Awaitable[None]], check: Check | None = None, *,
                       timeout: float = 300, attempts: int = 3,
                       fallback: Callable[[], Awaitable[None]] | None = None) -> None:
        t0 = time.perf_counter()
        logger.info(f"▶ {name}")
        note = ""
        try:
            for attempt in range(1, attempts + 1):
                try:
                    await asyncio.wait_for(action(), timeout)
                    break
                except (FlowAbort, SessionExpired):
                    raise
                except Exception as e:  # noqa: BLE001 — transient, timeout, half-loaded page …
                    logger.warning(f"  {name}: attempt {attempt}/{attempts} failed — {clean_error(e)}")
                    await self.resync()
                    if check and check()[0]:
                        note = " (applied despite the error)"
                        break
                    if attempt == attempts:
                        if fallback is None:
                            raise
                        logger.warning(f"  {name}: switching to browser fallback")
                        await asyncio.wait_for(fallback(), timeout * 2)
                        await self.resync()
                        note = " (browser fallback)"
                        break
                    await asyncio.sleep(5 * attempt)
            if check:
                ok, why = check()
                if not ok:
                    raise FlowAbort(f"{name}: {why}")
        except BaseException as e:
            dt = round(time.perf_counter() - t0, 2)
            self.run.steps.append({"step": name, "seconds": dt, "ok": False, "error": clean_error(e)})
            logger.error(f"✘ {name} ({dt}s)")
            raise
        dt = round(time.perf_counter() - t0, 2)
        self.run.steps.append({"step": name, "seconds": dt, "ok": True, "note": note.strip(" ()")})
        logger.success(f"✔ {name} ({dt}s){note}")


# ---------------------------------------------------------------------------------------------- the flow
async def create_draft_invoice(web: AribaWebSession, api: PortalApi, context: BrowserContext, inv: InvoiceInput,
                               invoice_number: str, pdf: Path, service_description: str, sac: str, comment: str,
                               run: Run, save: bool = True, existing_ok: bool = False,
                               upload_timeout_ms: float = 300_000) -> Run:
    res = run.result
    res |= {"invoice_number": invoice_number, "po": inv.po_number}
    step = StepRunner(run, web)
    state: dict = {}

    # 1 ── PO lookup (REST) ───────────────────────────────────────────────────────────────────
    async def find_po():
        po = await api.find_po(inv.po_number)
        state["doc_url"] = api.document_url(po["payloadId"])
        res["payload_id"] = po["payloadId"]
    await step("1. Find PO via REST (po-list-search)", find_po, lambda: ("doc_url" in state, "PO not resolved"))

    # 2 ── open PO + duplicate check ──────────────────────────────────────────────────────────
    async def open_po():
        await web.open_document(state["doc_url"])
    await step("2. Open PO (documentDetail)", open_po,
               lambda: (f"Purchase Order: {inv.po_number}" in web.text(), f"opened page is not PO {inv.po_number}"))
    if re.search(rf"Invoice:\s*{re.escape(invoice_number)}\b", web.text()):
        if existing_ok and re.search(rf"Draft Invoices:.*?Invoice:\s*{re.escape(invoice_number)}\b", web.text()):
            logger.success(f"draft {invoice_number} already on the PO (saved by the previous attempt) — nothing to do")
            res["status"] = "draft saved (verified after retry)"
            return run
        if save:
            raise FlowAbort(f"invoice {invoice_number} already exists on PO {inv.po_number} — not creating a duplicate")
        logger.warning(f"invoice {invoice_number} already exists on PO {inv.po_number} — continuing only because this is a dry run")

    # 3 ── Create Invoice → Standard Invoice ──────────────────────────────────────────────────
    async def create():
        if not _has_field(web, "Invoice #:"):
            await web.act(web.menu_pick("Create Invoice", "Standard Invoice"))
    await step("3. Create Invoice → Standard Invoice", create,
               lambda: (_has_field(web, "Invoice #:"), "invoice form did not open"))

    # 4 ── header fields + comment section ────────────────────────────────────────────────────
    def header_fields() -> dict[str, str]:
        return {web.field_by_label("Invoice #:").get("name"): invoice_number,
                web.field_by_label("Service Description:").get("name"): service_description,
                web.field_by_label("Tax Invoice Number:").get("name"): invoice_number}

    async def header():
        if not _has_field(web, "Comments:", "textarea"):
            await web.act(web.menu_pick("Add to Header", "Comment"), header_fields())
        elif _field_value(web, "Invoice #:") != invoice_number:
            await web.act(web.button(action="update").id, header_fields())
    await step("4. Header fields + Add to Header → Comment", header,
               lambda: (_field_value(web, "Invoice #:") == invoice_number and _has_field(web, "Comments:", "textarea")
                        and _field_value(web, "Tax Invoice Number:") == invoice_number,
                        "header fields / comment box not on the form"))

    # 5 ── comment + attachment section ───────────────────────────────────────────────────────
    def has_file_input() -> bool:
        return web.root.find(lambda n: n.tag == "input" and (n.get("type") or "").lower() == "file") is not None

    async def comment_and_attach_section():
        fields = {web.field_by_label("Comments:", "textarea").get("name"): comment}
        if not has_file_input():
            await web.act(web.menu_pick("Add to Header", "Attachment"), fields)
        elif _field_value(web, "Comments:", "textarea") != comment:
            await web.act(web.button(action="update").id, fields)
    await step("5. Comment + Add to Header → Attachment", comment_and_attach_section,
               lambda: (has_file_input() and _field_value(web, "Comments:", "textarea") == comment,
                        "comment / attachment section missing"))

    # 6 ── upload PDF ─────────────────────────────────────────────────────────────────────────
    def attached() -> bool:
        return web.root.find(lambda n: n.tag == "a" and n.text() == pdf.name) is not None

    async def upload():
        if attached():
            return
        tab = await browser_origin_tab(context)  # stub page on service.ariba.com; nothing is loaded from Ariba
        try:
            await web.upload_via_browser(web.button("Add Attachment").id, pdf, tab)
        finally:
            await tab.close()
    await step(f"6. Upload {pdf.name}", upload, lambda: (attached(), f"{pdf.name} not listed as attachment"),
               timeout=upload_timeout_ms / 1000 + 30, attempts=2)

    # 7 ── keep only the billing-month line ───────────────────────────────────────────────────
    async def delete_lines():
        lines = _line_rows(web)
        res.setdefault("lines_on_po", [l["description"] for l in lines])
        m_count = LINE_COUNT.search(web.text())
        if m_count and int(m_count.group(1)) != len(lines):
            raise FlowAbort(f"portal shows {m_count.group(1)} line items but only {len(lines)} could be read "
                            f"(unrecognised description format?): {[l['description'] for l in lines]}")
        keep = [l for l in lines if l["month"] == inv.month_start]
        if len(keep) != 1:
            raise FlowAbort(f"expected exactly one {inv.month_start} line, found {len(keep)}: {[l['description'] for l in lines]}")
        sub = web.root.find(lambda n: n.get("name") == keep[0]["subtotal_field"]) if keep[0]["subtotal_field"] else None
        remaining = sub.get("value") if sub is not None else None
        m_rem = re.search(r"[\d,]+\.\d{2}", remaining or "")
        left = _num(m_rem.group(0)) if m_rem else None
        res["line_remaining_on_po"] = left
        if left is not None and left <= 0:
            raise FlowAbort(f"the PO line for {inv.month_start} has 0.00 left to invoice — it is already fully invoiced "
                            f"(check the PO's Related Documents); not creating another invoice")
        if left is not None and inv.amount > left + 0.01:
            raise FlowAbort(f"Excel amount {inv.amount:,.2f} is more than the {left:,.2f} left on the PO line for "
                            f"{inv.month_start} (part of it is already invoiced?) — not creating the invoice")
        drop = [l for l in lines if l is not keep[0]]
        if drop:
            await web.act(web.button("Delete", after="Line Item Actions").id,
                          {l["checkbox"]: "1" for l in drop}, unset_fields=[keep[0]["checkbox"]])

    def one_line() -> tuple[bool, str]:
        lines = _line_rows(web)
        return (len(lines) == 1 and lines[0]["month"] == inv.month_start, f"lines now: {[l['description'] for l in lines]}")
    await step(f"7. Keep only the {inv.month_start} line, delete the rest", delete_lines, one_line)
    res["kept_line"] = _line_rows(web)[0]["description"]

    # 8 ── tax category (header chooser) ───────────────────────────────────────────────────────
    plan = inv.gst_plan  # e.g. [18% IGST]  or  [9% SGST, 9% CGST]  or  [0% CGST]
    GST_VALUE = re.compile(r"^\d+(\.\d+)?% .+ GST / [A-Z]+GST$")

    def line_sets() -> dict[str, str]:
        line = _line_rows(web)[0]
        sets = {_checkbox_in_row(web, "Tax Category").get("name"): "1", line["checkbox"]: "1"}
        if line["subtotal_field"]:
            sets[line["subtotal_field"]] = f"{inv.amount:,.2f} INR"
        return sets

    def gst_choosers() -> list[Node]:
        """Every tax-category chooser in page order: [header 'Tax Category', tax row 1, tax row 2, …]."""
        out = []
        for t in web.root.iter():
            if t.get("bh") == "PML" and t.get("_mid") and t.id:
                inp = t.find(lambda n: n.tag == "input")
                if inp is not None and GST_VALUE.match(inp.get("value") or ""):
                    out.append(t)
        return out

    def chooser_value(t: Node) -> str | None:
        inp = t.find(lambda n: n.tag == "input")
        return inp.get("value") if inp is not None else None

    def header_category() -> str | None:
        ch = gst_choosers()
        return chooser_value(ch[0]) if ch else None

    async def pick_gst():
        if header_category() != plan[0]:
            await web.act(web.menu_pick(None, plan[0], menu_id="invoiceMaterialLineTax"), line_sets())
    await step(f"8. Tax Category = {plan[0]}", pick_gst,
               lambda: (header_category() == plan[0], f"tax category shows {header_category()!r}"))

    # 9 ── Add to Included Lines: one tax row per category in the plan ─────────────────────────
    def tax_rows() -> list[Node]:
        return gst_choosers()[1:]

    def tax_amounts() -> list[float]:
        return [_num(m) for m in re.findall(r"Tax Amount:\s*" + AMOUNT, web.text())]

    async def include():
        while len(tax_rows()) < len(plan):  # counts existing rows first, so a retry never adds an extra one
            before = len(tax_rows())
            await web.act(web.button("Add to Included Lines").id, line_sets())
            if len(tax_rows()) == before:
                raise AribaWebError("Add to Included Lines did not add a tax row")
    await step(f"9. Add to Included Lines ({len(plan)} tax row{'s' if len(plan) > 1 else ''})", include,
               lambda: (len(tax_rows()) == len(plan), f"line has {len(tax_rows())} tax rows, expected {len(plan)}"))

    # 9a ── set each tax row's category (PAD: 2nd row of a CGST+SGST invoice becomes 9% Central GST / CGST)
    def rows_ok() -> tuple[bool, str]:
        vals = [chooser_value(t) for t in tax_rows()]
        return vals == plan, f"tax rows are {vals}, expected {plan}"

    async def set_row_categories():
        for i, (trigger, want) in enumerate(zip(tax_rows(), plan)):
            if chooser_value(trigger) != want:
                await web.act(web.menu_pick_on(trigger, want), line_sets())
                break  # page re-rendered: ids changed; the step engine re-checks and loops via retries
    if len(plan) > 1 or not rows_ok()[0]:
        for _ in range(len(plan)):
            if rows_ok()[0]:
                break
            await step(f"9a. Tax rows = {' + '.join(plan)}", set_row_categories, None)
        ok, why = rows_ok()
        if not ok:
            raise FlowAbort(why)

    def amounts() -> dict:
        """Line subtotal + every tax row as the portal currently shows them (Taxable Amount is an input field)."""
        def money(v):
            m = re.search(r"[\d,]+\.\d{2}", v or "")
            return _num(m.group(0)) if m else None
        line = _line_rows(web)[0]
        sub = web.root.find(lambda n: n.get("name") == line["subtotal_field"]) if line["subtotal_field"] else None
        rates = [float(r) for r in re.findall(r"Rate\(%\):\s*([\d.]+)", web.text())]
        taxes = tax_amounts()
        return {"line_subtotal": money(sub.get("value") if sub is not None else None),
                "taxable_amounts": [money(f.get("value")) for f in web.fields_by_label("Taxable Amount:")],
                "rates_percent": rates, "tax_rows": taxes,
                "tax_amount": round(sum(taxes), 2) if taxes else None}

    got = amounts()
    if got["tax_amount"] is None or abs(got["tax_amount"] - inv.expected_tax) > 0.05:
        logger.info(f"  tax shows {got}; pressing Update to let Ariba recalculate")
        sets = line_sets() | {f.get("name"): f"{inv.amount:,.2f} INR" for f in web.fields_by_label("Taxable Amount:")}
        await step("9b. Update (recalculate tax)", lambda: web.act(web.button(action="update").id, sets),
                   lambda: (bool(tax_amounts()), "tax rows disappeared after Update"))
        got = amounts()
    res |= got
    if abs((got["tax_amount"] or 0) - inv.expected_tax) > 0.05:
        raise FlowAbort(f"portal tax {got['tax_amount']} (rows {got['tax_rows']}, rates {got['rates_percent']}) ≠ "
                        f"Excel tax {inv.expected_tax:.2f} — line subtotal {got['line_subtotal']}, taxable "
                        f"{got['taxable_amounts']} (Excel amount {inv.amount:.2f}) — not saving")

    # 10 ── HSN/SAC + Next ────────────────────────────────────────────────────────────────────
    async def sac_next():
        if not _on_review_page(web):
            sac_fields = web.fields_by_label("HSN / SAC:")
            if not sac_fields:
                raise AribaWebError("no HSN / SAC field on the form")
            await web.act(web.button(action="next").id, {f.get("name"): sac for f in sac_fields})

    def review_ok() -> tuple[bool, str]:
        if not _on_review_page(web):
            return False, f"did not reach the review page; portal says: {_validation_messages(web) or web.page_errors() or web.text()[:300]}"
        return (invoice_number in web.text(), "review page does not show the invoice number")
    await step("10. HSN/SAC + Next (review page)", sac_next, review_ok)
    m_due = re.search(r"Amount Due:\s*" + AMOUNT, web.text())
    res["amount_due"] = _num(m_due.group(1)) if m_due else None

    # 11+ ── save (or discard) ────────────────────────────────────────────────────────────────
    if not save:
        async def discard():
            if not _exit_dialog_open(web):
                await web.act(web.button(action="exit").id)
            await web.act(web.link("Delete").id)
        await step("11. Dry run: Exit → Delete the invoice (nothing saved)", discard)
        res["status"] = "dry-run (discarded)"
        return run

    res["save_attempted"] = True
    saved = {}

    async def do_save():
        if _on_review_page(web):
            await web.act(web.button(text="Save", action="save").id)  # re-saving the same draft is harmless
        # the confirmation is only in the direct reply to Save; the re-read page no longer shows it
        from .html_tree import parse
        reply = parse(web.last_response).text() if web.last_response else ""
        m = re.search(r'Invoice "([^"]+)" is saved\. The saved invoice will be kept until ([^.]+)\.', reply + " " + web.text())
        if m:
            saved["until"] = m.group(2)
    await step("11. Save (draft)", do_save, lambda: ("until" in saved, "no 'is saved' confirmation after Save"))
    res["saved_until"] = saved["until"]

    async def exit_and_save():
        if _on_review_page(web):
            await web.act(web.button(action="exit").id)
        if _exit_dialog_open(web):
            await web.act(web.link("Save").id)
    await step("12. Exit → Save the invoice", exit_and_save,
               lambda: (not _on_review_page(web) and not _exit_dialog_open(web), "still inside the invoice wizard"))

    async def verify():
        await web.open_document(state["doc_url"])
    await step("13. Verify draft on PO page", verify,
               lambda: (bool(re.search(rf"Draft Invoices:.*?Invoice:\s*{re.escape(invoice_number)}\b", web.text())),
                        f"PO page does not list draft {invoice_number}"))
    res["status"] = "draft saved"
    return run
