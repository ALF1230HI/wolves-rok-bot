"""
Google Sheets integration for /donate.

Writes donation records to whichever spreadsheet a Discord server has
configured via /config (see server_config.py) using the bot's single shared
Google service account. Runs the blocking gspread calls in a thread so they
don't block the bot's event loop.
"""

import os
import json
import asyncio
from datetime import datetime, timezone as dt_timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Set these via environment variables (see .env.example)
# Either point to a JSON key file on disk...
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")
# ...or (useful on hosts like Railway where you can't upload a file) paste the
# entire JSON key contents into a GOOGLE_CREDENTIALS_JSON environment variable.
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Column order must match each configured sheet's header row exactly:
# Date | Alliance Member | Food | Wood | Stone | Gold
RESOURCE_COLUMNS = ["Food", "Wood", "Stone", "Gold"]

_client = None
_service_account_email = None


def get_service_account_email() -> str:
    """Return the bot's Google service account email, so admins know exactly
    which account to share their donations spreadsheet with in /config."""
    global _service_account_email
    if _service_account_email is None:
        if GOOGLE_CREDENTIALS_JSON:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
        elif os.path.exists(GOOGLE_CREDENTIALS_PATH):
            with open(GOOGLE_CREDENTIALS_PATH) as f:
                info = json.load(f)
        else:
            return "the bot's service account (credentials not found)"
        _service_account_email = info.get("client_email", "the bot's service account")
    return _service_account_email


def _get_client():
    global _client
    if _client is None:
        if GOOGLE_CREDENTIALS_JSON:
            # Credentials provided inline via env var (e.g. on Railway).
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        elif os.path.exists(GOOGLE_CREDENTIALS_PATH):
            creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        else:
            raise FileNotFoundError(
                f"Google service account credentials not found. Set GOOGLE_CREDENTIALS_JSON "
                f"(paste the full JSON key contents) or place a key file at "
                f"{GOOGLE_CREDENTIALS_PATH}. See .env.example / README for setup instructions."
            )
        _client = gspread.authorize(creds)
    return _client


def _ensure_member_exists_sync(sheet, display_name: str, members_tab_name: str):
    """Make sure `display_name` has a row in the Alliance Members tab.

    The Donation Summary tab has SUMIF formulas pre-filled down to row 500
    that reference Alliance Members column A, so simply adding the name here
    is enough for their donation totals to start showing up automatically —
    no need to touch Donation Summary directly.
    """
    members_ws = sheet.worksheet(members_tab_name)
    existing_names = members_ws.col_values(1)  # column A, includes header
    # Case-insensitive match so "wren" and "Wren" aren't treated as different people
    normalized_existing = {name.strip().lower() for name in existing_names[1:] if name.strip()}
    if display_name.strip().lower() not in normalized_existing:
        members_ws.append_row([display_name, ""], value_input_option="USER_ENTERED")


def _append_donation_sync(
    display_name: str,
    resources: dict,
    date_str: str,
    sheet_id: str,
    donations_tab_name: str,
    members_tab_name: str,
):
    client = _get_client()
    sheet = client.open_by_key(sheet_id)

    # Add the donor to Alliance Members first (if they're not already there)
    # so the Donation Summary formulas pick up their totals right away.
    _ensure_member_exists_sync(sheet, display_name, members_tab_name)

    worksheet = sheet.worksheet(donations_tab_name)
    row = [date_str, display_name]
    for col in RESOURCE_COLUMNS:
        amount = resources.get(col, 0)
        row.append(amount if amount else "")

    worksheet.append_row(row, value_input_option="USER_ENTERED")


async def append_donation(
    display_name: str,
    resources: dict,
    sheet_id: str,
    donations_tab_name: str = "Donations",
    members_tab_name: str = "Alliance Members",
):
    """Append a new donation row to the given spreadsheet/tabs. `resources`
    maps e.g. {'Food': 5000000, 'Gold': 2000000}.

    Also ensures the donor exists in the Alliance Members tab so the
    Donation Summary tab's formulas (which key off that list) include them.

    Runs the blocking Google Sheets API calls in a background thread so they
    don't stall the bot's event loop.
    """
    now = datetime.now(dt_timezone.utc)
    date_str = f"{now.month}/{now.day}/{now.strftime('%y')}"
    await asyncio.to_thread(
        _append_donation_sync,
        display_name,
        resources,
        date_str,
        sheet_id,
        donations_tab_name,
        members_tab_name,
    )
