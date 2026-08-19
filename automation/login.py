"""SAP Fieldglass Login and Session Management automation module."""

from pathlib import Path
from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from config.settings import Settings


from automation.navigation import ensure_home_page_ready, HomePageNotReadyError


async def authenticate_session(context: BrowserContext, page: Page, settings: Settings) -> tuple[bool, Page]:
    """Authenticate SAP Fieldglass session using saved storage_state if available or fresh login.

    Args:
        context: Active Playwright BrowserContext.
        page: Active Playwright Page instance.
        settings: Application settings.

    Returns:
        tuple[bool, Page]: (True if authenticated successfully, False otherwise; with Page instance).
    """
    auth_file = settings.AUTH_FILE_PATH

    if settings.USE_SAVED_SESSION and auth_file.exists():
        logger.info(f"Checking saved session state from: {auth_file}")
        try:
            await page.goto(settings.SAP_URL, wait_until="domcontentloaded", timeout=settings.DEFAULT_TIMEOUT)
            page_title = await page.title()
            username_loc = page.get_by_role("textbox", name="Username")
            is_login_form_visible = await username_loc.is_visible() if await username_loc.count() > 0 else await page.is_visible("#usernameId_new, input[name='username']")

            if not is_login_form_visible and "Sign In" not in page_title:
                logger.success("Loaded existing session")
                try:
                    await ensure_home_page_ready(page, timeout=settings.DEFAULT_TIMEOUT)
                    return True, page
                except HomePageNotReadyError as prep_err:
                    logger.warning(f"Home page readiness failed for saved session: {prep_err}. Re-authenticating via fresh login...")
            else:
                logger.warning("Session expired")
        except Exception as exc:
            sanitized_msg = str(exc).encode("ascii", "replace").decode("ascii")
            logger.warning(f"Error checking session state: {sanitized_msg}")
            logger.warning("Session expired")

    logger.info("Performing fresh login")
    success, page = await login_to_fieldglass(page=page, settings=settings)
    if success:
        try:
            auth_file.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(auth_file))
            logger.success("Saved new session")
        except Exception as exc:
            logger.warning(f"Failed to save session state to {auth_file}: {exc}")

    return success, page


async def login_to_fieldglass(page: Page, settings: Settings) -> tuple[bool, Page]:
    """Navigate to SAP Fieldglass login page, fill credentials, and authenticate.

    Args:
        page: Active Playwright Page instance.
        settings: Application settings containing SAP URL and credentials.

    Returns:
        tuple[bool, Page]: (True if login succeeded, False otherwise; along with the Page instance).
    """
    url = settings.SAP_URL
    username = settings.SAP_USERNAME or "dynprodel"
    password = settings.SAP_PASSWORD or "Dynpro@2026"

    logger.info(f"Initiating SAP Fieldglass login to URL: {url}")

    try:
        # 1. Navigate to SAP Fieldglass login page
        logger.info(f"Navigating to {url}...")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=settings.DEFAULT_TIMEOUT)

        if response and response.status >= 400:
            logger.error(f"Failed to load login page. HTTP Status code: {response.status}")
            return False, page

        logger.info(f"Page loaded: '{await page.title()}'. Waiting for login form...")

        # 2. Locate username and password fields using recorded role locators & fallback selectors
        username_locator = page.get_by_role("textbox", name="Username")
        if await username_locator.count() == 0:
            username_locator = page.locator("#usernameId_new, input[name='username'], input[id*='username']")

        password_locator = page.get_by_role("textbox", name="Password")
        if await password_locator.count() == 0:
            password_locator = page.locator("#passwordId_new, input[name='password'], input[id*='password']")

        await username_locator.first.wait_for(state="visible", timeout=15000)
        logger.info("Login form fields detected.")

        # Handle cookie / TrustArc consent banner if overlaying the screen
        try:
            cookie_accept = page.locator(
                "#truste-consent-button, .trustarc-agree-btn, button:has-text('Accept All'), button:has-text('Agree')"
            )
            if await cookie_accept.count() > 0 and await cookie_accept.first.is_visible():
                logger.info("Dismissing cookie consent banner...")
                await cookie_accept.first.click(timeout=3000)
        except Exception as cookie_exc:
            logger.debug(f"Cookie banner check note: {cookie_exc}")

        # 3. Fill credentials
        logger.info(f"Entering username: {username}")
        await username_locator.first.click()
        await username_locator.first.fill(username)

        logger.info("Entering password...")
        await password_locator.first.click()
        await password_locator.first.fill(password)

        # 4. Click Sign In button or submit form
        logger.info("Submitting login form...")
        signin_button = page.get_by_role("button", name="Sign In")
        if await signin_button.count() > 0 and await signin_button.first.is_visible():
            await signin_button.first.click(timeout=10000)
        else:
            submit_selector = page.locator(".formLoginButton_new, button[type='submit'], input[type='submit'][value='Sign In']")
            await submit_selector.first.click(timeout=10000, force=True)

        # 5. Wait for dashboard / home page to load
        logger.info("Waiting for dashboard/home page to load...")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except PlaywrightTimeoutError:
            logger.warning("DOM content load timeout; proceeding with page verification.")

        current_url = page.url
        page_title = await page.title()
        logger.info(f"Post-login URL: {current_url}")
        logger.info(f"Post-login Page Title: {page_title}")

        # 6. Check for failure conditions or success indicators
        error_element = await page.query_selector(".errorMessage, #error_msg, .error_message, [class*='error']")
        if error_element:
            error_text = (await error_element.text_content() or "").strip()
            if error_text:
                logger.error(f"Login failed! SAP Fieldglass returned error: {error_text}")
                await _capture_failure_screenshot(page, settings, "login_error")
                return False, page

        is_still_login_input = await username_locator.first.is_visible()
        if is_still_login_input and "Sign In" in page_title:
            logger.error("Login failed! Still on the login page after submission.")
            await _capture_failure_screenshot(page, settings, "login_failed_still_on_login_page")
            return False, page

        # 7. Ensure Home Page is fully initialized before declaring success
        await ensure_home_page_ready(page, timeout=settings.DEFAULT_TIMEOUT)

        logger.success("SAP Fieldglass login completed successfully! Dashboard loaded.")
        return True, page

    except PlaywrightTimeoutError as exc:
        sanitized_msg = str(exc).encode("ascii", "replace").decode("ascii")
        logger.error(f"Timeout occurred during login process: {sanitized_msg}")
        await _capture_failure_screenshot(page, settings, "login_timeout")
        return False, page
    except Exception as exc:
        sanitized_msg = str(exc).encode("ascii", "replace").decode("ascii")
        logger.error(f"An error occurred during login: {sanitized_msg}")
        await _capture_failure_screenshot(page, settings, "login_exception")
        return False, page


async def _capture_failure_screenshot(page: Page, settings: Settings, filename_prefix: str) -> None:
    """Capture failure screenshot for debugging.

    Args:
        page: Playwright Page instance.
        settings: Application settings.
        filename_prefix: Prefix for screenshot file name.
    """
    try:
        settings.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = settings.SCREENSHOT_DIR / f"{filename_prefix}.png"
        await page.screenshot(path=str(filepath), full_page=True)
        logger.info(f"Saved failure screenshot to: {filepath}")
    except Exception as exc:
        logger.warning(f"Failed to capture screenshot: {exc}")
