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
from datetime import datetime, timedelta, timezone as dt_timezone

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

# Monday=0 ... Saturday=5. The alliance's weekly donation window resets
# every Saturday -- shared by the bot's DONATION GUIDE post and the
# donation website so both always agree on exactly the same week, instead
# of each computing their own slightly-different version of "this week".
WEEKLY_RESET_WEEKDAY = 5


def current_week_window(today=None) -> tuple:
    """Returns (start_date, end_date) for the current Saturday-to-Friday
    week (7 days total). If `today` is itself a Saturday, that's the first
    day of a brand new week, so the window starts right there and runs
    through the following Friday. `today` is injectable for testing;
    defaults to today's date in UTC terms (callers needing a specific
    timezone's "today" should pass it in explicitly)."""
    if today is None:
        today = datetime.now(dt_timezone.utc).date()
    days_since_reset = (today.weekday() - WEEKLY_RESET_WEEKDAY) % 7
    start = today - timedelta(days=days_since_reset)
    end = start + timedelta(days=6)
    return start, end


def days_until_reset(today=None) -> int:
    """0 if `today` is reset day (Saturday) itself, otherwise how many days
    remain until the next one."""
    if today is None:
        today = datetime.now(dt_timezone.utc).date()
    return (WEEKLY_RESET_WEEKDAY - today.weekday()) % 7

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


def _get_top_donors_sync(sheet_id: str, summary_tab_name: str, limit: int):
    client = _get_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(summary_tab_name)
    rows = ws.get_all_values()[1:]  # skip header row (Donor | Total | Food | Wood | Stone | Gold)

    def parse_number(value: str) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except (ValueError, AttributeError):
            return 0.0

    totals: dict[str, float] = {}
    for row in rows:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        total = parse_number(row[1]) if len(row) > 1 else 0.0
        # Some sheets end up with a member listed more than once (e.g. a
        # rename) -- keep whichever total is higher rather than showing
        # duplicate rows for the same person.
        totals[name] = max(totals.get(name, 0.0), total)

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]


async def get_top_donors(sheet_id: str, summary_tab_name: str = "Donation Summary", limit: int = 5):
    """Returns the top `limit` donors as a list of (name, total) tuples,
    read from the sheet's "Donation Summary" tab (which already totals each
    member's donations via SUMIF formulas -- see _ensure_member_exists_sync).
    Runs the blocking Google Sheets API call in a background thread so it
    doesn't stall the bot's event loop."""
    return await asyncio.to_thread(_get_top_donors_sync, sheet_id, summary_tab_name, limit)


