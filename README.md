# SAP Fieldglass Timesheet Pipeline

Extracts a cycle month of approved timesheets out of **SAP Fieldglass** — structured data into
PostgreSQL, the printable PDF of every timesheet onto disk, and both into an Excel workbook for
review.

Replaces a legacy Microsoft Power Automate Desktop bot.

---

## How it works

The pipeline does not drive the Fieldglass UI. Clicking through menus and waiting on an export
button proved slow and brittle, so each stage calls the endpoint the portal itself calls:

| Stage | Source | Destination |
|---|---|---|
| **Month list** | Time Sheet list, filtered to the cycle | in memory, shared by the stages below |
| **Data extraction** | Timesheet detail page, one request each | PostgreSQL `fieldglass` |
| **PDF retrieval** | `Document2PDFServlet`, one request each | `downloads/timesheets/YYYY/MM/` |
| **PDF merge** | The weekly PDFs already on disk | `downloads/timesheets/YYYY/MM/merged/` |
| **Excel export** | PostgreSQL | `reports/Timesheets_YYYY_MM.xlsx` |

Data is kept for every timesheet whatever its status. PDFs are retrieved only once a timesheet is
Approved, Invoiced or Paid; anything still pending is counted and reported for a later run.

Re-running is always safe: data writes are upserts keyed on the timesheet ID, PDFs already on disk
are skipped by filename, and merged documents are rebuilt from their weekly sources every time.

Playwright is still used, but only to sign in and hold an authenticated session.

---

## Commands

```bash
.venv\Scripts\python.exe cli.py check     # verify database, schema and portal access
.venv\Scripts\python.exe cli.py data      # extract a month into PostgreSQL
.venv\Scripts\python.exe cli.py pdfs      # download approved timesheet PDFs
.venv\Scripts\python.exe cli.py both      # data then PDFs, sharing one list fetch
.venv\Scripts\python.exe cli.py merge     # one PDF per resource for the month
.venv\Scripts\python.exe cli.py export    # write the Excel workbook
.venv\Scripts\python.exe cli.py study     # coverage, completeness and integrity report
```

Every command defaults to the previous calendar month; pass `--month MM/YYYY` for another. A full
month is roughly 3 minutes for data, 45 minutes for PDFs, and seconds to merge.

A typical month:

```bash
.venv\Scripts\python.exe cli.py check
.venv\Scripts\python.exe cli.py both --month 07/2026 --merge
.venv\Scripts\python.exe cli.py export --month 07/2026
```

See [docs/runbook.md](docs/runbook.md) for the operational detail and
[docs/extraction-pipeline.md](docs/extraction-pipeline.md) for how the extraction works.

---

## Directory structure

```
├── cli.py                    # the entry point; every command lives here
├── automation/
│   ├── browser.py            # Playwright browser and context lifecycle
│   ├── login.py              # sign-in and session reuse
│   ├── navigation.py         # home page readiness check
│   ├── timesheet_pdf.py      # month list + weekly PDF retrieval
│   ├── timesheet_data.py     # detail page extraction into PostgreSQL
│   └── timesheet_merge.py    # weekly PDFs -> one document per resource
├── db/
│   ├── postgres.py           # schema, connection and upserts
│   └── export.py             # PostgreSQL -> Excel workbook
├── config/settings.py        # pydantic-settings environment loader
├── utils/                    # logging setup and date helpers
├── docs/                     # runbook and pipeline reference
├── tests/
│
├── downloads/                # timesheet PDFs (git-ignored)
├── reports/                  # generated workbooks (git-ignored)
├── logs/, screenshots/       # run output (git-ignored)
```

---

## Setup

Requires **Python 3.12+** and a reachable **PostgreSQL** instance.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in credentials:

```env
SAP_URL=https://www.us.fieldglass.cloud.sap/
SAP_USERNAME=your_username
SAP_PASSWORD=your_password

PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=your_password
PG_DATABASE=fieldglass

BROWSER_TYPE=chromium
HEADLESS=true
DEFAULT_TIMEOUT=30000.0
```

Then confirm everything is reachable before a long run:

```bash
.venv\Scripts\python.exe cli.py check
```

The database and schema are created on first use, so `check` reporting a missing database is not
a failure.

---

## Development

```bash
.venv\Scripts\python.exe -m pytest                                   # tests
.venv\Scripts\python.exe -m mypy cli.py automation/ db/ utils/ config/ tests/
```
