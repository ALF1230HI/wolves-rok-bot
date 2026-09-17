"""
Google Sheets integration for the WOLVES | BRAVIA 3953 bot.

Writes donation records to the "Alliance Bank Donations Tracker" spreadsheet
using a Google service account. Runs the blocking gspread calls in a thread
so they don't block the bot's event loop.
"""

import os
import asyncio
from datetime import datetime, timezone as dt_timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Set these via environment variables (see .env.example)
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")
DONATIONS_SHEET_ID = os.getenv("DONATIONS_SHEET_ID", "1spszkPihGZ9IIbe_v3Q2KtEai2awX1JITRxE09EzY4E")
DONATIONS_TAB_NAME = os.getenv("DONATIONS_TAB_NAME", "Donations")
ALLIANCE_MEMBERS_TAB_NAME = os.getenv("ALLIANCE_MEMBERS_TAB_NAME", "Alliance Members")

# Column order must match the sheet's header row exactly:
# Date | Alliance Member | Food | Wood | Stone | Gold
RESOURCE_COLUMNS = ["Food", "Wood", "Stone", "Gold"]

_client = None


def _get_client():
    global _client
    if _client is None:
        if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Google service account credentials not found at {GOOGLE_CREDENTIALS_PATH}. "
                "See .env.example / README for setup instructions."
            )
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _ensure_member_exists_sync(sheet, display_name: str):
    """Make sure `display_name` has a row in the Alliance Members tab.

    The Donation Summary tab has SUMIF formulas pre-filled down to row 500
    that reference Alliance Members column A, so simply adding the name here
    is enough for their donation totals to start showing up automatically —
    no need to touch Donation Summary directly.
    """
    members_ws = sheet.worksheet(ALLIANCE_MEMBERS_TAB_NAME)
    existing_names = members_ws.col_values(1)  # column A, includes header
    # Case-insensitive match so "wren" and "Wren" aren't treated as different people
    normalized_existing = {name.strip().lower() for name in existing_names[1:] if name.strip()}
    if display_name.strip().lower() not in normalized_existing:
        members_ws.append_row([display_name, ""], value_input_option="USER_ENTERED")


def _append_donation_sync(display_name: str, resources: dict, date_str: str):
    client = _get_client()
    sheet = client.open_by_key(DONATIONS_SHEET_ID)

    # Add the donor to Alliance Members first (if they're not already there)
    # so the Donation Summary formulas pick up their totals right away.
    _ensure_member_exists_sync(sheet, display_name)

    worksheet = sheet.worksheet(DONATIONS_TAB_NAME)
    row = [date_str, display_name]
    for col in RESOURCE_COLUMNS:
        amount = resources.get(col, 0)
        row.append(amount if amount else "")

    worksheet.append_row(row, value_input_option="USER_ENTERED")


async def append_donation(display_name: str, resources: dict):
    """Append a new donation row. `resources` maps e.g. {'Food': 5000000, 'Gold': 2000000}.

    Also ensures the donor exists in the Alliance Members tab so the
    Donation Summary tab's formulas (which key off that list) include them.

    Runs the blocking Google Sheets API calls in a background thread so they
    don't stall the bot's event loop.
    """
    now = datetime.now(dt_timezone.utc)
    date_str = f"{now.month}/{now.day}/{now.strftime('%y')}"
    await asyncio.to_thread(_append_donation_sync, display_name, resources, date_str)
