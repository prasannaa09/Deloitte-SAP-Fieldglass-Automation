"""Login (browser, once) and the SAP Business Network REST APIs (bearer token held in memory only)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from loguru import logger
from playwright.async_api import BrowserContext, Page

from .config import Settings
from .errors import SessionExpired, TransientError, clean_error, with_retries

PORTAL = "https://portal.us.bn.cloud.ariba.com"
TOKEN_URL = f"{PORTAL}/tpx/ingress/auth/v1/token"
DATA_SERVICE = "https://service.ariba.com/Network/txndataservicesupplier/data-service/v1"
DOC_DETAIL = "https://service.ariba.com/Supplier.aw/ad/documentDetail?pageToReturn=SellerAppWorkbench&docPayload="
CONSENT_BUTTONS = ("button:has-text('Accept All'):visible, button:has-text('Accept all'):visible, "
                   "a:has-text('Accept All'):visible")


async def dismiss_popups(page: Page, watch_seconds: float = 0) -> bool:
    """Click 'Accept All' on the cookie-consent dialog if it is (or, within watch_seconds, becomes) visible."""
    deadline = asyncio.get_event_loop().time() + watch_seconds
    while True:
        for frame in page.frames:
            try:
                btn = frame.locator(CONSENT_BUTTONS)
                if await btn.count():
                    await btn.first.click(timeout=5_000)
                    logger.info("closed cookie-consent popup (Accept All)")
                    return True
            except Exception:  # frame navigated away / detached: ignore
                pass
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(1)


async def install_popup_handlers(context: BrowserContext) -> None:
    """Close the consent popup on every tab: when it blocks a click, and proactively when a page loads."""
    async def accept(loc):
        logger.info("closed cookie-consent popup (Accept All)")
        await loc.first.click(timeout=10_000)

    async def on_page(page: Page) -> None:
        await page.add_locator_handler(page.locator(CONSENT_BUTTONS), accept, no_wait_after=True)
        page.on("load", lambda p: asyncio.ensure_future(dismiss_popups(p, watch_seconds=15)))

    for pg in context.pages:
        await on_page(pg)
    context.on("page", lambda pg: asyncio.ensure_future(on_page(pg)))


async def _press_link(page: Page, link, what: str, tries: int = 4) -> None:
    """Activate an AribaWeb link and CONFIRM it was sent: the browser must POST `awsn=<link id>`.

    Ariba drops clicks that arrive while its own background request (e.g. the field's blur refresh) is
    running, so the click is repeated — alternating a normal and a JavaScript click — until the POST is seen.
    """
    await link.first.wait_for(timeout=60_000)
    link_id = await link.first.get_attribute("id")
    marker = f"awsn={quote(link_id or '', safe='')}"

    def is_press(req) -> bool:
        if req.method != "POST" or "/aw" not in req.url:
            return False
        body = req.post_data or ""
        return marker in body or (link_id and f"awsn={link_id}" in body)

    for i in range(1, tries + 1):
        await dismiss_popups(page)
        await page.wait_for_timeout(1200)  # let Ariba finish the field's own blur request
        try:
            async with page.expect_request(is_press, timeout=8_000):
                if i % 2:
                    await link.first.click(timeout=10_000)
                else:
                    await link.first.evaluate("e => e.click()")
            logger.debug(f"{what}: sent (try {i})")
            return
        except Exception as e:  # noqa: BLE001
            if i == tries:
                raise TransientError(f"{what}: Ariba never received the click ({clean_error(e)})") from None
            logger.warning(f"{what}: click was not sent, clicking again ({i}/{tries - 1})")


async def login(page: Page, s: Settings) -> None:
    """Username -> Next -> password -> Sign in, until the portal dashboard has a session id."""
    s.require_credentials()
    await page.goto(s.login_url, wait_until="load", timeout=s.timeout_ms)
    user = page.locator("#userid")
    await user.wait_for(timeout=s.timeout_ms)
    await user.fill(s.username)
    await user.press("Tab")  # blur: Ariba registers the value before the Next click
    await _press_link(page, page.locator("a:visible, button:visible").filter(has_text=re.compile(r"^\s*Next\s*$", re.I)), "Next")
    pw = page.locator("input[type=password]:visible")
    await pw.first.wait_for(timeout=s.timeout_ms)

    await pw.first.fill(s.password)
    await pw.first.press("Tab")
    await _press_link(page, page.locator("a:visible, button:visible").filter(has_text=re.compile(r"^\s*Sign in\s*$", re.I)), "Sign in")
    try:
        await page.wait_for_url(lambda u: u.startswith(PORTAL + "/dashboard"), timeout=s.timeout_ms, wait_until="commit")
    except Exception:
        text = (await page.evaluate("() => document.body ? document.body.innerText : ''"))[:3000].lower()
        if any(k in text for k in ("incorrect", "invalid username", "invalid password", "locked", "not valid")):
            raise RuntimeError("Ariba rejected the login (wrong password or locked account) — not retrying") from None
        raise TransientError(f"Sign in was sent but the dashboard never loaded (at {page.url[:70]})") from None
    if "/dashboard/error" in page.url:
        logger.warning("portal showed its error page after login; reloading the dashboard")
        await page.goto(PORTAL + "/dashboard/home", wait_until="domcontentloaded", timeout=s.timeout_ms)
        await page.wait_for_timeout(5000)
        if "/dashboard/error" in page.url:
            raise TransientError("portal dashboard keeps showing its error page")
    for _ in range(90):
        try:
            if await page.evaluate("() => sessionStorage.getItem('sa.sessionId')"):
                await dismiss_popups(page, watch_seconds=8)  # homepage consent popup
                return
        except Exception:  # dashboard still redirecting
            pass
        await asyncio.sleep(1)
    raise TransientError("logged in, but the portal session id never appeared")


async def login_with_retries(context: BrowserContext, s: Settings, attempts: int = 3) -> Page:
    """Fresh tab per attempt; slow or frozen login pages are retried, bad credentials are not."""
    await install_popup_handlers(context)
    for i in range(1, attempts + 1):
        page = await context.new_page()
        try:
            await asyncio.wait_for(login(page, s), s.timeout_ms / 1000 * 2)
            return page
        except Exception as e:  # noqa: BLE001
            if "rejected the login" in str(e) or i == attempts:
                raise
            logger.warning(f"login attempt {i} failed ({clean_error(e)}); retrying with a fresh page")
            await page.close()
    raise AssertionError("unreachable")


class PortalApi:
    """REST calls authenticated with the portal's own bearer token (never logged or persisted)."""

    def __init__(self, context: BrowserContext, portal_page: Page, s: Settings) -> None:
        self.ctx = context
        self.page = portal_page
        self.s = s
        self._token: str | None = None
        self._expires: datetime = datetime.min

    async def _headers(self) -> dict[str, str]:
        sid = await self.page.evaluate("() => sessionStorage.getItem('sa.sessionId')")
        if not sid:
            raise SessionExpired("portal session id is gone")
        if not self._token or datetime.now() >= self._expires:
            async def fetch():
                r = await self.ctx.request.get(TOKEN_URL, headers={"x-ariba-session-id": sid, "Accept": "application/json"},
                                               timeout=self.s.timeout_ms)
                if r.status in (401, 403):
                    raise SessionExpired(f"token endpoint returned HTTP {r.status}")
                if r.status != 200:
                    raise TransientError(f"token endpoint returned HTTP {r.status}")
                return await r.json()
            data = await with_retries(fetch, "token")
            self._token = data["accessToken"]
            self._expires = datetime.now() + timedelta(seconds=max(int(data.get("expiresIn", 300)) - 60, 30))
        return {"Authorization": f"Bearer {self._token}", "x-ariba-session-id": sid,
                "Content-Type": "application/json", "Accept": "application/json"}

    async def _post(self, path: str, body: dict) -> dict:
        async def once():
            for attempt in (1, 2):
                r = await self.ctx.request.post(f"{DATA_SERVICE}/{path}", data=json.dumps(body),
                                                headers=await self._headers(), timeout=self.s.timeout_ms)
                if r.status == 401 and attempt == 1:
                    self._token = None  # token expired: fetch a new one once
                    continue
                if r.status in (401, 403):
                    raise SessionExpired(f"{path} returned HTTP {r.status}")
                if r.status >= 500:
                    raise TransientError(f"{path} returned HTTP {r.status}")
                if r.status != 200:
                    raise RuntimeError(f"{path} returned HTTP {r.status}")
                return await r.json()
        return await with_retries(once, path)

    async def find_po(self, po_number: str) -> dict:
        """Look the PO up among 'orders to invoice' of the last 365 days; returns the row incl. payloadId."""
        now = datetime.now().astimezone()
        tz = now.strftime("%z"); tz = f"{tz[:3]}:{tz[3:]}"
        body = {
            "category": "ORDERS_TO_INVOICE", "requestFrom": "ORDERS_TO_INVOICE_TRANSACTION",
            "created": {"dateRange": "LAST_365_DAYS",
                        "from": (now - timedelta(days=365)).strftime("%Y-%m-%dT00:00:00") + tz,
                        "to": (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00") + tz},
            "selectedColumns": [{"key": k} for k in ("payloadId", "orderNumber", "customer", "amount", "date", "orderStatus", "amountInvoiced")],
            "sortingColumns": [{"key": "date", "isAscending": False}],
            "pageSize": 1000, "pageNumber": 0, "timezone": "Asia/Calcutta",
            "excludeOrderStatus": False, "subTypeOption": "no", "showOnlyInquiryDocuments": "no", "includeUomInfo": True,
        }
        d = await self._post("po-list-search", body)
        rows = [dict(zip(d["headers"], row)) for row in d.get("data", [])]
        hits = [r for r in rows if r.get("orderNumber") == po_number]
        if not hits:
            raise LookupError(f"PO {po_number} not found among {len(rows)} orders to invoice (last 365 days)")
        if len(hits) > 1:
            logger.warning(f"PO {po_number} matched {len(hits)} rows; using the most recent")
        return hits[0]

    @staticmethod
    def document_url(payload_id: str) -> str:
        return DOC_DETAIL + quote(payload_id, safe="")
