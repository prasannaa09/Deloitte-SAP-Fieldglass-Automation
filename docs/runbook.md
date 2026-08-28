# Runbook — Timesheet Extraction Pipeline

Everything needed to run, test and inspect the pipeline. All commands are run from the project root
`c:\Deloitte - SAP Fieldglass`.

> Use the virtual environment's interpreter: `.venv\Scripts\python.exe`. Substituting a system
> `python` will fail on missing packages.

---

## What the pipeline currently does

| Stage | Source | Destination | Coverage |
|---|---|---|---|
| **Month list** | Fieldglass timesheet list, filtered to the cycle | in memory | All timesheets, all statuses |
| **Data extraction** | Timesheet detail page, one request each | PostgreSQL `fieldglass` | **All statuses**, including Pending Approval |
| **PDF retrieval** | Document servlet, one request each | `downloads/timesheets/YYYY/MM/` | **Approved / Invoiced / Paid only** |
| **PDF merge** | The weekly PDFs already on disk | `downloads/timesheets/YYYY/MM/merged/` | One document per resource |
| **Excel export** | PostgreSQL | `reports/Timesheets_YYYY_MM.xlsx` | Whatever is stored |

Data is retained for every timesheet regardless of status, so a timesheet's progression through
approval is visible and corrections can be detected later. Documents are retrieved only after
approval; Pending Approval is skipped, counted and reported for a later run.

Re-running is always safe. Data writes are upserts keyed on the timesheet ID; documents already on
disk are skipped by filename; merged documents are rebuilt from their weekly sources every time, so
weeks collected on a later run are folded in without duplicating what was already there.

**Not yet implemented:** SharePoint upload, correction/revision comparison. Documents currently land
in the local folder structure that mirrors the intended SharePoint layout.

---

## One-time setup

Already done on this machine; listed for a rebuild.

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

`.env` must contain both sets of credentials (the file is gitignored):

```ini
SAP_URL=https://www.us.fieldglass.cloud.sap/
SAP_USERNAME=<user>
SAP_PASSWORD=<secret>

PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=<secret>
PG_DATABASE=fieldglass
```

The database and tables are created automatically on first run — no manual SQL needed.

---

## Commands

All commands default to the **previous calendar month**. Pass `--month MM/YYYY` for any other.

```bash
.venv\Scripts\python.exe cli.py --help
```

### Before a run — verify the environment

```bash
.venv\Scripts\python.exe cli.py check
```

Checks the database connection, creates/verifies the schema, reports row counts, and confirms
sign-in to Fieldglass. Takes about 30 seconds. Run this first if anything has changed.

### Extract data into PostgreSQL

```bash
.venv\Scripts\python.exe cli.py data                      # previous month
.venv\Scripts\python.exe cli.py data --month 07/2026      # a specific month
.venv\Scripts\python.exe cli.py data --limit 5            # smoke test, 5 timesheets
.venv\Scripts\python.exe cli.py data --concurrency 5      # faster, heavier on SAP
```

**~3 minutes** for a full month (452 timesheets). Safe to re-run.

### Download PDFs

```bash
.venv\Scripts\python.exe cli.py pdfs                      # previous month
.venv\Scripts\python.exe cli.py pdfs --limit 5            # smoke test
.venv\Scripts\python.exe cli.py pdfs --month 07/2026
```

**~45 minutes** for a full month at the default concurrency of 3. Documents already on disk are
skipped, so re-running after an interruption resumes rather than restarting.

Output: `downloads/timesheets/2026/07/` plus `manifest_2026_07.csv` recording the outcome of every
timesheet — downloaded, already present, skipped as pending, or failed.

Add `--merge` to fold each resource's weeks into a single document as soon as the download finishes.

### Merge PDFs by resource

Turns a month of weekly timesheets — 452 files across 97 people — into one document per person, with
the weeks in date order and a bookmark on each.

```bash
.venv\Scripts\python.exe cli.py merge                     # previous month
.venv\Scripts\python.exe cli.py merge --month 07/2026
.venv\Scripts\python.exe cli.py merge --no-bookmarks
.venv\Scripts\python.exe cli.py pdfs --month 07/2026 --merge   # download, then merge
```

**Seconds**, and entirely offline — no browser, no portal session. That is what makes it the right
thing to re-run after timesheets clear approval: download the new weeks, merge again, done.

Output: `downloads/timesheets/2026/07/merged/` plus `merged_manifest_2026_07.csv`, listing for each
resource the weeks that went in, how many were expected, and whether the document is complete. The
weekly PDFs are left exactly where they are — they remain the source of truth, and each merged
document is rebuilt from them on every run rather than appended to.

