# Cognizant — SAP Ariba Standard Invoice (Draft) Runbook

Creating a **Standard Invoice saved as Draft** against a Cognizant PO on SAP Business Network
(SAP Ariba supplier side), and the HTTP requests behind each step, so the automation can be
**API-first**.

> **Safety rule:** the invoice is only ever **saved as a draft**. The review page shows
> **Submit** right next to **Save**. Automation must never trigger the Submit action.

Findings come from a live, observed, read-only traffic capture on **2026-09-24**
(PO `C9827-R89`, dummy invoice `DI-27-2718a`). **No invoice or draft was saved.** Each
finding is marked **Confirmed** (request seen on the wire) or **Inferred** (from page
structure or the protocol pattern; must be confirmed before relying on it).

---

## 1. Inputs

| Value | Example | Source |
|---|---|---|
| PO number | `C9827-R89` | Excel tracker, column G "PO / SOW No." |
| Invoice number | `DI-27-2718a` | Excel tracker, column E "Tax invoice" |
| Invoice date | today, format `24 Sep 2026` | runtime |
| Service description | `Man Power Industry` | config |
| Tax Invoice Number | same as invoice number | Excel tracker, column E |
| Header comment | `Being invoiced Raised for the month of <Mon'YY>` | PDD rule 5.3 |
| Attachment | `<INVOICE_PDF_DIR>\<invoice no>.pdf` | file named after the invoice number |
| Month to keep | e.g. `Sep-2026` | billing month |
| GST type | see rule below | Excel tracker, CGST / SGST / IGST columns |
| HSN / SAC | `998313` | config |

**GST rule (PDD section 5.1):**

| Excel values | Tax Category to choose |
|---|---|
| SEZ, or CGST / SGST / IGST all blank | `0% Integrated GST / IGST` |
| IGST > 0 | `18% Integrated GST / IGST` |
| CGST > 0 and SGST > 0 | `9% State GST / SGST` (with 9% Central GST / CGST) |

---

## 2. Manual process (portal UI)

1. Open **https://service.ariba.com/Supplier.aw**. Enter the username (`#userid`) and click **Next**, then enter the password and click **Sign in**.
2. You land on `https://portal.us.bn.cloud.ariba.com/dashboard/home`. Click the **Orders to invoice** tile.
3. Type the PO into **Order numbers**, select **Exact match**, and click **Apply**. Click the PO number.
4. Click **Create Invoice ▾** and choose **Standard Invoice**.
5. Fill **Invoice #**, check **Invoice Date** (pre-filled with today), and fill **Service Description**. Under **Additional India Specific Information**, set **Tax Invoice Number** = invoice number.
6. Click **Add to Header ▾** → **Comment** and type the comment.
7. Click **Add to Header ▾** → **Attachment**, click **Choose File**, then click **Add Attachment**. Wait for the upload (a 4 MB PDF took about 2 minutes).
8. **Line Items:** tick the header box (select all), untick only the billing-month line, and click the line-item **Delete**
   (the one next to *Line Item Actions*, **not** the attachment Delete).
9. Tick the remaining line and **Tax Category**, choose the GST type, and click **Add to Included Lines**.
10. Set **HSN / SAC** = `998313`.
11. Click **Next**, then **Save** (draft), then **Exit**. **Never click Submit.**

---

## 3. Authentication and session mechanism (Confirmed)

The portal is two web apps with three kinds of credentials, all created by the normal browser login.

| Layer | Used by | Credentials sent |
|---|---|---|
| **Portal SPA** (`portal.us.bn.cloud.ariba.com`) | dashboard, workbench, token endpoint | session cookies + `x-ariba-session-id` header |
| **Network REST APIs** (`service.ariba.com/Network/...`) | PO / invoice list search | `Authorization: Bearer <accessToken>` + `x-ariba-session-id` |
| **Portal OData** (`/tpx/ingress/tps/...`) | messaging etc. (not needed) | cookies + `x-xsrf-token` |
| **AribaWeb** (`service.ariba.com/Supplier.aw`) | PO detail, Create Invoice form | cookies only + `awssk` key in URL/body. No bearer, no CSRF header |

