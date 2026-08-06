"""
Google One automation for the Telegram bot.

This module keeps the existing public API while making Google authentication
resilient to redirects, consent screens, delayed rendering, regional layouts,
CAPTCHA/interstitial pages, and Railway's headless Chromium environment.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

import pyotp
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)

LOGIN_WAIT = max(int(getattr(config, "WEBDRIVER_TIMEOUT", 30)), 45)
PAGE_READY_WAIT = max(15, min(LOGIN_WAIT, 30))
DIAGNOSTIC_DIR = Path(os.environ.get("SELENIUM_DIAGNOSTIC_DIR", "/tmp/google_automation_diagnostics"))

EMAIL_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[name='identifier']"),
    (By.ID, "identifierId"),
    (By.CSS_SELECTOR, "input[autocomplete='username']"),
    (By.CSS_SELECTOR, "input[aria-label*='Email' i]"),
    (By.CSS_SELECTOR, "input[aria-label*='email' i]"),
)
PASSWORD_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.CSS_SELECTOR, "input[type='password']"),
    (By.CSS_SELECTOR, "input[name='Passwd']"),
    (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
)
EMAIL_NEXT_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.ID, "identifierNext"),
    (By.CSS_SELECTOR, "button#identifierNext"),
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.XPATH, "//button[contains(., 'Next') or contains(., 'Далее') or contains(., 'Suivant') or contains(., 'Siguiente') or contains(., 'Weiter') or contains(., 'Avanti') ]"),
    (By.XPATH, "//*[@role='button' and (contains(., 'Next') or contains(., 'Далее') or contains(., 'Continue'))]"),
)
PASSWORD_NEXT_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.ID, "passwordNext"),
    (By.CSS_SELECTOR, "button#passwordNext"),
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.XPATH, "//button[contains(., 'Next') or contains(., 'Далее') or contains(., 'Continue') or contains(., 'Продолжить') ]"),
    (By.XPATH, "//*[@role='button' and (contains(., 'Next') or contains(., 'Далее') or contains(., 'Continue'))]"),
)


class GoogleAutomationError(Exception):
    """Raised when Google automation cannot complete safely."""


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:100] or "unknown"


def _capture_diagnostics(driver, reason: str, exc: Optional[BaseException] = None) -> None:
    """Save URL/title, HTML, screenshot, and browser console logs for failures."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    prefix = DIAGNOSTIC_DIR / f"{stamp}-{_safe_name(reason)}"
    try:
        DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as mkdir_error:
        logger.warning("Could not create Selenium diagnostic directory %s: %s", DIAGNOSTIC_DIR, mkdir_error)
        return

    current_url = "<unavailable>"
    title = "<unavailable>"
    try:
        current_url = driver.current_url
    except Exception:
        pass
    try:
        title = driver.title
    except Exception:
        pass

    logger.error("Selenium failure reason=%s url=%s title=%s", reason, current_url, title)
    if exc:
        logger.error("Selenium failure exception=%s: %s", type(exc).__name__, exc)

    try:
        (prefix.with_suffix(".meta.txt")).write_text(
            f"reason={reason}\nurl={current_url}\ntitle={title}\nexception={exc!r}\n",
            encoding="utf-8",
        )
    except Exception as error:
        logger.warning("Could not save Selenium diagnostic metadata: %s", error)

    try:
        html = driver.page_source or ""
        (prefix.with_suffix(".html")).write_text(html, encoding="utf-8", errors="replace")
        logger.error("Saved Selenium page source: %s.html", prefix)
    except Exception as error:
        logger.warning("Could not save Selenium page source: %s", error)

    try:
        if driver.save_screenshot(str(prefix.with_suffix(".png"))):
            logger.error("Saved Selenium screenshot: %s.png", prefix)
    except Exception as error:
        logger.warning("Could not save Selenium screenshot: %s", error)

    try:
        browser_logs = driver.get_log("browser")
        (prefix.with_suffix(".browser.log")).write_text(
            "\n".join(str(item) for item in browser_logs), encoding="utf-8"
        )
        if browser_logs:
            logger.error("Browser console errors/logs: %s", browser_logs[-20:])
    except Exception as error:
        logger.debug("Browser console logs unavailable: %s", error)