**Grouping and the name-collision guard.** Resources are grouped by worker name, which is what gives
one document per person. Name alone is not safe on its own, so when the month's data is in PostgreSQL
the merge cross-checks each name against its Fieldglass worker records:

| Situation | What happens |
|---|---|
| One name, one worker id | Merged normally |
| One name, several worker ids, **weeks do not overlap** | Treated as a mid-cycle re-badge — one person, one document, every worker id recorded in the manifest |
| One name, several worker ids, **weeks overlap** | Treated as two different people — split into one document per worker id, suffixed `__DLTWK…`, and logged as a warning |

July 2026 contains one real instance of the middle case: `Sahare, Avinash A` was reissued from
`DLTWK00121001` to `DLTWK00156582` on 26/07 with a rate change, and their five weeks stay in a single
document. Without the database the merge falls back to grouping on name alone and says so.

### Both, in one session

```bash
.venv\Scripts\python.exe cli.py both
```

Fetches the month list once and shares it: data first (~3 min), then PDFs (~45 min). Sequential by
design — running them in parallel would double the load on Fieldglass, and the cheap data pass
should not depend on the long document run completing.

Add `--merge` to finish with one document per resource:

```bash
.venv\Scripts\python.exe cli.py both --month 07/2026 --merge
```

### Excel workbook

```bash
.venv\Scripts\python.exe cli.py export
.venv\Scripts\python.exe cli.py export --out reports\July.xlsx
```

Writes 5 sheets: Summary, Timesheets (one row each, Sun–Sat hours as columns), Daily Hours,
Comments, Rate Lines. If the target workbook is open in Excel, it writes to `..._new.xlsx` instead
of failing.

### Data quality report

```bash
.venv\Scripts\python.exe cli.py study
```

Read-only, no portal access. Reports coverage, status breakdown, weeks, completeness, financial
totals, five integrity checks, and lists any multi-rate timesheets flagged for review. Exits
non-zero if an integrity check fails, so it can gate a pipeline.

---

## Expected output

`cli.py study` on a healthy July cycle:

```
== COVERAGE ==
  timesheets     452
  workers        98
  day rows       3164
  rate lines     906
  comments       142

== INTEGRITY (all should be 0) ==
  OK  quantity <> logged hours     0
  OK  amount <> rate x qty         0
  OK  multi-line <> sum of lines   0
  OK  incomplete day grids         0
  OK  negative amounts             0
```

Three completeness warnings are **expected and correct**, verified against the portal:

- `bill_rate` / `quantity` missing on **1** — that timesheet genuinely bills 0.00.
- `legal_entity` missing on **5** — an SOW-based resource whose page has "SOW Owner" instead.
- Workers reads **98** against 97 names — one individual holds two worker records.

---

## Database

Connect:

```bash
"C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -d fieldglass
```

Or point pgAdmin at `localhost:5432`, database `fieldglass`.

### Tables

```sql
\dt
--  timesheets           one per timesheet, keyed on the DLTTS reference
--  timesheet_days       seven per timesheet
--  timesheet_rates      each bill and pay line
--  timesheet_comments   each comment, with author and timestamp
```

### Everything for one timesheet

```sql
SELECT * FROM timesheets WHERE timesheet_id = 'DLTTS03902647';

SELECT day_name, day_date, hours_text, minutes
FROM timesheet_days WHERE timesheet_id = 'DLTTS03902647' ORDER BY day_index;

SELECT entered_at, author, comment_text
FROM timesheet_comments WHERE timesheet_id = 'DLTTS03902647' ORDER BY comment_index;

SELECT party, line_index, category, rate, quantity, amount
FROM timesheet_rates WHERE timesheet_id = 'DLTTS03902647' ORDER BY party, line_index;
```

### One row per timesheet, week across columns

```sql
SELECT t.timesheet_id, t.worker_name, t.status,
       t.period_start, t.period_end,
       t.bill_rate, t.quantity, t.amount,
       max(d.hours_text) FILTER (WHERE d.day_name='Sun') AS sun,
       max(d.hours_text) FILTER (WHERE d.day_name='Mon') AS mon,
       max(d.hours_text) FILTER (WHERE d.day_name='Tue') AS tue,
       max(d.hours_text) FILTER (WHERE d.day_name='Wed') AS wed,
       max(d.hours_text) FILTER (WHERE d.day_name='Thu') AS thu,
       max(d.hours_text) FILTER (WHERE d.day_name='Fri') AS fri,
       max(d.hours_text) FILTER (WHERE d.day_name='Sat') AS sat,
       t.total_worked, t.legal_entity, t.comments_joined
FROM timesheets t JOIN timesheet_days d USING (timesheet_id)
GROUP BY t.timesheet_id, t.worker_name, t.status, t.period_start, t.period_end,
         t.bill_rate, t.quantity, t.amount, t.total_worked, t.legal_entity, t.comments_joined
ORDER BY t.worker_name, t.period_start;
```