### 3.1 Login (UI; SSO, no public API)
`Supplier.aw` → username page → `Authenticator.aw/ad/ssoIDP` (password) → SAP IAS
(`lwbnlive.accounts.ondemand.com`, OAuth2 + SAML) → `portal.us.bn.cloud.ariba.com/dashboard`.
This is an interactive IdP flow and is kept as a Playwright login. MFA was not requested for this account.

### 3.2 Bearer token (Confirmed working at runtime, in memory only)

```
GET https://portal.us.bn.cloud.ariba.com/tpx/ingress/auth/v1/token
Headers: x-ariba-session-id: <sessionStorage['sa.sessionId'] of the portal tab>
Cookies: portal session cookies (from login)
→ 200 application/json  { "accessToken": str(36), "refreshToken": str(36), "expiresIn": int }
```

- API calls send `Authorization: Bearer <accessToken>` (43 chars) and the same `x-ariba-session-id`.
- `x-ariba-session-id` is a GUID the SPA keeps in `sessionStorage['sa.sessionId']`.
- Tested: fetched with Playwright's `context.request` (it shares the browser cookies), then passed
  directly into the next request's headers. **Never printed, logged or written to disk.** The traffic
  recorder stores sensitive header values as lengths only and never records `/auth/*/token` bodies.
- Rule for the implementation: keep the token in a local variable, and re-fetch it on HTTP 401
  or when `expiresIn` elapses.

---

## 4. Confirmed API endpoints (JSON)

### 4.1 PO search: `po-list-search` (Confirmed, replayed successfully)

```
POST https://service.ariba.com/Network/txndataservicesupplier/data-service/v1/po-list-search
Content-Type: application/json
Authorization: Bearer <accessToken>
x-ariba-session-id: <sa.sessionId>
```
```json
{
  "category": "ORDERS_TO_INVOICE",
  "requestFrom": "ORDERS_TO_INVOICE_TRANSACTION",
  "created": {"dateRange": "LAST_365_DAYS",
              "from": "2025-09-24T00:00:00+05:30", "to": "2026-09-25T00:00:00+05:30"},
  "selectedColumns": [{"key":"payloadId"},{"key":"orderNumber"},{"key":"customer"},
                      {"key":"amount"},{"key":"date"},{"key":"orderStatus"},{"key":"amountInvoiced"}],
  "sortingColumns": [{"key":"date","isAscending":false}],
  "pageSize": 1000, "pageNumber": 0, "timezone": "Asia/Calcutta",
  "excludeOrderStatus": false, "subTypeOption": "no",
  "showOnlyInquiryDocuments": "no", "includeUomInfo": true
}
```
Response (200): `{"totalSize":116,"maxReturnSize":1000,"headers":[...],"data":[[...],...]}`.
Replayed result for the test PO:
```json
{"payloadId": "1790160224140.1854853512.000040501@HXFfu0ls0gGQi7PncH+9wxX5Y7A=",
 "orderNumber": "C9827-R89", "customer": "Cognizant",
 "amount": {"quantity": 2466220.04, "unit": "INR"},
 "date": "2026-09-23T16:13:47+05:30", "orderStatus": "New", "amountInvoiced": null}
```
**The returned ID is `payloadId`**, the key used to open the PO (section 5.1).

### 4.2 Other endpoints seen (same auth)

| Endpoint | Purpose | Status |
|---|---|---|
| `POST …/data-service/v1/po-list-count` | tile counts; `requestFrom` = `NEW_ORDERS_TRANSACTION`, `CHANGED_ORDERS_TRANSACTION`, `ORDERS_TO_INVOICE_TRANSACTION`, `ORDERS_TRANSACTION` | Confirmed (seen) |
| `POST …/data-service/v1/invoice-list-search` | invoice list (`requestFrom: INVOICES_TRANSACTION`, optional `invoiceStatus: ["INVOICE_SENT" \| "INVOICE_APPROVED" \| …]`). **Does not return drafts**: tried `INVOICE_DRAFT`, `INVOICE_SAVED`, `DRAFT`, etc. → 0 rows. Useful after submission, not for draft verification | Confirmed (replayed) |
| `POST …/data-service/v1/invoice-list-count` | invoice counts | Seen |
| `GET  …/data-service/v1/metadata/customer` | customer list | Seen |
| `GET  /tpx/ingress/auth/v1/token` | bearer token | Confirmed |