def _build_driver(profile: DeviceProfile, email: str) -> webdriver.Chrome:
    options = Options()
    if config.HEADLESS:
        options.add_argument("--headless=new")

    chrome_binary = (
        getattr(config, "CHROME_BINARY", "")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chrome_binary:
        options.binary_location = chrome_binary
        logger.info("Using Chromium binary: %s", chrome_binary)
    else:
        logger.warning("No Chromium binary found on PATH; Selenium will use default browser discovery")

    if getattr(config, "PROXY", ""):
        options.add_argument(f"--proxy-server={config.PROXY}")
        logger.info("Configured web driver proxy server")

    clean_email = re.sub(r"[^a-zA-Z0-9]", "_", email)
    profile_path = Path(__file__).resolve().parent / "chrome_profiles" / f"profile_{clean_email}"
    profile_path.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_path}")

    # Required for unprivileged Railway containers and shared-memory limits.
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-US")
    options.add_argument("--accept-lang=en-US,en")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-blink-features=AutomationControlled")

    # The old device UA advertised Chrome 124 while Railway currently ships a
    # much newer Chromium. That mismatch can produce alternate login pages. Use
    # the real browser UA by default; retain the old emulation as an opt-in.
    if os.environ.get("GOOGLE_USE_DEVICE_USER_AGENT", "false").strip().lower() in {"1", "true", "yes", "on"}:
        options.add_argument(f"--user-agent={profile.user_agent}")
        logger.info("Using configured device user-agent by request")

    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)

    driver_path = getattr(config, "CHROMEDRIVER_PATH", "") or shutil.which("chromedriver")
    service = Service(executable_path=driver_path) if driver_path else Service()
    if driver_path:
        logger.info("Using chromedriver: %s", driver_path)

    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                """
            },
        )
    except (JavascriptException, WebDriverException) as error:
        logger.warning("Could not install Chromium startup script: %s", error)

    driver.implicitly_wait(0)
    driver.set_page_load_timeout(max(int(getattr(config, "PAGE_LOAD_TIMEOUT", 60)), 90))
    driver.set_script_timeout(max(int(getattr(config, "PAGE_LOAD_TIMEOUT", 60)), 90))
    return driver


def _wait_page_ready(driver, timeout: int = PAGE_READY_WAIT) -> None:
    """Wait for document readiness without assuming a particular Google layout."""
    WebDriverWait(driver, timeout, poll_frequency=0.25).until(
        lambda current: current.execute_script("return document.readyState") in {"interactive", "complete"}
    )
    WebDriverWait(driver, timeout, poll_frequency=0.25).until(
        lambda current: bool(current.execute_script("return document.body != null"))
    )


def _visible(element) -> bool:
    try:
        return element.is_displayed() and element.is_enabled()
    except (StaleElementReferenceException, WebDriverException):
        return False


def _find_in_current_document(driver, selectors: Sequence[tuple[str, str]]):
    for by, value in selectors:
        try:
            for element in driver.find_elements(by, value):
                if _visible(element):
                    return element
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue
    return None


def _find_in_frames(driver, selectors: Sequence[tuple[str, str]], max_depth: int = 2):
    """Find a visible element in the main document or nested login iframe."""
    element = _find_in_current_document(driver, selectors)
    if element:
        return element
    if max_depth <= 0:
        return None

    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except WebDriverException:
        return None

    for frame in frames:
        try:
            driver.switch_to.frame(frame)
            element = _find_in_frames(driver, selectors, max_depth - 1)
            if element:
                return element
            driver.switch_to.parent_frame()
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            try:
                driver.switch_to.parent_frame()
            except WebDriverException:
                driver.switch_to.default_content()
    return None


def _wait_for_any(driver, selectors: Sequence[tuple[str, str]], timeout: int = LOGIN_WAIT):
    """Wait for any robust selector, searching the main document and iframes."""
    def locate(current):
        try:
            return _find_in_frames(current, selectors)
        except WebDriverException:
            return False

    return WebDriverWait(driver, timeout, poll_frequency=0.25, ignored_exceptions=(StaleElementReferenceException,)).until(locate)


def _click_any(driver, selectors: Sequence[tuple[str, str]], timeout: int = LOGIN_WAIT) -> None:
    element = _wait_for_any(driver, selectors, timeout)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    except WebDriverException:
        pass
    try:
        element.click()
    except (ElementClickInterceptedException, WebDriverException):
        driver.execute_script("arguments[0].click();", element)
    finally:
        driver.switch_to.default_content()


def _page_text(driver) -> str:
    """Return visible page text for classification, not raw scripts or hidden markup."""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return f"{driver.current_url}\n{driver.title}\n{body_text}".lower()
    except WebDriverException:
        return ""


def _detect_unexpected_page(driver) -> Optional[str]:
    url = ""
    try:
        url = driver.current_url.lower()
    except WebDriverException:
        pass
    text = _page_text(driver)

    checks = (
        ("captcha", ("/recaptcha/", "captcha", "unusual traffic", "not a robot", "verify you are human")),
        ("verify_its_you", ("verify it's you", "verify it’s you", "confirm it's you", "verify your identity")),
        ("suspicious_login", ("suspicious sign in", "suspicious login", "account security", "this browser or app may not be secure")),
        ("network_error", ("err_name_not_resolved", "err_connection", "this site can’t be reached", "this site can't be reached", "network error")),
        ("service_unavailable", ("service unavailable", "temporarily unavailable", "500 internal server error", "502 bad gateway")),
    )
    for reason, markers in checks:
        if any(marker in text or marker in url for marker in markers):
            return reason
    return None


def _dismiss_consent_dialogs(driver) -> bool:
    """Dismiss common Google consent screens in multiple locales/layouts."""
    selectors = (
        (By.CSS_SELECTOR, "button[aria-label='Accept all']"),
        (By.CSS_SELECTOR, "button[aria-label='I agree']"),
        (By.CSS_SELECTOR, "[role='button'][aria-label='Accept all']"),
        (By.XPATH, "//button[contains(., 'Accept all') or contains(., 'I agree') or contains(., 'Agree') or contains(., 'Accept') or contains(., 'Принять все') or contains(., 'Принять') or contains(., 'Tout accepter') or contains(., 'Accepter') or contains(., 'Aceptar todo') or contains(., 'Alle akzeptieren') or contains(., 'Accetta tutto') ]"),
        (By.XPATH, "//*[@role='button' and (contains(., 'Accept all') or contains(., 'I agree') or contains(., 'Принять все') or contains(., 'Tout accepter') or contains(., 'Aceptar todo'))]"),
    )
    try:
        element = _find_in_frames(driver, selectors, max_depth=2)
        if not element:
            return False
        logger.info("Google consent dialog detected; accepting it")
        try:
            element.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", element)
        finally:
            driver.switch_to.default_content()
        time.sleep(1)
        return True
    except WebDriverException as error:
        logger.warning("Consent dialog was detected but could not be dismissed: %s", error)
        driver.switch_to.default_content()
        return False


def _wait(driver, by, value, timeout=LOGIN_WAIT):
    """Backward-compatible single-selector wait helper."""
    return _wait_for_any(driver, ((by, value),), timeout)


def _do_login(driver, email, password, totp_key="") -> bool:
    """Login to Google using resilient selectors and explicit state checks."""
    try:
        logger.info("Opening Google login page: %s", config.GMAIL_LOGIN_URL)
        driver.get(config.GMAIL_LOGIN_URL)
        _wait_page_ready(driver)
        logger.info("Google login page loaded: url=%s title=%s", driver.current_url, driver.title)
        _dismiss_consent_dialogs(driver)

        unexpected = _detect_unexpected_page(driver)
        if unexpected:
            _capture_diagnostics(driver, f"initial-{unexpected}")
            return False

        hostname = (urlparse(driver.current_url).hostname or "").lower()
        if hostname in {"myaccount.google.com", "one.google.com", "mail.google.com"} and "/signin" not in driver.current_url.lower():
            logger.info("Already authenticated with the saved Chrome profile")
            return True

        try:
            email_field = _wait_for_any(driver, EMAIL_SELECTORS, LOGIN_WAIT)
        except TimeoutException as error:
            reason = _detect_unexpected_page(driver) or "email_field_not_found"
            _capture_diagnostics(driver, reason, error)
            logger.error("Google email input was not found after %ss; selectors=%s", LOGIN_WAIT, EMAIL_SELECTORS)
            return False

        logger.info("Google email input found using a supported selector")
        email_field.clear()
        email_field.send_keys(email)
        driver.switch_to.default_content()
        _click_any(driver, EMAIL_NEXT_SELECTORS, LOGIN_WAIT)
        _wait_page_ready(driver)
        _dismiss_consent_dialogs(driver)

        unexpected = _detect_unexpected_page(driver)
        if unexpected:
            _capture_diagnostics(driver, f"after-email-{unexpected}")
            return False

        try:
            password_field = _wait_for_any(driver, PASSWORD_SELECTORS, LOGIN_WAIT)
        except TimeoutException as error:
            reason = _detect_unexpected_page(driver) or "password_field_not_found"
            _capture_diagnostics(driver, reason, error)
            logger.error("Google password input was not found after email submission")
            return False

        password_field.clear()
        password_field.send_keys(password)
        driver.switch_to.default_content()
        _click_any(driver, PASSWORD_NEXT_SELECTORS, LOGIN_WAIT)
        _wait_page_ready(driver)

        if totp_key:
            _complete_totp(driver, totp_key)

        # Give the redirect a bounded amount of time, then classify the result.
        try:
            WebDriverWait(driver, LOGIN_WAIT, poll_frequency=0.5).until(
                lambda current: _detect_unexpected_page(current) is not None
                or (urlparse(current.current_url).hostname or "").lower() not in {"accounts.google.com", ""}
            )
        except TimeoutException:
            pass

        unexpected = _detect_unexpected_page(driver)
        if unexpected:
            _capture_diagnostics(driver, f"post-login-{unexpected}")
            return False

        hostname = (urlparse(driver.current_url).hostname or "").lower()
        if hostname == "accounts.google.com" and "/signin" in urlparse(driver.current_url).path.lower():
            _capture_diagnostics(driver, "login_still_on_signin")
            logger.error("Google remained on the sign-in page after credentials were submitted")
            return False

        logger.info("Google login completed: url=%s title=%s", driver.current_url, driver.title)
        return True
    except (TimeoutException, WebDriverException) as error:
        _capture_diagnostics(driver, "login-selenium-error", error)
        logger.exception("Google login Selenium failure")
        return False
    except Exception as error:
        _capture_diagnostics(driver, "login-unexpected-error", error)
        logger.exception("Unexpected Google login failure")
        return False


def _complete_totp(driver, totp_key: str) -> None:
    code = pyotp.TOTP(totp_key.replace(" ", "").upper()).now()
    logger.info("Submitting TOTP verification code")
    time.sleep(2)

    totp_selectors = (
        (By.CSS_SELECTOR, "input[type='tel']"),
        (By.CSS_SELECTOR, "input[id*='totp' i]"),
        (By.CSS_SELECTOR, "input[id*='code' i]"),
        (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
        (By.CSS_SELECTOR, "input[name='totp']"),
    )
    try:
        field = _wait_for_any(driver, totp_selectors, 15)
    except TimeoutException:
        logger.info("No direct TOTP field; trying alternate verification method")
        _click_any(
            driver,
            (
                (By.XPATH, "//*[contains(., 'Try another way') or contains(., 'Другой способ') or contains(., 'Другие способы') or contains(., 'Use another method') ]"),
            ),
            10,
        )
        _click_any(
            driver,
            ((By.XPATH, "//*[contains(., 'Authenticator') or contains(., 'Google Authenticator') or contains(., 'приложения') ]"),),
            15,
        )
        field = _wait_for_any(driver, totp_selectors, 15)

    field.clear()
    field.send_keys(code)
    driver.switch_to.default_content()
    _click_any(
        driver,
        (
            (By.ID, "totpNext"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(., 'Next') or contains(., 'Далее') or contains(., 'Continue')]"),
        ),
        15,
    )
    _wait_page_ready(driver, 20)


def _find_offer_link(driver):
    keywords = config.GEMINI_OFFER_KEYWORDS
    for link in driver.find_elements(By.TAG_NAME, "a"):
        try:
            text = (link.text + " " + (link.get_attribute("aria-label") or "")).lower()
            href = link.get_attribute("href") or ""
            if "LOCKED:" in href or "/benefits/" in href:
                continue
            if any(kw in text for kw in keywords) and href:
                return href
        except Exception:
            continue
    pat = re.compile(r"(gemini|upgrade|activate|offer|redeem|trial|checkout)", re.IGNORECASE)
    for link in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = link.get_attribute("href") or ""
            if "LOCKED:" in href or "/benefits/" in href:
                continue
            if pat.search(href):
                return href
        except Exception:
            continue
    return None


TRIAL_KEYWORDS = [
    "try for free", "start trial", "get started", "try free", "claim", "activate", "redeem", "get offer", "start free",
    "free trial", "get gemini", "upgrade", "try gemini", "get 12 month", "get 1 year",
    "попробовать бесплатно", "начать пробный", "получить предложение", "активировать", "получить бесплатно", "попробовать", "начать", "бесплатно", "получить",
]

OFFER_URL_PATTERNS = [
    "payments.google.com",
    "play.google.com/store/account",
    "one.google.com/checkout",
    "store.google.com",
]


def _find_checkout_url_after_clicks(driver) -> Optional[str]:
    cur_url = driver.current_url
    if any(pat in cur_url for pat in OFFER_URL_PATTERNS):
        logger.info("Current URL is already a checkout page: %s", cur_url)
        return cur_url

    selectors = ["button", "a", "[role='button']", "div[class*='btn']", "div[class*='button']", "span[class*='btn']", "span[class*='button']"]
    candidates = []
    seen = set()
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in elements:
                try:
                    if el in seen or not el.is_displayed():
                        continue
                    combined = ((el.text or "") + " " + (el.get_attribute("aria-label") or "")).lower()
                    if any(kw in combined for kw in TRIAL_KEYWORDS):
                        candidates.append(el)
                        seen.add(el)
                except Exception:
                    continue
        except Exception:
            continue

    logger.info("Found %d candidate offer buttons/links", len(candidates))
    if not candidates:
        return None

    original_handle = driver.current_window_handle
    for i, el in enumerate(candidates):
        try:
            text = (el.text or el.get_attribute("aria-label") or "Element").strip()
            logger.info("Clicking candidate %d/%d: %s", i + 1, len(candidates), text)
            pre_handles = driver.window_handles
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            time.sleep(1.5)

            post_handles = driver.window_handles
            if len(post_handles) > len(pre_handles):
                for handle in post_handles:
                    if handle == original_handle:
                        continue
                    try:
                        driver.switch_to.window(handle)
                        time.sleep(2)
                        new_url = driver.current_url
                        logger.info("New tab URL: %s", new_url)
                        if any(pat in new_url for pat in OFFER_URL_PATTERNS):
                            driver.close()
                            driver.switch_to.window(original_handle)
                            return new_url
                        driver.close()
                    except Exception as error:
                        logger.warning("Tab check error: %s", error)
                driver.switch_to.window(original_handle)

            cur_url = driver.current_url
            if any(pat in cur_url for pat in OFFER_URL_PATTERNS):
                return cur_url

            for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    src = iframe.get_attribute("src") or ""
                    if any(pat in src for pat in OFFER_URL_PATTERNS):
                        return src
                    driver.switch_to.frame(iframe)
                    for link_el in driver.find_elements(By.TAG_NAME, "a"):
                        href = link_el.get_attribute("href") or ""
                        if any(pat in href for pat in OFFER_URL_PATTERNS):
                            driver.switch_to.default_content()
                            return href
                    driver.switch_to.default_content()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
        except Exception as error:
            logger.warning("Candidate click execution error: %s", error)
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return None


def _check_google_one(driver):
    for url in ("https://one.google.com/offers", config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            _wait_page_ready(driver, PAGE_READY_WAIT)
            _dismiss_consent_dialogs(driver)
            html_content = (driver.page_source or "").lower()
            if "ошибка 404" in html_content or "error 404" in html_content or "404" in driver.title:
                logger.warning("404 page detected at %s; skipping", url)
                continue
            unexpected = _detect_unexpected_page(driver)
            if unexpected:
                _capture_diagnostics(driver, f"google-one-{unexpected}")
                continue
            link = _find_checkout_url_after_clicks(driver)
            if link:
                return link
        except Exception as error:
            _capture_diagnostics(driver, "google-one-navigation-error", error)
            logger.warning("Navigation to %s failed: %s", url, error)
    return None


def check_gemini_offer(email: str, password: str, device: DeviceProfile, totp_key: str = "") -> Optional[str]:
    """Login and find the Gemini Pro offer while preserving the existing caller API."""
    driver = None
    try:
        driver = _build_driver(device, email)
        if not _do_login(driver, email, password, totp_key):
            raise GoogleAutomationError("Google login failed; inspect Selenium diagnostics for the exact page state")
        logger.info("Logged in, searching Google One")
        return _check_google_one(driver)
    except GoogleAutomationError:
        raise
    except WebDriverException as error:
        if driver:
            _capture_diagnostics(driver, "webdriver-error", error)
        raise GoogleAutomationError(f"Selenium WebDriver failure: {error}") from error
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                logger.debug("Error while closing Selenium driver", exc_info=True)
