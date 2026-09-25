# Cognizant SAP Ariba: Draft Invoice Automation (API-first)

Creates a **Standard Invoice saved as a draft** on SAP Business Network (Ariba) for a Cognizant PO,
using a row of the Excel tracker as input. **It never submits.**

The browser is only used to log in. Everything after that runs as HTTP requests on the logged-in
session: the REST API for the PO lookup, and the AribaWeb form endpoint for the invoice wizard.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
copy .env.example .env      # then fill in the Ariba username / password and paths
```

## Run

```powershell
# full flow up to the review page, then "Exit -> Delete the invoice" (nothing is saved)
.venv\Scripts\python run.py --invoice DI-27-4909 --suffix a --dry-run

# create the draft DI-27-4909a
.venv\Scripts\python run.py --invoice DI-27-4909 --suffix a
```

| Option | Meaning |
|---|---|
| `--invoice` | Tax invoice number exactly as in the tracker (column **Tax invoice**) |
| `--suffix` | Appended to the invoice number on the portal (e.g. `a` for test drafts) |
| `--pdf` | Explicit PDF. Default: `INVOICE_PDF_DIR/<invoice><suffix>.pdf`, falling back to `<invoice>.pdf` |
| `--month` | Override the billing month from the Excel, e.g. `"Sep'26"` (testing / corrections) |
| `--dry-run` | Run every step, then discard instead of saving (the duplicate check only warns) |
| `--keep-open N` | After the run, show the PO in the browser for N seconds |

Each run writes `reports/run_<invoice>_<timestamp>.json` (inputs, per-step timings, result) and
`logs/run_<timestamp>.log`.

## What is taken from the Excel row

| Portal field | Source |
|---|---|
| PO | `PO / SOW No.` |
| Invoice # and Tax Invoice Number | `Tax invoice` + `--suffix` |
| Line to keep | `month` (e.g. `Aug'26` keeps the line whose period starts in Aug-2026; all others are deleted) |
| Line subtotal | `Inv. Amt.` |
| Tax rows (PAD logic) | IGST > 0, CGST/SGST blank → **18% Integrated GST / IGST** · CGST & SGST > 0, IGST blank → **two tax rows: 9% State GST / SGST + 9% Central GST / CGST** · all blank → **0% Central GST / CGST** · any other combination → stops |
| Comment | `COMMENT_TEMPLATE` with `{month}` |
| Service description, HSN/SAC | `.env` (`SERVICE_DESCRIPTION`, `SAC_CODE`) |
| Attachment | the PDF found as above |

## Safety checks (the run aborts before Save if any fails)

- The PO already lists an invoice with the same number (draft or sent): no duplicate is created.
- There isn't exactly one PO line for the billing month, or the portal's line count differs from the lines read.
- The month's PO line has 0.00 left, or less than the Excel amount (already invoiced).
- After Add to Included Lines, the portal's **Tax Amount** differs from the Excel tax (IGST, or CGST + SGST) by more than 0.05.
- The review page doesn't show the invoice number.
- No `Invoice "<no>" is saved` confirmation appears, or the PO page doesn't list the draft afterwards.
- Any control labelled **Submit** / **Send** (or `_a="submit"`) is refused by the HTTP client itself.

## Measured performance (2026-09-25, DI-27-4909b on PO C9827-R66)

| Part | Time |
|---|---|
| Login (browser) | 22–70 s (Ariba-dependent) |
| Find PO → open PO → invoice form → header/comment | ~10 s |
| PDF upload (3.7 MB) | 19–97 s (Ariba-dependent) |
| Lines, GST, SAC, Next, Save, Exit, verify | ~21 s |
| **End to end** | **108 s** (status: draft saved and verified on the PO) |

## Error handling

| Situation | What happens |
|---|---|
| Cookie-consent popup (login page, homepage, any tab) | Closed with **Accept All** as soon as a page loads, and again whenever it blocks a click |
| Login link ignores a click / page slow | Next → Enter → Next (15 s apart), Sign in → Enter → Sign in (40 s apart); then the whole login is retried on a fresh tab (up to 3×). A wrong password or locked account is **not** retried |
| Network error, timeout, HTTP 5xx on a GET (token, PO search, page reload) | Retried with backoff 3 s → 6 s → 12 s |
| Network error / timeout on a form POST (may or may not have been applied) | The page is re-read. If the step's check already passes, the flow moves on; otherwise the step is retried (every action only does what is still missing, so repeating it is safe) |
| A step hangs | Every step has a hard time limit (5 min each, upload 5.5 min) and is then treated like a timeout |
| PDF upload | Ariba's edge drops multi-MB uploads from non-browser HTTP clients, so the multipart POST is sent with the browser's own network stack (`fetch()` from a stub page on the service.ariba.com origin — no Ariba page is loaded, no UI clicks). 2 attempts, 5 min cap each |
| Session expired / Ariba error page | Detected on every page load. The run logs in again and starts over (up to 3 attempts). If Save had already been attempted, the new attempt accepts an existing **draft** with the same number instead of creating a duplicate |
| Business/safety check fails (duplicate, month line, tax mismatch, no review page) | Stops immediately, nothing saved, not retried |
| Any failure | Secret-free message (Playwright call logs with cookies are stripped); the last page HTML is saved to `logs/last_page_*.html` for diagnosis |

## How it works

| # | Step | Request |
|---|---|---|
| 0 | Login | Playwright: `Supplier.aw` → username → password → portal dashboard |
| 1 | Find PO | `POST …/data-service/v1/po-list-search` (Bearer token from `/tpx/ingress/auth/v1/token`, kept in memory only) → `payloadId` |
| 2 | Open PO | `GET Supplier.aw/ad/documentDetail?docPayload=…` + SSO hand-off form |
| 3–12 | Invoice wizard | AribaWeb `POST /Supplier.aw/<app>/aw` (form fields + `awsn=<control>`), full page re-read after each step |
| 13 | Verify | Re-open the PO; it must list `Draft Invoices: Invoice: <no>` |

Full protocol notes: [docs/cognizant_ariba_invoice_runbook.md](docs/cognizant_ariba_invoice_runbook.md).

## Layout

```
run.py                      CLI, timing, report
ariba_invoice/config.py     settings from .env
ariba_invoice/excel_input.py  Excel row -> InvoiceInput, GST rule, PDF lookup
ariba_invoice/portal.py     login + REST (token, po-list-search)
ariba_invoice/aribaweb.py   AribaWeb form-POST client (+ submit guard)
ariba_invoice/html_tree.py  dependency-free HTML parser used by the client
ariba_invoice/flow.py       the invoice steps and checks
```