**There is no JSON API for PO line items, invoice creation, attachments, GST/SAC or saving a draft.**
Those all live in AribaWeb (section 5).

---

## 5. Confirmed form POST endpoints (AribaWeb)

### 5.1 Open the PO directly (Confirmed; pure HTTP, cookies only)

```
GET https://service.ariba.com/Supplier.aw/ad/documentDetail?pageToReturn=SellerAppWorkbench&docPayload=<url-encoded payloadId>
→ 200 HTML with an auto-submit form
POST https://service.ariba.com/Authenticator.aw/ad/login/SSOActions   (x-www-form-urlencoded: redirectSessionID, ssocc, …)
→ 302 → GET /scripts/WebObjects.dll/Supplier.woa/<appId>/ad/documentDetail?...
→ 302 → GET https://service.ariba.com/Supplier.aw/<appId>/aw?awh=r&awssk=<awssk>&dard=1   (PO detail page)
```
This replaces the dashboard tile, search, Exact match, Apply and PO-click UI steps. `appId` (e.g.
`109560069`) and `awssk` (e.g. `GMpttzWX`) are session values taken from the final URL / page.

### 5.2 AribaWeb request protocol (Confirmed)

Every button, menu item or link on the PO and invoice pages is the same request shape:

```
POST https://service.ariba.com/Supplier.aw/<appId>/aw?awr=<n>&awssk=<awssk>&
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Cookies: service.ariba.com session cookies        (no Authorization, no CSRF header)

<fieldId>=<value>&<fieldId>=<value>&...            ← every current form field (text, checked boxes, hidden)
&awsnf=<formId>&awfa=<formAction>&awfid=true&awcharset=UTF-8
&awsn=<senderId>[,<menuItemId>]                    ← WHICH control was activated
&awr=<n>&awssk=<awssk>&awst=0&awsl=0&awrv=AW6&awii=xmlhttp
```

| Parameter | Meaning |
|---|---|
| `awssk` | session key (acts as the anti-forgery token; same for the whole session) |
| `awr` | request counter, **must increase by one per request**. The next value is in each response: `ariba.Request.initParams('<next awr>','<awssk>',…)` |
| `awsn` | ID of the control that was pressed; menu picks send `<menuButtonId>,<menuItemId>` |
| `awsnf` / `awfa` | form ID and form action (hidden inputs in the form) |
| `<fieldId>` | generated element names such as `_iqejyb`. **They are regenerated on every render**, so they must be looked up in the latest HTML each time (by row label or `_a` attribute), never hard-coded |

Response: `200 text/html`, an **incremental HTML fragment** (`<div class=rr id=…>` blocks) holding
the re-rendered form with **new field IDs** and the next `awr`, or a
`ariba.Request.redirectRefresh()` script, after which `GET …/aw?awh=r&awssk=…` returns the full page.
Stable anchors in the HTML:
- field rows: the first `<td>` holds the label (`Invoice #:`, `Service Description:`, `Tax Invoice Number:`, `HSN / SAC:`)
- toolbar buttons carry a semantic `_a` attribute: `_a=update | save | exit | next`
- menu items are `<a class="w-pmi-item">Comment</a>` etc.

### 5.3 Step-by-step capture

