# Fieldglass Extraction Pipeline — Technical Reference

How timesheet data and printable timesheet documents are retrieved from SAP Fieldglass by calling
its endpoints directly, rather than by driving the portal interface.

| | |
|---|---|
| **Scope** | Deloitte USI · buyer code `DLT` · host `dlt.us.fieldglass.cloud.sap` |
| **Validated against** | July 2026 cycle — 452 timesheets, 97 resources |
| **Status** | Tested; not yet in production |
| **Figures** | Measured, not estimated |

---

## Contents

1. [Why not UI automation](#1-why-not-ui-automation)
2. [Pipeline architecture](#2-pipeline-architecture)
3. [Authentication and session](#3-authentication-and-session)
4. [Request 1 — the month list](#4-request-1--the-month-list)
5. [Request 2 — timesheet detail](#5-request-2--timesheet-detail)
6. [Request 3 — the PDF document](#6-request-3--the-pdf-document)
7. [Concurrency and load](#7-concurrency-and-load)
8. [Data model](#8-data-model)
9. [Test results](#9-test-results--july-2026)
10. [Engineering notes](#10-engineering-notes)

---

## 1. Why not UI automation

The obvious approach is to reproduce what a person does: open the timesheet list, click into a
resource, click into each week, open the Actions menu, click Print. At this scale that has three
problems.

**It is stateful.** Every step depends on the previous one leaving the page in the expected
condition. A slow postback, a re-rendered grid, or a stale element handle breaks the chain, and the
failure often surfaces several steps later.

**It is slow.** Roughly 450 timesheets a month, each needing several page loads before the useful
action, puts a full cycle in the region of four to six hours.

**It discovers work by navigating.** Finding out which timesheets exist requires walking the
interface, so discovery and retrieval are entangled.

Every one of those clicks ultimately issues an HTTP request. Issuing those requests directly removes
the navigation and leaves only the useful calls. Discovery separates cleanly from retrieval, each
retrieval becomes independent and retryable, and the work parallelises.

---

## 2. Pipeline architecture

The whole process reduces to three request types. The numbering is a real sequence — each stage
supplies the identifiers the next one needs.

| Stage | What it does | Cost |
|---|---|---|
| **1 — Month list** | One request returns every timesheet in the cycle with status, resource, week ending, hours, reference and internal record ID | 1 request · ~15 s · 452 rows |
| **2 — Timesheet detail** | One request per timesheet returns daily hours, billing lines, legal entity, worker ID and all comments | 452 requests · ~1.3 s each |
| **3 — PDF document** | One request per approved timesheet returns the printable document, identical to Actions → Print | ≤452 requests · ~20 s each |

Stage 1 is what makes the rest cheap: it establishes the complete work list in a single call, so
stages 2 and 3 are flat loops over known identifiers with no navigation between them.

Stages 2 and 3 are independent of each other. Data extraction can run on its own — daily, if wanted —
while document retrieval, an order of magnitude more expensive, runs only when documents are needed.

```
  login (once)
      │
      ▼
  ┌─────────────────┐   one request, whole cycle
  │ 1. MONTH LIST   │ ──────────────────────────────► 452 rows
  └─────────────────┘   status · resource · week · DLTTS ref · internal id
      │
      ├──────────────────────────────┐
      ▼                              ▼
  ┌─────────────────┐          ┌─────────────────┐
  │ 2. DETAIL       │          │ 3. PDF          │
  │ all statuses    │          │ approved+ only  │
  │ ~1.3 s each     │          │ ~20 s each      │
  └─────────────────┘          └─────────────────┘
      │                              │
      ▼                              ▼
   PostgreSQL                    SharePoint
```

---

## 3. Authentication and session

Authentication happens once, through a real browser session, because the sign-in form is the only
part of the flow that genuinely needs one.

```
POST /login.do
     username=<service account>&password=<secret>

→ Set-Cookie: JSESSIONID, SAPFG, InSite, sga, ...
```

Those cookies are persisted to a session state file and reused. Every subsequent call is issued
through an HTTP client that **shares the browser's cookie jar but renders nothing** — no page is
opened, no JavaScript executes, no assets are fetched. This is why a timesheet detail costs about
1.3 seconds rather than the several seconds a full page load would take.

One consequence matters and is easy to miss: because no JavaScript runs, any value the portal draws
client-side is **not** present as rendered markup — it exists only as the underlying data embedded in
the page. Parsing must target that data, not the markup a browser would eventually produce. See
[note 5](#5-badge-fields-exist-only-as-data-not-markup-silent).

> **Session expiry.** Sessions lapse within roughly an hour of issue, and a long document run can
> outlive one. An expired session does not return an error — it returns **HTTP 200 with the sign-in
> page**. Every response is therefore validated by content, and the run re-authenticates and resumes
> rather than failing, or worse, succeeding falsely.

---

## 4. Request 1 — the month list

The timesheet list is filtered to the cycle by setting the two date fields and submitting. The
response is an HTML page, but the useful content is a structured payload embedded within it, holding
**every row in the result set** — including rows the on-screen grid has not drawn, since it renders
lazily.

```
POST /time_sheet_list.do?cf=1
     filterStartDate=01/07/2026&filterEndDate=31/07/2026

→ embedded payload, one record per timesheet:
{"name":"status",         "value":"Invoiced"},
{"name":"time_sheet_ref", "value":"DLTTS03830900"},
{"name":"worker_name",    "value":"Abhilash, Domal"},
{"name":"end_date",       "value":"04/07/2026"},
{"name":"st_hours",       "value":"40.00"},
... plus the row's link, carrying the internal record ID:
   /time_sheet_detail.do?id=z260802023747344138749d8
```

Two identifiers come out of this and both are needed:

- **`time_sheet_ref`** (`DLTTS…`) — the human-facing reference, used for naming and reconciliation.
- **`id`** (`z…`) — Fieldglass's internal record identifier, and the only one the detail and document
  endpoints accept.

The tenant host is also read from that link rather than hard-coded, so the pipeline is not pinned to
one buyer's hostname.

> **Correctness guard.** The embedded payload is written once, at first render, and is *not* refreshed
> when filters change. Reading the page after filtering therefore returns a complete, well-formed set
> of rows for the *previous* range — a failure that looks exactly like success. The pipeline reads the
> filter response itself, then verifies that the returned week-ending dates actually fall in the
> requested range before proceeding. See notes [2](#2-the-list-payload-does-not-refresh-on-filtering-silent)
> and [4](#4-applying-a-filter-does-not-always-issue-a-request-silent).

---

## 5. Request 2 — timesheet detail

Each timesheet is fetched individually using its internal ID:

```
GET /time_sheet_detail.do?id=z260802023747344138749d8
    &buyerCode=DLT&sjkName=DLT&dataBaseType=sql&startFlow=true
```

The response carries the required values in two distinct shapes, and both must be read.

### Embedded record data

The header badge — status, timesheet ID, period, worker ID — is drawn by JavaScript from a data array
in the page. Since no JavaScript runs, this is parsed from the data directly:

```json
{"value":"Invoiced",                     "key":"Status"},
{"value":"DLTTS03830900",                "key":"Time Sheet ID"},
{"value":"28\/06\/2026 to 04\/07\/2026", "key":"Period"},
{"value":"DLTWK00120709",                "key":"Worker ID"}
```

### Rendered tables

Hours, billing and comments are server-rendered tables and are parsed as such.

```
Daily grid
Day        2/8 Sun  3/8 Mon  4/8 Tue  5/8 Wed  6/8 Thu  7/8 Fri  8/8 Sat    Total
ST /Hr       0h 0m   7h 28m    8h 0m    0h 0m    8h 0m    8h 0m    0h 0m   31h 28m

Accounting — Bill to Buyer
Rate Category / UOM   Bill Rate   Quantity   Days   Amount (INR)
ST /Hr                   283.75      31.47      -       8,928.67
Total                                                   8,928.67

Comments
11/08/2026 09:02 AM   Gonugunta, Aruna   Approved
08/08/2026 10:04 PM   Abhilash, Domal    UPTO of 32 min on 3/08 due to system issue…
```

### Fields captured

| Field | Source | Notes |
|---|---|---|
| `timesheet_id` | badge data | DLTTS reference, primary key |
| `worker_id` | badge data | DLTWK reference |
| `status` | badge data | Pending Approval / Approved / Invoiced / Paid |
| `period_start`, `period_end` | badge data | Week covered |
| daily hours ×7 | daily grid | Stored per day with real dates |
| `bill_rate`, `quantity`, `amount` | accounting | Amount taken from the Total row |
| `pay_rate`, `pay_amount` | accounting | Pay to Worker block |
| rate lines | accounting | Each line kept — see [note 8](#8-billing-can-span-multiple-rate-lines-silent) |
| `legal_entity`, `site`, `business_unit` | posting info | Absent for SOW-based resources |
| comments | comments table | All, with author and timestamp |

Each request is stateless and self-contained, so a failure affects only that timesheet and is retried
in isolation. Nothing is carried between iterations.

---

## 6. Request 3 — the PDF document

The portal's Actions → Print resolves to a single call to a document-generation servlet, which
returns `application/pdf` directly.

```
GET /Document2PDFServlet
    ?processMode=xml2pdf
    &docSource=<url-encoded> TimeSheetXMLServlet?loginId=<user>&timeSheetId=<internal id>
    &docXslt=xslt/timesheet/timesheet.xsl
    &filename=timesheet_DLTTS03830900.pdf
    &moduleId=70&cf=1

→ 200 application/pdf · ~36 KB · content-disposition: attachment
```

Four parameters are constants. `loginId` identifies the authenticated user and is stable, though it is
read from the page at run time rather than hard-coded so that a change surfaces as a clear failure.
**The only per-timesheet variable is the internal record ID** already obtained from stage 1.

The parameter naming reveals the mechanism: Fieldglass renders the timesheet as structured data and
transforms it into a document with an XSL stylesheet. That transform is where the ~20 seconds is
spent, and it is server-side work we cannot reduce.

> **Considered and rejected.** The data source in that URL, `TimeSheetXMLServlet`, would return the
> timesheet as structured data directly — faster and lighter than either of the other routes. It was
> tested across seven variations (with and without session parameters, with a referer, on both hosts,
> and via alternative processing modes). Every attempt returned the sign-in page. The servlet is
> reachable only from within Fieldglass's own infrastructure, so this route is not available and the
> detail page remains the source for data.

### Status filter

Documents are retrieved only for timesheets that have cleared approval — **Approved, Invoiced or
Paid**. Pending Approval is skipped, counted and reported so a later run collects it once approved.

Extracted **data**, by contrast, is retained for every timesheet in the cycle regardless of status.
This costs nothing extra — the row is already in the stage 1 payload — and it provides the prior state
needed to detect corrections and to observe a timesheet's progression through approval.

### File naming

```
Abhilash_Domal__2026-07-04__DLTTS03830900.pdf
└─ resource ──┘  └─ week end ┘  └─ timesheet ID ┘
```

Resource first groups a person's weeks together; the ISO date orders those weeks correctly; the
timesheet ID makes the name unique and joins the document to its data record.

---

## 7. Concurrency and load

Requests are issued a few at a time under a fixed concurrency limit. Because each request is
independent this is safe; because the tenant is shared production infrastructure, the limit is
deliberately conservative.

| Operation | Per item | Concurrency | Effective | Full cycle |
|---|---:|---:|---:|---:|
| Month list | — | 1 | — | ~15 s |
| Detail extraction | ~1.3 s | 3 | 0.33 s | **~2.5 min** |
| PDF retrieval | ~20 s | 3 | 5.6 s | **~45 min** |
| PDF retrieval | ~20 s | 5 | 3.0 s | ~25 min |

Concurrency 5 measured roughly 1.6× faster than 3, confirmed in both run orders to rule out
server warm-up effects. It also produced an occasional `HTTP 500`, which succeeded on immediate retry.
**Concurrency 3 is the chosen default:** on an unattended run the twenty minutes saved does not
justify additional load on shared production infrastructure.

A document costs roughly ten times what a data page costs. This asymmetry is the main argument for
keeping the two stages separate — data can be refreshed frequently at negligible cost, while documents
are retrieved once per timesheet and skipped thereafter.

---

## 8. Data model

A timesheet is not a flat record — it has seven days, potentially several billing lines, and any
number of comments. It is stored across four related tables.

| Table | Rows (July) | Grain |
|---|---:|---|
| `timesheets` | 452 | One per timesheet; keyed on the DLTTS reference |
| `timesheet_days` | 3,164 | Seven per timesheet, with date and minutes |
| `timesheet_rates` | 906 | Each bill and pay line |
| `timesheet_comments` | 142 | Each comment, with author and timestamp |

Writes are upserts keyed on the timesheet ID, and child rows are replaced rather than merged — the
portal is the single source of truth, so a re-extraction leaves exactly what the portal currently
shows. Re-running is therefore always safe and never duplicates.

A denormalised `comments_joined` column is also kept, holding all comments semicolon-separated, for
reporting that needs one row per timesheet.

---

## 9. Test results — July 2026

**452 timesheets · 97 resources · 3 min 04 s · 0 failures**

| Phase | Duration |
|---|---:|
| Authentication | 21.1 s |
| Month list — 452 rows | 15.4 s |
| Extract 452 details and store | 147.0 s |
| **Total** | **183.7 s** |

### Independent validation

Output was checked against itself rather than assumed correct. Each check compares values parsed from
different regions of the page, so agreement is meaningful evidence.

| Check | Discrepancies |
|---|---:|
| Billed quantity vs. hours summed from the daily grid | 0 / 452 |
| Amount vs. rate × quantity (single-line) | 0 / 450 |
| Multi-line total vs. sum of its lines | 0 / 2 |
| Timesheets with a complete seven-day grid | 452 / 452 |
| Negative amounts | 0 |

Total billed quantity across the cycle is **17,833.93 hours**. Hours summed independently from 3,164
individual day records total **17,833.92** — a 0.01 rounding difference. The two figures are parsed
from different parts of the page, so their agreement is strong evidence the parse is sound.

### Expected absences

Three records carry missing values, each verified against the portal as correct rather than a parsing
failure:

- **One timesheet with no rate or quantity** — genuinely bills 0.00, with no rate line at all.
- **Five timesheets with no legal entity** — an SOW-based resource, whose page carries "SOW Owner" in
  place of that field.
- **98 worker IDs against 97 names** — one individual holds two worker records, i.e. two contracts,
  not a duplicate.

All 452 July timesheets are at revision 0, so no client corrections arose in this cycle.

---

## 10. Engineering notes

Behaviours found during development that are not obvious from the interface. Those marked **SILENT**
are the important ones — they produce plausible, complete, wrong output rather than an error.

### 1. The nested query string must stay encoded (LOUD)

In the document request, `docSource` is itself a query string and must be passed as a single
percent-encoded value. Decoded, its parameters are parsed as top-level ones; the servlet then runs for
roughly 20 seconds before returning HTML instead of a PDF, which resembles a permissions problem
rather than a malformed request.

### 2. The list payload does not refresh on filtering (SILENT)

The embedded row data is written at first render and never updated when filters change. Reading the
page after filtering yields a full, well-formed set of rows for the *previous* range. The pipeline
reads the filter response directly, then validates that the returned week-ending dates fall in the
requested range before using them.

### 3. Date filters must be targeted by element ID (SILENT)

Locating the date fields by accessible label resolves to a neighbouring element. The typed value is
discarded without error and the portal's own default range remains in force. The fields are addressed
by ID, and the values are read back and asserted before submitting.

### 4. Applying a filter does not always issue a request (SILENT)

Fieldglass remembers a user's most recent filter. Re-requesting the same range leaves the fields
unchanged, so submitting fires nothing and a naive wait-for-response times out. All list responses are
captured, and the most recent one matching the requested range is used — whether it arrived from the
filter submission or the initial page load.

### 5. Badge fields exist only as data, not markup (SILENT)

Status, period and worker ID are drawn client-side from an embedded data array. A browser capture
shows them as rendered markup; a plain HTTP fetch does not, because no JavaScript runs. Parsing the
markup therefore works when testing through a browser and silently returns nothing in production. The
embedded data is parsed directly, with the markup as a fallback, so both capture methods behave
identically.

### 6. Attribute spacing varies between pages (SILENT)

The comments table is emitted as `summary="Comments">` on some pages and `summary="Comments" >` on
others. An exact tag match finds nothing on the second form and reports zero comments rather than
failing. Tags are matched tolerantly.

### 7. Non-working days render as a dash (SILENT)

The daily grid usually shows `0h 0m` but sometimes a bare `-`. Requiring every cell to parse as a
duration rejects the whole row, losing that timesheet's daily hours entirely. Rows are accepted when
most cells parse, and the dash is treated as no time recorded.

### 8. Billing can span multiple rate lines (SILENT)

When a rate changes part-way through a week, the accounting block carries two lines and a combined
total. Reading only the first line understates the amount — on the affected timesheets this omitted a
material sum.

```
ST /Hr   1,824.13   36.00   →   65,668.68
ST /Hr   1,787.65    9.00   →   16,088.85
Total                           81,757.53
```

The amount is taken from the Total row, the quantity is summed across lines, every line is stored
individually, and the timesheet is flagged with its line count so these cases can be reviewed rather
than hidden.

### 9. An expired session returns HTTP 200 (SILENT)

Fieldglass answers an unauthenticated request with the sign-in page and a 200 status, not a 401 or a
redirect. Trusting the status code would write several hundred HTML pages named `.pdf` and report the
run as successful. Every response is validated by content — documents by their `%PDF` signature, data
pages by the presence of the expected record — and the run re-authenticates and resumes when a lapse
is detected.

### 10. Documents are not byte-stable (INFO)

Requesting the same timesheet twice produces different bytes: Fieldglass stamps a generation timestamp
into each document. The two files differ by about 0.2% and are otherwise identical. Deduplication and
"already retrieved" checks therefore work on filename, never on a content hash, which would report
every document as changed on every run.

---

## Module reference

| Path | Responsibility |
|---|---|
| `automation/timesheet_pdf.py` | Month list retrieval and parsing; PDF endpoint; download orchestration |
| `automation/timesheet_data.py` | Detail page parsing; extraction orchestration |
| `db/postgres.py` | Schema, upserts, connection handling |
| `db/export.py` | Multi-sheet Excel export from the database |
| `main.py` | Commands — `data`, `pdfs`, `both` |
