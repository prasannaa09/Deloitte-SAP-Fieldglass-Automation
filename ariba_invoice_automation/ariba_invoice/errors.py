"""Error types, secret-free error messages, and retry helpers."""

from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, TypeVar

from loguru import logger

T = TypeVar("T")


class FlowAbort(RuntimeError):
    """Stopped on purpose because a business/safety check failed. Not retried."""


class SessionExpired(RuntimeError):
    """The Ariba session ended (login page / session-expired page). Recover by logging in again."""


class TransientError(RuntimeError):
    """Network hiccup, timeout, 5xx, or a page that did not load. Safe to resync and retry."""


_CALL_LOG = re.compile(r"\n?\s*(Call log|=+ logs =+).*", re.S)
_SECRET_HDR = re.compile(r"(?im)^\s*-?\s*(cookie|authorization|set-cookie|x-ariba-session-id)\s*:.*$")


def clean_error(exc: BaseException) -> str:
    """Exception text without Playwright call logs, which include request headers (cookies, tokens)."""
    msg = _CALL_LOG.sub("", str(exc))
    msg = _SECRET_HDR.sub(r"\1: <redacted>", msg).strip()
    if not msg and isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        msg = "step took longer than its time limit"
    return f"{type(exc).__name__}: {msg[:500] or '(no message)'}"


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TransientError, asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "socket hang up", "econnreset", "econnrefused", "etimedout",
                                   "network", "net::err", "target closed", "http 5", "503", "502", "504"))


async def with_retries(fn: Callable[[], Awaitable[T]], what: str, attempts: int = 3, base_delay: float = 3.0) -> T:
    """Retry an IDEMPOTENT call on transient failures with exponential backoff."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except (FlowAbort, SessionExpired):
            raise
        except Exception as e:  # noqa: BLE001
            if i == attempts or not is_transient(e):
                raise
            delay = base_delay * 2 ** (i - 1)
            logger.warning(f"{what}: {clean_error(e)} — retry {i}/{attempts - 1} in {delay:.0f}s")
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