def _get_weekly_top_donors_sync(
    sheet_id: str, donations_tab_name: str, start_date, end_date, limit: int
):
    client = _get_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(donations_tab_name)
    rows = ws.get_all_values()[1:]  # skip header (Date | Alliance Member | Food | Wood | Stone | Gold)

    def parse_number(value: str) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except (ValueError, AttributeError):
            return 0.0

    # Keyed by lowercased name so e.g. "Wren" and "wren" are merged into one
    # donor instead of splitting their total across two leaderboard rows
    # (mirrors the case-insensitive matching _ensure_member_exists_sync
    # already does). The display name shown is whichever casing was seen
    # first in the sheet.
    totals: dict[str, float] = {}
    display_names: dict[str, str] = {}
    for row in rows:
        if not row or not row[0].strip() or len(row) < 2 or not row[1].strip():
            continue
        try:
            row_date = datetime.strptime(row[0].strip(), "%m/%d/%y").date()
        except ValueError:
            continue  # skip rows with an unparseable/blank date rather than crash the whole report
        if not (start_date <= row_date <= end_date):
            continue

        name = row[1].strip()
        key = name.lower()
        display_names.setdefault(key, name)
        row_total = sum(parse_number(v) for v in row[2:6])
        totals[key] = totals.get(key, 0.0) + row_total

    ranked = sorted(
        ((display_names[key], total) for key, total in totals.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    return ranked[:limit]


async def get_weekly_top_donors(
    sheet_id: str, start_date, end_date, donations_tab_name: str = "Donations", limit: int = 5
):
    """Returns the top `limit` donors for the [start_date, end_date] window
    (inclusive), summing each donor's Food+Wood+Stone+Gold across every row
    of the raw "Donations" tab that falls in that range. Used for the
    DONATION GUIDE's weekly leaderboard, which resets every Saturday rather
    than showing an all-time total. Runs the blocking Sheets API call in a
    background thread so it doesn't stall the bot's event loop."""
    return await asyncio.to_thread(
        _get_weekly_top_donors_sync, sheet_id, donations_tab_name, start_date, end_date, limit
    )


def _compute_weekly_compliance_sync(
    sheet_id: str,
    donations_tab_name: str,
    members_tab_name: str,
    start_date,
    end_date,
    minimum: float,
):
    """Shared logic for the two public compliance functions below -- reads
    the registered member list plus every donation row in the window and
    returns (compliant_names, member_names), both in the Alliance Members
    tab's original order/casing."""
    client = _get_client()
    sheet = client.open_by_key(sheet_id)

    members_ws = sheet.worksheet(members_tab_name)
    member_names = [n.strip() for n in members_ws.col_values(1)[1:] if n.strip()]

    donations_ws = sheet.worksheet(donations_tab_name)
    rows = donations_ws.get_all_values()[1:]

    def parse_number(value: str) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except (ValueError, AttributeError):
            return 0.0

    # Per-member, per-resource totals for the week (case-insensitive key, same
    # reasoning as get_weekly_top_donors -- a member's name casing can vary
    # between rows).
    per_resource: dict[str, dict[str, float]] = {}
    for row in rows:
        if not row or not row[0].strip() or len(row) < 2 or not row[1].strip():
            continue
        try:
            row_date = datetime.strptime(row[0].strip(), "%m/%d/%y").date()
        except ValueError:
            continue
        if not (start_date <= row_date <= end_date):
            continue

        key = row[1].strip().lower()
        bucket = per_resource.setdefault(key, {col: 0.0 for col in RESOURCE_COLUMNS})
        for i, col in enumerate(RESOURCE_COLUMNS):
            if len(row) > 2 + i:
                bucket[col] += parse_number(row[2 + i])

    compliant_names = []
    for name in member_names:
        bucket = per_resource.get(name.lower())
        if bucket and all(bucket[col] >= minimum for col in RESOURCE_COLUMNS):
            compliant_names.append(name)

    return compliant_names, member_names


async def get_weekly_compliance(
    sheet_id: str,
    start_date,
    end_date,
    donations_tab_name: str = "Donations",
    members_tab_name: str = "Alliance Members",
    minimum: float = 10_000_000,
):
    """Returns (compliant_count, total_members) -- how many registered
    alliance members have donated at least `minimum` of EACH resource
    (Food, Wood, Stone, Gold) within [start_date, end_date]. Used for the
    DONATION GUIDE's weekly compliance tracker. Runs the blocking Sheets
    API call in a background thread so it doesn't stall the bot's event
    loop."""
    compliant_names, member_names = await asyncio.to_thread(
        _compute_weekly_compliance_sync, sheet_id, donations_tab_name, members_tab_name, start_date, end_date, minimum
    )
    return len(compliant_names), len(member_names)


async def get_weekly_compliance_detail(
    sheet_id: str,
    start_date,
    end_date,
    donations_tab_name: str = "Donations",
    members_tab_name: str = "Alliance Members",
    minimum: float = 10_000_000,
):
    """Like get_weekly_compliance, but returns the actual
    (compliant_names, total_members) instead of just a count -- used by the
    donation website's Compliance tab to show who's already hit this
    week's minimum."""
    compliant_names, member_names = await asyncio.to_thread(
        _compute_weekly_compliance_sync, sheet_id, donations_tab_name, members_tab_name, start_date, end_date, minimum
    )
    return compliant_names, len(member_names)


def _get_donor_weekly_progress_sync(
    sheet_id: str, donations_tab_name: str, display_name: str, start_date, end_date
):
    client = _get_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(donations_tab_name)
    rows = ws.get_all_values()[1:]

    def parse_number(value: str) -> float:
        try:
            return float(str(value).replace(",", "").strip() or 0)
        except (ValueError, AttributeError):
            return 0.0

    key = display_name.strip().lower()
    totals = {col: 0.0 for col in RESOURCE_COLUMNS}
    for row in rows:
        if not row or not row[0].strip() or len(row) < 2 or not row[1].strip():
            continue
        if row[1].strip().lower() != key:
            continue
        try:
            row_date = datetime.strptime(row[0].strip(), "%m/%d/%y").date()
        except ValueError:
            continue
        if not (start_date <= row_date <= end_date):
            continue
        for i, col in enumerate(RESOURCE_COLUMNS):
            if len(row) > 2 + i:
                totals[col] += parse_number(row[2 + i])

    return totals


async def get_donor_weekly_progress(
    sheet_id: str,
    display_name: str,
    start_date,
    end_date,
    donations_tab_name: str = "Donations",
):
    """Returns {'Food': total, 'Wood': total, 'Stone': total, 'Gold': total}
    for one specific donor within [start_date, end_date] -- used by the
    donation website's success page to show "here's how close you are to
    this week's 10M-per-resource minimum" progress bars right after someone
    submits. Runs the blocking Sheets API call in a background thread so it
    doesn't stall the event loop."""
    return await asyncio.to_thread(
        _get_donor_weekly_progress_sync, sheet_id, donations_tab_name, display_name, start_date, end_date
    )


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
