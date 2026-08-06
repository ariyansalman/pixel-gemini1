"""
Android emulator automation via uiautomator2.
Logs into Google, navigates Google One, extracts Gemini Pro offer link.
"""
import logging, time, re
from typing import Optional

import uiautomator2 as u2

import config

logger = logging.getLogger(__name__)

TRIAL_KEYWORDS = ["try for free", "start trial", "get started", "try free", "claim", "activate"]


def _connect() -> u2.Device:
    d = u2.connect()
    logger.info("Connected: %s", d.info)
    return d


def _find_and_click(d: u2.Device, text_contains: str, timeout: int = 15) -> bool:
    """Find element by text (case-insensitive) and click it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for elem in d(textContains=text_contains):
            try:
                elem.click()
                return True
            except Exception:
                pass
        for elem in d(descriptionContains=text_contains):
            try:
                elem.click()
                return True
            except Exception:
                pass
        time.sleep(1)
    return False


def _type_text(d: u2.Device, text: str):
    """Type text into focused field."""
    d.send_keys(text)


def _wait_for_text(d: u2.Device, text: str, timeout: int = 20) -> bool:
    """Wait until text appears on screen."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if d(text=text).exists or d(textContains=text).exists:
            return True
        time.sleep(1)
    return False


def _extract_url_from_browser(d: u2.Device) -> Optional[str]:
    """Get current URL from Chrome address bar."""
    try:
        # Try to get URL via content description
        info = d.dump_hierarchy()
        # Look for URLs in text fields
        for elem in d(className="android.widget.EditText"):
            text = elem.get_text() or ""
            if "google.com" in text or "one.google.com" in text or "play.google.com" in text:
                return text
        # Try getting from app info
        result = d.shell("dumpsys activity activities | grep 'mResumedActivity'")
        logger.info("Activity: %s", result)
    except Exception as e:
        logger.warning("URL extraction error: %s", e)
    return None


def _login_google(d: u2.Device, email: str, password: str, totp_key: str = "") -> bool:
    """Log into Google account on the emulator."""
    # Open Chrome and go to Google login
    d.shell("am start -a android.intent.action.VIEW -d https://accounts.google.com/signin")
    time.sleep(5)

    # Enter email
    if not _find_and_click(d, "email", timeout=10):
        # Try finding email field directly
        for elem in d(className="android.widget.EditText"):
            try:
                elem.click()
                break
            except Exception:
                pass
    time.sleep(1)
    _type_text(d, email)
    time.sleep(1)
    d.press("enter")
    time.sleep(4)

    # Enter password
    for elem in d(className="android.widget.EditText"):
        try:
            elem.click()
            break
        except Exception:
            pass
    time.sleep(1)
    _type_text(d, password)
    time.sleep(1)
    d.press("enter")
    time.sleep(5)

    # TOTP if needed
    if totp_key:
        try:
            import pyotp
            code = pyotp.TOTP(totp_key.replace(" ", "").upper()).now()
            logger.info("TOTP: %s", code)
            time.sleep(3)
            for elem in d(className="android.widget.EditText"):
                try:
                    elem.click()
                    break
                except Exception:
                    pass
            time.sleep(1)
            _type_text(d, code)
            time.sleep(1)
            d.press("enter")
            time.sleep(4)
        except Exception as e:
            logger.warning("TOTP failed: %s", e)

    time.sleep(3)
    return True


def _navigate_google_one(d: u2.Device) -> Optional[str]:
    """Navigate to Google One and find trial offer."""
    d.shell("am start -a android.intent.action.VIEW -d https://one.google.com/benefits")
    time.sleep(5)

    # Accept cookies if present
    _find_and_click(d, "accept all", timeout=3)
    _find_and_click(d, "agree", timeout=3)

    # Look for trial button
    for kw in TRIAL_KEYWORDS:
        if _find_and_click(d, kw, timeout=2):
            logger.info("Clicked trial button: %s", kw)
            time.sleep(8)
            url = _extract_url_from_browser(d)
            if url:
                return url
            # Check if URL changed
            result = d.shell("dumpsys activity activities | grep 'mResumedActivity'")
            for line in (result or "").split("\n"):
                if "one.google.com" in line or "play.google.com" in line or "payments.google.com" in line:
                    return line.strip()

    # Scan all clickable elements for trial-related text
    for elem in d(clickable=True):
        try:
            text = (elem.get_text() or "").lower()
            desc = (elem.get_content_description() or "").lower()
            combined = text + " " + desc
            if any(kw in combined for kw in TRIAL_KEYWORDS):
                logger.info("Clicking: %s", combined[:80])
                elem.click()
                time.sleep(8)
                url = _extract_url_from_browser(d)
                if url:
                    return url
        except Exception:
            continue

    return None


def check_gemini_offer_emulator(email: str, password: str, totp_key: str = "") -> Optional[str]:
    """
    Full automation via Android emulator.
    Returns the checkout URL if offer found.
    """
    d = None
    try:
        d = _connect()

        # Unlock screen
        d.unlock()
        d.press("home")

        logger.info("Logging into Google...")
        if not _login_google(d, email, password, totp_key):
            return None

        logger.info("Navigating Google One...")
        link = _navigate_google_one(d)
        return link

    except Exception as e:
        logger.exception("Emulator automation error")
        return None
    finally:
        if d:
            try:
                d.press("home")
            except Exception:
                pass