### Totals per worker

```sql
SELECT worker_name, worker_id, count(*) AS weeks,
       sum(quantity) AS hours, sum(amount) AS billed
FROM timesheets
GROUP BY worker_name, worker_id
ORDER BY billed DESC;
```

### Multi-rate timesheets (flagged, not errors)

```sql
SELECT t.timesheet_id, t.worker_name, t.period_start, t.period_end,
       t.amount AS total, r.line_index, r.rate, r.quantity, r.amount AS line_amount
FROM timesheets t JOIN timesheet_rates r USING (timesheet_id)
WHERE t.rate_line_count > 1 AND r.party = 'bill'
ORDER BY t.timesheet_id, r.line_index;
```

### Still awaiting approval — no document retrieved yet

```sql
SELECT timesheet_id, worker_name, period_end, quantity, amount
FROM timesheets
WHERE status NOT IN ('Approved', 'Invoiced', 'Paid')
ORDER BY worker_name, period_end;
```

### Integrity checks — each should return no rows

```sql
-- billed quantity vs hours actually logged
SELECT t.timesheet_id, t.quantity, sum(d.minutes)/60.0 AS logged
FROM timesheets t JOIN timesheet_days d USING (timesheet_id)
GROUP BY t.timesheet_id, t.quantity
HAVING abs(t.quantity - sum(d.minutes)/60.0) > 0.02;

-- amount vs rate x quantity, single-rate timesheets only
SELECT timesheet_id, bill_rate, quantity, amount
FROM timesheets
WHERE rate_line_count = 1 AND abs(amount - bill_rate * quantity) > 1.20;

-- multi-rate total vs the sum of its own lines
SELECT t.timesheet_id, t.amount
FROM timesheets t
WHERE t.rate_line_count > 1
  AND abs(t.amount - (SELECT sum(amount) FROM timesheet_rates r
                      WHERE r.timesheet_id = t.timesheet_id AND r.party = 'bill')) > 0.02;

-- every timesheet should have exactly seven days
SELECT timesheet_id, count(*) FROM timesheet_days
GROUP BY timesheet_id HAVING count(*) <> 7;
```

### Reset a month (re-extract from scratch)

Rarely needed — the pipeline upserts. Child rows cascade on delete.

```sql
DELETE FROM timesheets WHERE period_end BETWEEN DATE '2026-07-01' AND DATE '2026-08-07';
```

---

## Testing

```bash
.venv\Scripts\python.exe -m pytest -q            # unit tests
.venv\Scripts\python.exe -m mypy automation db   # type check
```

Smoke test against the live portal without committing to a full run:

```bash
.venv\Scripts\python.exe cli.py data --limit 3
.venv\Scripts\python.exe cli.py pdfs --limit 3
.venv\Scripts\python.exe cli.py study
```

`--limit` is for smoke tests only. Rows beyond the limit are not processed and do not appear in the
manifest, so never use it for a real cycle.

---

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `Authentication failed` | Wrong or changed credentials | Check `SAP_USERNAME` / `SAP_PASSWORD` in `.env` |
| `Session expired` in the log, then a fresh login | Normal | None — sessions lapse within about an hour and the run re-authenticates |
| `database "fieldglass" does not exist` | First run | None — it is created automatically |
| `password authentication failed` | Wrong `PG_PASSWORD` | Correct it in `.env` |
| `No Time Sheet list response matched …` | Filter did not apply | Re-run; if it persists the list page layout has changed |
| `Permission denied: …xlsx` | Workbook open in Excel | None — the export writes `..._new.xlsx` instead |
| PDF run reports failures | Usually transient `HTTP 500` under load | Re-run; completed files are skipped. Lower `--concurrency` if it recurs |
| Timesheets missing from a run | Still Pending Approval | Expected — re-run once approved |

Logs are written to `logs/`. Failure screenshots go to `screenshots/`.

---

## Reference

- Technical design and endpoint detail: [extraction-pipeline.md](extraction-pipeline.md)
- `automation/timesheet_pdf.py` — month list, PDF endpoint, download orchestration
- `automation/timesheet_data.py` — detail page parsing, extraction orchestration
- `db/postgres.py` — schema and upserts
- `db/export.py` — Excel export
- `automation/timesheet_merge.py` — merging weekly PDFs into one document per resource
- `cli.py` — command line entry point, the only way the pipeline is run