| # | Step | Request | Status |
|---|---|---|---|
| 1 | Find PO | `po-list-search` (JSON) | **Confirmed**, replayed |
| 2 | Open PO | `GET documentDetail?docPayload=…` + SSO hand-off | **Confirmed** |
| 3 | Create Invoice → Standard Invoice | AribaWeb POST, `awsn=<Create Invoice menu id>,<"Standard Invoice" item id>`; response = redirect refresh → full form | **Confirmed** |
| 4 | Header fields | No request of their own. Values ride along with the next action POST, e.g. `_iqejyb=DI-27-2718a` (Invoice #), `_hvmgod=24 Sep 2026` (Invoice Date), `_5abepd=Man Power Industry` (Service Description), `_bzm9d=DI-27-2718a` (Tax Invoice Number), `_24jbqb`=Supplier Tax ID, `_mztkhb`=Supplier GSTIN | **Confirmed** |
| 5 | Comment | Add to Header → Comment = AribaWeb POST `awsn=<Add to Header id>,<Comment item id>`. Adds a `Comments:` textarea; its text is sent with the next POST (e.g. `_pkdz_b=<comment>`) | **Confirmed** |
| 6a | Attachment section | Add to Header → Attachment = AribaWeb POST `awsn=<Add to Header id>,<Attachment item id>`. Adds a file input (e.g. `_lqy5hc`) and an **Add Attachment** button | **Confirmed** |
| 6b | Upload PDF | **`POST https://service.ariba.com/Supplier.aw/<appId>/aw`, `multipart/form-data`**, sent by Add Attachment. Same endpoint as every other action; there is **no separate upload service**. Parts (captured, in order): every form field as a text part, then the file part `name="_lqy5hc"; filename="DI-27-2718a.pdf"; Content-Type: application/pdf`, then `awsnf, awfa, awfid, awcharset, PageErrorPanelIsMinimized`, **`awsn=_ucj$3`** (Add Attachment), `awr, awssk, awst, awsl, awrv=AW6, awii=AWRefreshFrame`. Response = re-rendered form listing `DI-27-2718a.pdf  4074070  application/pdf` | **Confirmed** |
| 7a | Line items: select all | POST `awsn=_ctvrpd` (the header checkbox itself is the action) | **Confirmed** (not needed for HTTP) |
| 7b | Untick billing-month line | POST `awsn=<that line's checkbox id>` | **Confirmed** (not needed for HTTP) |
| 7c | **Delete other lines** | POST `awsn=_unxwzc` (line-item Delete) with the lines to delete sent as `<line checkbox name>=1`, e.g. `_dq8ly=1&_lmytk=1&_zcybrc=1&_7owdtc=1&_jaerpd=1` (lines 2–6). Result: only `02-Sep-2026 to 30-Sep-2026` remains | **Confirmed** |
| 8a | Tick remaining line + Tax Category | **No request**: client-side; sent with the next POST as `_b_ujcb=1` (line) and `_$phx9c=1` (Tax Category box) | **Confirmed** |
| 8b | Open GST chooser | **No request** (client-side popup; options are `<a>` elements with their own IDs) | **Confirmed** |
| 8c | Pick GST | POST `awsn=_ypiwq,_zbtjv` (chooser ID, option ID of `0% Integrated GST / IGST`) | **Confirmed** |
| 8d | Add to Included Lines | POST `awsn=_cysvxb` + `_$phx9c=1&_jvyz$b=0% Integrated GST / IGST&_b_ujcb=1`. Result: line Tax block Category 0% IGST, Taxable 399,555.04 INR, Rate 0, Tax 0.00 | **Confirmed** |
| 9 | HSN / SAC | Line field `_1fxis=998313`, sent with the next POST | **Confirmed** |
| 10a | Next | POST `awsn=_t1zf` (`_a=next`) + `_1fxis=998313`, `_s3$rbd=0% Integrated GST / IGST` (line tax category). Response = review page (*Previous / Save / **Submit** / Exit*) | **Confirmed** |
| 10b | **Save (draft)** | POST `awsn=_yush9d` (review-page button `_a="save"`), url-encoded, no fields. Response contains **`Invoice "DI-27-2718a" is saved. The saved invoice will be kept until 13 Nov 2026.`** (drafts expire after ~50 days) | **Confirmed** (clicked by the user; recorded) |
| 10c | Exit | POST `awsn=_8z6fuc` (`_a="exit"`). Response = dialog: *Save the invoice / Delete the invoice / Continue to work on the invoice* | **Confirmed** |
| 10d | Exit dialog → Save the invoice | POST `awsn=_zcqbp`. Response = redirect refresh → back to the PO page | **Confirmed** |
| 11 | Verify | Re-open the PO (`documentDetail`, 5.1). The PO page header shows **`Draft Invoices: Invoice: DI-27-2718a`** | **Confirmed** (pure HTTP GET + HTML parse) |

On the review page the buttons carry `_a="prev" | "save" | "submit" | "exit"`. **The automation must pick
`_a="save"` and hard-refuse `_a="submit"` and any control whose text contains "Submit".**

> ⚠️ **Button IDs change meaning between pages.** On the edit page `_yush9d`=Update and `_jsl7tb`=Save;
> on the **review page `_yush9d`=Save and `_jsl7tb`=SUBMIT**. The client must resolve every control
> from the *current* response by its label / `_a` attribute and refuse anything labelled Submit.
> It must never reuse a remembered ID across pages.

**Field IDs are stable across sessions.** Two separate logins produced the same IDs (`_iqejyb`, `_bzm9d`,
`_lqy5hc`, `_ucj$3`, line names `_b_ujcb`…`_jaerpd`), so they come from the page structure. They still depend
on the page layout (number of lines, sections added), so resolve them by label from each response.
`awr` counts in **base 36** (`8, 9, a, b, c, d …`).

**PO line data:** the six PO lines appear in the invoice form HTML (for example
`932553-Venkata Vijay Miriyala-02-Sep-2026 to 30-Sep-2026`, subtotal `399,555.04 INR`), each with its
checkbox name, subtotal field and HSN/SAC field. So line data can be read from the HTML.
No JSON source was found.

---

## 6. Attachment upload mechanism

- **Endpoint:** the same AribaWeb action URL, `POST /Supplier.aw/<appId>/aw`, as `multipart/form-data`,
  triggered by the *Add Attachment* button. There is no dedicated upload endpoint.
- **ID:** **none is returned to the client.** The file is attached to the **server-side invoice
  object held in the AribaWeb session**. The page only shows a session-bound link
  (`<a _t="invoice_attachment">DI-27-2718a.pdf</a>`), its size and type, and a checkbox
  for the attachment Delete.
- **Association:** implicit. The attachment belongs to the invoice being edited in that session and is
  stored with it when the invoice is saved. If the session ends without Save, it is discarded.
- **Programmatic upload is feasible** with the same session: POST the multipart form
  (all current fields, `awsn=<Add Attachment id>`, file part under the current file-input name).
  Choosing the file is straightforward: look for `<INVOICE_PDF_DIR>/<invoice number>.pdf` (e.g.
  `D:\cognizant\DI-27-2718a.pdf`) and fail if it is missing or duplicated.
- The upload is slow (~2 min for 4 MB), so it needs a long timeout.

---

## 7. What can run without UI, and what genuinely needs a browser

| Step | Without UI? | Notes |
|---|---|---|
| Login | **No (browser once)** | IAS OAuth2/SAML IdP flow. After it, everything runs on the session cookies |
| Token for REST | **Yes** | `GET /auth/v1/token` with cookies, held in memory |
| Find PO → `payloadId` | **Yes** | `po-list-search` |
| Open PO | **Yes** | `documentDetail` GET + auto-posted SSO form |
| Create Invoice form | **Yes** | AribaWeb POST (confirmed). Needs HTML parsing to find control IDs |
| Header, comment | **Yes** | field values in the same POSTs (confirmed) |
| Attachment upload | **Yes** | multipart POST to the same endpoint (confirmed part list) |
| Line delete, GST, Add to Included Lines, SAC, Next | **Yes** | all confirmed as AribaWeb POSTs; select-all/untick not needed over HTTP |
| Next, Save as draft, Exit → Save | **Yes** | confirmed AribaWeb POSTs (`_a=next`, `_a=save`, `_a=exit`, dialog "Save the invoice") |
| Verify draft | **Yes** | re-open the PO over HTTP and read "Draft Invoices: Invoice: <no>"; REST list does not show drafts |

**Result: every step after the login can run over HTTP.** Only the IdP login needs a browser.

**Reliability assessment for AribaWeb replay.** It is reproducible over plain HTTP (cookies,
`awssk`, `awr`, form fields; no signed payloads, no bearer, no CSRF header). The trade-offs are:
- **Everything needs an HTML parser.** Field and control IDs change on every render, so each
  request depends on parsing the previous response by label or `_a` attribute.
- **`awr` must be tracked exactly.** A wrong counter gets a "page expired / out of sync" response.
- **Some behaviour is client-side JavaScript** (select-all, chooser popups, the multipart switch)
  and has to be re-implemented.
- **The protocol is undocumented** and can change with Ariba releases.

---

## 8. Proposed final architecture

```
┌──────────────────────── one browser login (Playwright, visible) ─────────────────────────┐
│ Supplier.aw → IAS → dashboard.  Result: cookie jar + sa.sessionId                          │
└────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                             │  cookies handed to an HTTP client (same session)
┌────────────────────────────────────────────▼────────────────────────────────────────────────┐
│ AribaApi (REST, JSON)                                                                       │
│  • get_token()           GET /tpx/ingress/auth/v1/token   (memory only, refresh on 401)      │
│  • find_po(po)           POST po-list-search → payloadId                                    │
│  • find_invoice(inv)     POST invoice-list-search          → status after submission       │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│ AribaWebSession (form-POST client)                                                           │
│  • open_po(payloadId)    GET documentDetail + SSO hand-off → appId, awssk, awr, form HTML    │
│  • act(control, fields)  POST /Supplier.aw/<appId>/aw  (awsn, awr++, all fields)            │
│  • upload(control, pdf)  multipart POST, same endpoint                                       │
│  • parser                label→field-id, _a→button-id, menu item text→id, next awr         │
│  • guard                 refuses any control whose text/_a is submit/send                    │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│ InvoiceDraftFlow                                                                             │
│  find_po → open_po → Create Invoice(Standard) → header → comment → attachment →              │
│  delete non-month lines → GST + Add to Included Lines → SAC → Next → Save → Exit →          │
│  verify: re-open PO (documentDetail) → "Draft Invoices: Invoice: <no>"                        │
│  Fallback: any AribaWeb step that fails verification is re-run with Playwright on the SAME   │
│  session (visible browser), then control returns to HTTP.                                    │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
Config: INVOICE_PDF_DIR, SAC, service description, GST rule; Excel tracker supplies PO / invoice / GST.
```

Recommendation: implement the REST parts (token, `find_po`, `open_po`, verification) first. They
are confirmed and remove the whole workbench UI. Then build `AribaWebSession` against the invoice form,
switching each step from Playwright to HTTP only once its request has been captured and replayed.

---

## 9. Open items (need your go-ahead / permission)

1. **A test draft now exists on the live portal:** PO C9827-R89, dummy invoice **DI-27-2718a**, Sep-2026 line
   (399,555.04 INR), 0% IGST, SAC 998313, demo comment, `DI-27-2718a.pdf`. Saved 2026-09-24 and kept by Ariba
   until 13 Nov 2026. It is test data and should be **deleted** (open it from the PO's "Draft Invoices" link →
   Exit → "Delete the invoice"), or reused if DI-27-2718a becomes the real invoice.
2. The whole flow has been proven in the browser. **The pure-HTTP replay of the AribaWeb steps is not built yet.**
   It is the next implementation step and should first be tested on a PO/invoice that is safe to save.
3. The Save click in this run was done by the user: the Claude Code permission check blocks the agent from
   clicking Save on the live portal. The production automation, run by the user, is not affected by that.

---

## 10. Status of the earlier Antigravity attempt

- It never reached the workbench: it treated the SSO redirect page as "landed" and ran every step against
  an empty page, then logged success. No clicks reached the real portal.
- Its `AribaApiClient` endpoints (`/api/po/search`, `/api/invoice/draft`) do not exist.
- `automation/cognizant_ariba.py` will be rewritten to the architecture in section 8 once the open
  items are resolved.
