"""HTTP client for SAP Ariba's AribaWeb pages (Supplier.aw): PO detail, Create Invoice wizard.

Every button / menu item / link on these pages is the same request:

    POST /Supplier.aw/<appId>/aw?awr=<n>&awssk=<key>&
    <all current form fields> + awsn=<activated control id>[,<menu item id>] + awr + awssk + ...

Control and field ids are generated from the page structure, so they are ALWAYS resolved from the
latest page by label / text / `_a` attribute, never remembered across pages (on the review page the
id that used to be "Save" becomes "Submit").
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from loguru import logger
from playwright.async_api import APIRequestContext

from .errors import SessionExpired, TransientError, clean_error, with_retries
from .html_tree import Node, parse

BASE = "https://service.ariba.com"
SESSION_GONE = re.compile(r"session (has )?(expired|timed out)|you have been logged out|please (log|sign) in again", re.I)
LOGIN_PAGE = re.compile(r'id=["\']?userid|name=["\']?Password|Authenticator\.aw/ad/(loginPage|ssoIDP)', re.I)
ERROR_PAGE = re.compile(r"an (unexpected )?error has occurred|your request could not be processed|page (has )?expired|"
                        r"this page is no longer valid|service (is )?(temporarily )?unavailable", re.I)
FORBIDDEN = re.compile(r"\b(submit|send)\b", re.I)
INIT_PARAMS = re.compile(r"ariba\.Request\.initParams\('([^']*)','([^']*)','([^']*)','[^']*','[^']*','([^']*)'")


UPLOAD_JS = """async ([fields, fileField, fileName, b64, tail, url, referrer, timeoutMs]) => {
  const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const fd = new FormData();
  for (const [k, v] of fields) fd.append(k, v);
  fd.append(fileField, new Blob([bin], {type: 'application/pdf'}), fileName);
  for (const [k, v] of tail) fd.append(k, v);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method: 'POST', body: fd, credentials: 'include', referrer, signal: ctl.signal});
    return [r.status, (await r.text()).length];
  } finally { clearTimeout(timer); }
}"""
BLANK_ON_ORIGIN = BASE + "/__automation_blank__"


async def browser_origin_tab(context):
    """A tab on the service.ariba.com origin WITHOUT loading anything from Ariba (stub served locally)."""
    tab = await context.new_page()
    await tab.route(BLANK_ON_ORIGIN, lambda route: route.fulfill(status=200, content_type="text/html",
                                                                   body="<html><body>upload helper</body></html>"))
    await tab.goto(BLANK_ON_ORIGIN)
    return tab


REQUEST_ERROR = re.compile(r"Error: Your request cannot be processed[^.]*\.(?: [^.]*\.)?")


class AribaWebError(RuntimeError):
    pass


class SubmitGuardError(AribaWebError):
    """Raised when a control that could submit the invoice would be activated."""


@dataclass
class PageState:
    html: str
    root: Node
    awr: str
    awssk: str
    refresh_url: str
    post_url: str


class AribaWebSession:
    def __init__(self, request: APIRequestContext, timeout_ms: float = 180_000, client_timezone: str = "Asia/Calcutta") -> None:
        self.req = request
        self.timeout = timeout_ms
        self.client_timezone = client_timezone
        self.upload_timeout = 300_000  # hard cap per upload attempt (a 4 MB PDF took ~2 min in the browser)
        self.page: PageState | None = None
        self.last_response = ""
        self.multipart_when_file_input = False  # browser behaviour: whole form as multipart once a file box exists
        self.requests = 0

    # ------------------------------------------------------------------ page loading
    def _load(self, html: str) -> PageState:
        m = INIT_PARAMS.search(html)
        if not m:
            self._raise_for_page(html, "page without AribaWeb session parameters")
        awr, awssk, refresh, post = m.groups()
        root = parse(html)
        text = root.text()
        if ERROR_PAGE.search(text):
            raise TransientError(f"AribaWeb error page: {ERROR_PAGE.search(text).group(0)}")
        if SESSION_GONE.search(text):
            raise SessionExpired("Ariba session expired")
        self.page = PageState(html, root, awr, awssk, urljoin(BASE, refresh), urljoin(BASE, post))
        return self.page

    @staticmethod
    def _raise_for_page(html: str, what: str) -> None:
        text = parse(html).text()
        if SESSION_GONE.search(text) or LOGIN_PAGE.search(html):
            raise SessionExpired(f"Ariba session expired ({what})")
        raise TransientError(f"{what}: {text[:160]!r}")

    async def _get(self, url: str) -> tuple[str, str]:
        async def once():
            r = await self.req.get(url, timeout=self.timeout, headers={"Referer": BASE + "/Supplier.aw"})
            self.requests += 1
            if r.status >= 500:
                raise TransientError(f"GET -> HTTP {r.status}")
            return r.url, await r.text()
        return await with_retries(once, "AribaWeb GET")

    async def refresh(self) -> PageState:
        """Re-render the current page in full (what the browser does after ariba.Request.redirectRefresh)."""
        assert self.page
        _, html = await self._get(self.page.refresh_url)
        return self._load(html)

    async def open_document(self, url: str) -> PageState:
        """GET a Supplier.aw deep link, completing the SSO hand-off form it returns."""
        async def once():
            final_url, html = await self._get(url)
            for _ in range(3):
                if INIT_PARAMS.search(html):
                    return self._load(html)
                form = parse(html).find(lambda n: n.tag == "form" and n.get("action"))
                if form is None:
                    break
                data = {i.get("name"): i.get("value", "") for i in form.find_all(lambda n: n.tag == "input" and n.get("name"))}
                if "SSOActions" in (form.get("action") or ""):
                    data.update(self._client_clock_fields(data))
                r = await self.req.post(urljoin(final_url, form.get("action")), form=data, timeout=self.timeout,
                                        headers={"Origin": BASE, "Referer": final_url})
                self.requests += 1
                final_url, html = r.url, await r.text()
            self._raise_for_page(html, f"could not open document (landed on {final_url[:70]})")
        return await with_retries(once, "open document")

    def _client_clock_fields(self, data: dict[str, str]) -> dict[str, str]:
        """Values the login page's JavaScript fills in before auto-posting the SSO hand-off form."""
        offset = -int(datetime.now().astimezone().utcoffset().total_seconds() // 60)  # JS getTimezoneOffset()
        out = {k: str(offset) for k in data if k.startswith("timezone")}
        out |= {"clientTime": str(int(time.time() * 1000)), "clientTimezone": self.client_timezone, "useGetProfile": "1"}
        return out

    # ------------------------------------------------------------------ queries on the current page
    @property
    def root(self) -> Node:
        assert self.page
        return self.page.root

    def text(self) -> str:
        return self.root.text()

    def main_form(self) -> Node:
        marker = self.root.find(lambda n: n.tag == "input" and n.get("name") == "awsnf" and n.form is not None)
        if marker is None:
            raise AribaWebError("main AribaWeb form not found")
        return marker.form

    def form_controls(self) -> list[Node]:
        form = self.main_form()
        return [n for n in self.root.iter() if n.form is form]

    @staticmethod
    def _labelled(n: Node, label: str) -> bool:
        """Row label starts with `label`: first cell of the row, or the cell right before the field's cell."""
        tr = n.closest("tr")
        first_td = tr.find(lambda c: c.tag in ("td", "th")) if tr else None
        if first_td is not None and first_td.text().startswith(label):
            return True
        td = n.closest("td")
        if td is not None and td.parent is not None:
            cells = [c for c in td.parent.children if isinstance(c, Node) and c.tag in ("td", "th")]
            i = cells.index(td) if td in cells else -1
            if i > 0 and cells[i - 1].text().startswith(label):
                return True
        return False

    def field_by_label(self, label: str, tag: str = "input") -> Node:
        """First text/textarea field whose row label starts with `label` (e.g. 'Invoice #:')."""
        found = self.fields_by_label(label, tag)
        if not found:
            raise AribaWebError(f"field {label!r} not found")
        return found[0]

    def fields_by_label(self, label: str, tag: str = "input") -> list[Node]:
        """Every text field whose row label starts with `label` (e.g. one HSN / SAC field per line)."""
        return [n for n in self.root.iter()
                if n.tag == tag and n.get("name") and (tag != "input" or n.get("type", "text") == "text")
                and self._labelled(n, label)]

    def button(self, text: str | None = None, action: str | None = None, after: str | None = None) -> Node:
        """A <button> by exact visible text and/or `_a` attribute; optionally the first one after button `after`."""
        buttons = [n for n in self.root.iter() if n.tag == "button" and n.id]
        if after:
            idx = next((i for i, b in enumerate(buttons) if b.text() == after), None)
            if idx is None:
                raise AribaWebError(f"anchor button {after!r} not found")
            buttons = buttons[idx + 1:]
        for b in buttons:
            if (text is None or b.text() == text) and (action is None or b.get("_a") == action):
                return b
        raise AribaWebError(f"button text={text!r} _a={action!r} not found")

    def link(self, text: str) -> Node:
        n = self.root.find(lambda n: n.tag == "a" and n.id and n.text() == text)
        if n is None:
            raise AribaWebError(f"link {text!r} not found")
        return n

    def menu_pick(self, trigger_text: str | None, item_text: str, menu_id: str | None = None) -> str:
        """awsn for a pulldown/chooser pick: '<trigger id>,<item id>'."""
        triggers = [n for n in self.root.iter() if n.get("bh") == "PML" and n.get("_mid") and n.id]
        if menu_id:
            triggers = [t for t in triggers if t.get("_mid") == menu_id]
        if trigger_text:
            triggers = [t for t in triggers if trigger_text in t.text() or trigger_text in (t.find(lambda c: c.tag == "input") or t).get("value", "")]
        if not triggers:
            raise AribaWebError(f"menu trigger {trigger_text or menu_id!r} not found")
        trig = triggers[0]
        menu = self.root.find(lambda n: n.id == trig.get("_mid"))
        if menu is None:
            raise AribaWebError(f"menu {trig.get('_mid')!r} not found")
        item = menu.find(lambda n: n.tag == "a" and n.id and n.text() == item_text)
        if item is None:
            raise AribaWebError(f"menu item {item_text!r} not in menu {trig.get('_mid')!r}")
        return f"{trig.id},{item.id}"

    def menu_pick_on(self, trigger: Node, item_text: str) -> str:
        """awsn for picking `item_text` in the menu of one specific trigger (e.g. the 2nd tax row's chooser)."""
        menu = self.root.find(lambda n: n.id == trigger.get("_mid"))
        item = menu.find(lambda n: n.tag == "a" and n.id and n.text() == item_text) if menu else None
        if item is None:
            raise AribaWebError(f"menu item {item_text!r} not in menu {trigger.get('_mid')!r}")
        return f"{trigger.id},{item.id}"

    # ------------------------------------------------------------------ form serialisation + actions
    def form_fields(self) -> dict[str, str]:
        """Serialise the main form like the browser: text/hidden/textarea/checked boxes/selected options."""
        out: dict[str, str] = {}
        for n in self.form_controls():
            name = n.get("name")
            if not name or n.get("disabled") is not None:
                continue
            if n.tag == "input":
                t = (n.get("type") or "text").lower()
                if t in ("checkbox", "radio"):
                    if n.get("checked") is not None:
                        out[name] = n.get("value", "on")
                elif t not in ("file", "submit", "button", "image", "reset"):
                    out[name] = n.get("value", "")
            elif n.tag == "textarea":
                out[name] = n.text()
            elif n.tag == "select":
                opt = n.find(lambda o: o.tag == "option" and o.get("selected") is not None) or n.find(lambda o: o.tag == "option")
                if opt is not None:
                    out[name] = opt.get("value", opt.text())
        return out

    def _guard(self, awsn: str) -> None:
        for cid in awsn.split(","):
            node = self.root.find(lambda n, cid=cid: n.id == cid)
            label = (node.text() if node else "") + " " + ((node.get("_a") or "") if node else "")
            if FORBIDDEN.search(label):
                raise SubmitGuardError(f"refusing to activate control {cid} ({label.strip()!r})")

    async def act(self, awsn: str, set_fields: dict[str, str] | None = None,
                  unset_fields: list[str] | None = None, file: tuple[str, Path] | None = None) -> PageState:
        """Activate control(s) `awsn` with the current form values (+ overrides), then reload the page."""
        assert self.page
        self._guard(awsn)
        fields = self.form_fields()
        for k in unset_fields or []:
            fields.pop(k, None)
        fields.update(set_fields or {})
        file_input = next((n for n in self.form_controls() if n.tag == "input" and (n.get("type") or "").lower() == "file"), None)
        has_file_input = file_input is not None
        tail = {"awsn": awsn, "awr": self.page.awr, "awssk": self.page.awssk, "awst": "0", "awsl": "0", "awrv": "AW6"}
        url = f"{self.page.post_url}?awr={self.page.awr}&awssk={self.page.awssk}&"
        # same headers the browser sends; the edge firewall resets bare uploads without them
        headers = {"Origin": BASE, "Referer": self.page.refresh_url,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        try:
            if file or (self.multipart_when_file_input and has_file_input):
                mp: dict = dict(fields)
                if file and file_input is not None:
                    name, path = file[0] or file_input.get("name"), file[1]
                    mp[name] = {"name": path.name, "mimeType": "application/pdf", "buffer": path.read_bytes()}
                elif file_input is not None:
                    mp[file_input.get("name")] = {"name": "", "mimeType": "application/octet-stream", "buffer": b""}
                mp.update(tail | {"awii": "AWRefreshFrame"})
                r = await self.req.post(self.page.post_url, multipart=mp, headers=headers,
                                        timeout=self.upload_timeout if file else self.timeout)
            else:
                r = await self.req.post(url, form=fields | tail | {"awii": "xmlhttp"}, timeout=self.timeout, headers=headers)
        except Exception as e:  # network error / timeout: the action may or may not have been applied
            raise TransientError(f"POST awsn={awsn} failed: {clean_error(e)}") from None
        finally:
            self.requests += 1
        if r.status >= 500:
            raise TransientError(f"POST awsn={awsn} -> HTTP {r.status}")
        if r.status != 200:
            raise AribaWebError(f"AribaWeb POST awsn={awsn} -> HTTP {r.status}")
        body = await r.text()
        self.last_response = body
        logger.debug(f"POST awsn={awsn} awr={self.page.awr} -> {len(body)} bytes")
        return await self.refresh()

    async def upload_via_browser(self, awsn: str, pdf: Path, browser_page, set_fields: dict[str, str] | None = None) -> PageState:
        """Same multipart action POST as the browser's upload, sent with the browser's own network stack.

        Ariba's edge drops multi-MB uploads coming from Playwright's HTTP client ("socket hang up") but accepts
        them from Chrome. `browser_page` must be on the https://service.ariba.com origin (see `browser_origin_tab`).
        """
        assert self.page
        self._guard(awsn)
        fields = self.form_fields() | (set_fields or {})
        file_input = next((n for n in self.form_controls() if n.tag == "input" and (n.get("type") or "").lower() == "file"), None)
        if file_input is None:
            raise AribaWebError("no file input on the page (attachment section not open)")
        tail = {"awsn": awsn, "awr": self.page.awr, "awssk": self.page.awssk, "awst": "0", "awsl": "0",
                "awrv": "AW6", "awii": "AWRefreshFrame"}
        import base64
        payload = [list(fields.items()), file_input.get("name"), pdf.name,
                   base64.b64encode(pdf.read_bytes()).decode(), list(tail.items()),
                   self.page.post_url, self.page.refresh_url, int(self.upload_timeout)]
        try:
            status, length = await browser_page.evaluate(UPLOAD_JS, payload)
        except Exception as e:
            raise TransientError(f"browser upload failed: {clean_error(e)}") from None
        finally:
            self.requests += 1
        if status >= 500 or status == 0:
            raise TransientError(f"browser upload -> HTTP {status}")
        if status != 200:
            raise AribaWebError(f"browser upload -> HTTP {status}")
        logger.debug(f"browser upload awsn={awsn} -> {length} bytes")
        return await self.refresh()

    def page_errors(self) -> list[str]:
        """Visible validation messages on the page (red error texts)."""
        msgs = []
        for n in self.root.iter():
            cls = n.get("class") or ""
            if any(c in cls for c in ("errorText", "w-error", "pageError", "errorMsg", "error-message")):
                t = n.text()
                if t and t not in msgs:
                    msgs.append(t)
        return msgs
