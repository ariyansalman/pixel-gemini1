"""
Google Pixel device registration via Play Services API.
Adds a Pixel 10 Pro to the user's Google account device list.
"""
import logging
import requests
import base64
import json
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Google Play Services checkin / register endpoints
CHECKIN_URL = "https://android.googleapis.com/checkin"
REGISTER_URL = "https://android.googleapis.com/c2dm/register3"

# Pixel 10 Pro device constants
PIXEL_FINGERPRINT = "google/pixel_10_pro/pixel_10_pro:16/AP4A.250405.002/eng.user.release-keys"
PIXEL_HARDWARE = "pixel_10_pro"
PIXEL_MODEL = "Pixel 10 Pro"
PIXEL_BRAND = "google"
ANDROID_SDK = 36


def generate_android_id() -> int:
    """Generate a random 64-bit Android ID."""
    import random
    return random.randint(1, 2**63 - 1)


def generate_imei() -> str:
    """Generate a valid IMEI."""
    import random
    tac = "35" + "".join(random.choices("0123456789", k=6))
    serial = "".join(random.choices("0123456789", k=6))
    partial = tac + serial
    # Luhn checksum
    digits = [int(d) for d in partial + "0"]
    for i in range(len(digits) - 2, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    check = (10 - sum(digits) % 10) % 10
    return partial + str(check)


def google_checkin(email: str, oauth_token: str) -> Optional[str]:
    """
    Perform Google Play Services checkin.
    Returns the android_id string if successful.
    """
    android_id = generate_android_id()
    imei = generate_imei()

    # Build checkin request (simplified protobuf as JSON)
    checkin_data = {
        "imei": imei,
        "androidId": android_id,
        "googleAccount": email,
        "platform": 2,  # Android
        "deviceType": "phone",
        "deviceModel": PIXEL_MODEL,
        "deviceBrand": PIXEL_BRAND,
        "hardware": PIXEL_HARDWARE,
        "fingerprint": PIXEL_FINGERPRINT,
        "sdkVersion": ANDROID_SDK,
        "locale": "en_US",
        "timeZone": "America/New_York",
        "version": 3,
    }

    try:
        headers = {
            "Authorization": f"Bearer {oauth_token}",
            "Content-Type": "application/json",
            "User-Agent": "Android-Finsky/42.0.25 (api=3,versionCode=84202510,sdk=36,device=pixel_10_pro,hardware=pixel_10_pro,product=pixel_10_pro)",
        }
        resp = requests.post(CHECKIN_URL, json=checkin_data, headers=headers, timeout=30)
        logger.info("Checkin response: %s", resp.status_code)
        if resp.status_code == 200:
            logger.info("Device checkin successful! Android ID: %s", android_id)
            return str(android_id)
        else:
            logger.warning("Checkin failed: %s %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        logger.error("Checkin error: %s", e)
        return None


def register_device_for_gcm(oauth_token: str, android_id: str) -> bool:
    """
    Register device for Google Cloud Messaging (GCM/FCM).
    This officially adds the device to the account.
    """
    try:
        headers = {
            "Authorization": f"Bearer {oauth_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "app": "com.google.android.apps.gcs",
            "device": android_id,
            "sender": "314927920699",  # Google Play Services sender ID
            "cert": "38918a453d07199354f8b19af05ec6562ced5788",
            "app_ver": "84202510",
            "info": base64.b64encode(json.dumps({
                "platform": 2,
                "deviceModel": PIXEL_MODEL,
                "fingerprint": PIXEL_FINGERPRINT,
            }).encode()).decode(),
        }
        resp = requests.post(REGISTER_URL, data=data, headers=headers, timeout=30)
        logger.info("GCM register response: %s", resp.status_code)
        return resp.status_code == 200
    except Exception as e:
        logger.error("GCM register error: %s", e)
        return False


def add_pixel_device_to_account(email: str, oauth_token: str) -> bool:
    """
    Full flow: register a Pixel 10 Pro device to the Google account.
    Returns True if device was successfully added.
    """
    logger.info("Registering Pixel 10 Pro for %s...", email)

    android_id = google_checkin(email, oauth_token)
    if not android_id:
        logger.warning("Checkin failed, trying alternative method...")
        # Fallback: just generate an ID and try GCM registration
        android_id = str(generate_android_id())

    success = register_device_for_gcm(oauth_token, android_id)
    if success:
        logger.info("Pixel 10 Pro device added to account!")
    else:
        logger.warning("GCM registration failed")

    return success
