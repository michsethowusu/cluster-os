"""
utils/zoom_api.py
-----------------
All Zoom Server-to-Server OAuth API calls for the AU ECED-FLN platform.
Uses the Zoom MEETINGS API (not Webinars).

Required environment variables (set in Coolify):
    ZOOM_ACCOUNT_ID
    ZOOM_CLIENT_ID
    ZOOM_CLIENT_SECRET

Zoom App setup:
  1. Go to marketplace.zoom.us → Develop → Build App → Server-to-Server OAuth
  2. Add scopes: meeting:write:admin  meeting:read:admin  recording:read:admin
  3. Activate the app, copy Account ID / Client ID / Client Secret into Coolify env vars.

Differences from Webinar API:
  - Endpoint: /v2/users/me/meetings  (was /v2/users/me/webinars)
  - Registrant endpoint: /v2/meetings/{id}/registrants
  - Recording endpoint:  /v2/meetings/{id}/recordings
  - Meeting type 2 = scheduled meeting (was type 5 webinar)
  - approval_type 0 = auto-approve registrants
  - The column in the DB is still named zoom_webinar_id for backwards
    compatibility — it now stores a Zoom meeting ID instead.
"""

import os
import requests
import base64
from datetime import datetime, timezone


# ── Token cache (in-process, resets on worker restart — fine for low traffic) ──
_token_cache = {"access_token": None, "expires_at": 0}


def _get_access_token():
    """Fetch or return a cached Zoom access token via Server-to-Server OAuth."""
    now = datetime.now(timezone.utc).timestamp()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    account_id    = os.environ.get("ZOOM_ACCOUNT_ID")
    client_id     = os.environ.get("ZOOM_CLIENT_ID")
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET")

    if not all([account_id, client_id, client_secret]):
        raise RuntimeError(
            "Zoom credentials missing. Set ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, "
            "ZOOM_CLIENT_SECRET in your environment variables."
        )

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    resp = requests.post(
        f"https://zoom.us/oauth/token"
        f"?grant_type=account_credentials&account_id={account_id}",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"]   = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers():
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def create_zoom_webinar(event):
    """
    Create a Zoom Meeting for the given Event object.
    The function is intentionally still named create_zoom_webinar so that
    all existing call-sites in app.py continue to work without renaming.

    Returns the Zoom meeting_id (string) on success, raises on failure.

    Zoom meeting type 2 = scheduled meeting.
    registration_type 1 = register once.
    approval_type 0 = automatically approve registrants.
    auto_recording = 'cloud' so the recording can be fetched later.
    """
    start_iso = event.start_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    if event.end_date:
        duration = max(1, int((event.end_date - event.start_date).total_seconds() // 60))
    else:
        duration = 60

    payload = {
        "topic":      event.title,
        "type":       2,            # 2 = scheduled meeting
        "start_time": start_iso,
        "duration":   duration,
        "timezone":   "UTC",
        "agenda":     event.description[:2000],
        "settings": {
            "host_video":              True,
            "participant_video":       False,
            "join_before_host":        False,
            "mute_upon_entry":         True,
            "approval_type":           0,   # 0 = auto-approve
            "registration_type":       1,   # 1 = once per occurrence
            "audio":                   "both",
            "auto_recording":          "cloud",
            "registrants_email_notification":    True,
            "registrants_confirmation_email":    True,
            # Require registration so each attendee gets a unique join link
            "meeting_authentication":  False,
        },
    }

    resp = requests.post(
        "https://api.zoom.us/v2/users/me/meetings",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data["id"])


def register_user_for_webinar(meeting_id, user):
    """
    Register a platform member for an existing Zoom Meeting.
    The function is intentionally still named register_user_for_webinar so
    that all existing call-sites in app.py continue to work without renaming.

    Zoom will send the member its own branded confirmation email with the
    unique join link.
    Returns the registrant join_url on success, raises on failure.
    """
    parts      = user.name.strip().split(" ", 1)
    first_name = parts[0]
    last_name  = parts[1] if len(parts) > 1 else "."

    payload = {
        "email":      user.email,
        "first_name": first_name,
        "last_name":  last_name,
        "org":        getattr(user, "organization", ""),
    }

    resp = requests.post(
        f"https://api.zoom.us/v2/meetings/{meeting_id}/registrants",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("join_url", "")


def fetch_recording_url(meeting_id):
    """
    Fetch the cloud recording share URL for a completed meeting.
    Returns the share_url string, or None if no recording is found yet.
    Raises on API errors (except 404 which returns None gracefully).
    """
    resp = requests.get(
        f"https://api.zoom.us/v2/meetings/{meeting_id}/recordings",
        headers=_headers(),
        timeout=15,
    )

    if resp.status_code == 404:
        return None   # Recording not available yet

    resp.raise_for_status()
    data = resp.json()
    return data.get("share_url") or None


def delete_zoom_webinar(meeting_id):
    """
    Delete a Zoom Meeting (called when admin deletes an event).
    The function is intentionally still named delete_zoom_webinar so that
    all existing call-sites in app.py continue to work without renaming.

    Silently ignores 404 (already deleted on Zoom side).
    """
    resp = requests.delete(
        f"https://api.zoom.us/v2/meetings/{meeting_id}",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code not in (204, 404):
        resp.raise_for_status()
