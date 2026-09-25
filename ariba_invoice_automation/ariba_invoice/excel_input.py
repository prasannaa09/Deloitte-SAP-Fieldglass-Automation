"""Map a row of the Cognizant Excel tracker to the values the invoice needs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import openpyxl

GST_0_CGST = "0% Central GST / CGST"
GST_18_IGST = "18% Integrated GST / IGST"
GST_9_SGST = "9% State GST / SGST"
GST_9_CGST = "9% Central GST / CGST"


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(",", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class InvoiceInput:
    tax_invoice_no: str          # column E "Tax invoice" (as in the tracker)
    po_number: str               # column G "PO / SOW No."
    month_label: str             # e.g. "Aug'26"
    month_start: str             # e.g. "Aug-2026" – used to pick the PO line
    amount: float                # "Inv. Amt."
    cgst: float
    sgst: float
    igst: float
    sez_or_regular: str
    employee: str

    @property
    def gst_plan(self) -> list[str]:
        """Tax category per tax row on the invoice line, as in the production PAD flow:

        * IGST > 0, CGST and SGST blank  -> [18% Integrated GST / IGST]
        * CGST > 0 and SGST > 0, IGST blank -> [9% State GST / SGST, 9% Central GST / CGST]  (two tax rows)
        * all three blank                -> [0% Central GST / CGST]
        * anything else                  -> not supported (raises)
        """
        c, s, i = self.cgst > 0, self.sgst > 0, self.igst > 0
        if i and not c and not s:
            return [GST_18_IGST]
        if c and s and not i:
            return [GST_9_SGST, GST_9_CGST]
        if not (c or s or i):
            return [GST_0_CGST]
        raise ValueError(f"unsupported GST combination for {self.tax_invoice_no}: "
                         f"CGST={self.cgst} SGST={self.sgst} IGST={self.igst}")

    @property
    def gst_category(self) -> str:
        return " + ".join(self.gst_plan)

    @property
    def expected_tax(self) -> float:
        return round(self.igst + self.cgst + self.sgst, 2)


def _month_start(label: str) -> str:
    m = re.match(r"\s*([A-Za-z]{3})[A-Za-z]*['’\s-]*(\d{2,4})", label or "")
    if not m:
        raise ValueError(f"cannot read month {label!r}")
    year = int(m.group(2))
    year = year + 2000 if year < 100 else year
    return f"{m.group(1).title()}-{year}"


def load_invoice(excel: Path, sheet: str, tax_invoice_no: str) -> InvoiceInput:
    wb = openpyxl.load_workbook(excel, data_only=True, read_only=True)
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    col = {h: i for i, h in enumerate(header) if h}
    need = ["Tax invoice", "PO / SOW No.", "month", "Inv. Amt.", "CGST", "SGST", "IGST"]
    missing = [n for n in need if n not in col]
    if missing:
        raise ValueError(f"Excel sheet {sheet!r} is missing columns {missing}")
    matches = [r for r in rows if r and str(r[col["Tax invoice"]] or "").strip() == tax_invoice_no]
    wb.close()
    if not matches:
        raise ValueError(f"invoice {tax_invoice_no!r} not found in {excel.name}")
    if len(matches) > 1:
        raise ValueError(f"invoice {tax_invoice_no!r} appears {len(matches)} times in {excel.name}")
    r = matches[0]
    month = str(r[col["month"]] or "").strip()
    return InvoiceInput(
        tax_invoice_no=tax_invoice_no,
        po_number=str(r[col["PO / SOW No."]]).strip(),
        month_label=month,
        month_start=_month_start(month),
        amount=round(_num(r[col["Inv. Amt."]]), 2),
        cgst=_num(r[col["CGST"]]),
        sgst=_num(r[col["SGST"]]),
        igst=_num(r[col["IGST"]]),
        sez_or_regular=str(r[col["SEZ / Regular"]] or "").strip() if "SEZ / Regular" in col else "",
        employee=str(r[col["Name"]] or "").strip() if "Name" in col else "",
    )


def find_pdf(pdf_dir: Path, *names: str) -> Path:
    """First existing <name>.pdf in pdf_dir (exact file name match, case-insensitive)."""
    files = {p.name.lower(): p for p in pdf_dir.glob("*.pdf")}
    for n in names:
        p = files.get(f"{n}.pdf".lower())
        if p:
            return p
    raise FileNotFoundError(f"no PDF named {' / '.join(n + '.pdf' for n in names)} in {pdf_dir}")


def today_label() -> str:
    return datetime.now().strftime("%d %b %Y")
