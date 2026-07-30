"""Automation package containing Playwright web automation handlers."""

from automation.browser import PlaywrightManager, get_browser_session

__all__ = ["PlaywrightManager", "get_browser_session"]
