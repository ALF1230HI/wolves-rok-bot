"""
Per-Discord-server configuration, stored centrally in a private "master"
Google Sheet that only the bot's operator can view (buyers/admins never see
this sheet — they only interact with it indirectly via /config).

This lets a single bot instance serve many Discord servers, each pointing
`/donate` at their own donations spreadsheet, tab names, bank name, and
announcement channel — without needing a separate bot deployment per server.

The operator's master sheet must have a "Server Configs" tab with this
header row (created once, manually, by the operator):

    Guild ID | Guild Name | Added On (UTC) | Configured By |
    Donations Sheet ID | Donations Tab Name | Alliance Members Tab Name |
    Bank Name | Announce Channel ID | Last Updated (UTC)
"""

import os
import time
import asyncio
from datetime import datetime, timezone as dt_timezone

from sheets import _get_client  # reuse the bot's existing Google auth logic

# The operator's private master config/dashboard sheet. Not meant to ever be
# shared with buyers — only the bot operator has view access to it.
MASTER_SHEET_ID = os.getenv(
    "MASTER_CONFIG_SHEET_ID", "1-PaCoohjEmINZ9r6d9x1KZ3cOYxPtVV2b1Nw-D3_fiE"
)
MASTER_TAB_NAME = os.getenv("MASTER_CONFIG_TAB_NAME", "Server Configs")

HEADERS = [
    "Guild ID",
    "Guild Name",
    "Added On (UTC)",
    "Configured By",
    "Donations Sheet ID",
    "Donations Tab Name",
    "Alliance Members Tab Name",
    "Bank Name",
    "Announce Channel ID",
    "Last Updated (UTC)",
]

# Column indexes (1-based, matching HEADERS order) for targeted cell updates.
COL_GUILD_ID = 1
COL_GUILD_NAME = 2
COL_ADDED_ON = 3
COL_CONFIGURED_BY = 4
COL_SHEET_ID = 5
COL_DONATIONS_TAB = 6
COL_MEMBERS_TAB = 7
COL_BANK_NAME = 8
COL_ANNOUNCE_CHANNEL = 9
COL_LAST_UPDATED = 10

# Fallback settings for any guild that hasn't run /config yet — keeps the
# original WOLVES servers working out of the box without needing to
# reconfigure them, and gives new servers a sane default before they set
# their own sheet up.
DEFAULT_CONFIG = {
    "donations_sheet_id": os.getenv(
        "DONATIONS_SHEET_ID", "1spszkPihGZ9IIbe_v3Q2KtEai2awX1JITRxE09EzY4E"
    ),
    "donations_tab_name": os.getenv("DONATIONS_TAB_NAME", "Donations"),
    "alliance_members_tab_name": os.getenv("ALLIANCE_MEMBERS_TAB_NAME", "Alliance Members"),
    "bank_name": os.getenv("BANK_NAME", "KD Bank 53"),
    "announce_channel_id": None,
}

_CACHE_TTL_SECONDS = 60
_cache: dict[int, tuple[dict, float]] = {}


def _now_str() -> str:
    return datetime.now(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _get_master_ws():
    client = _get_client()
    sheet = client.open_by_key(MASTER_SHEET_ID)
    return sheet.worksheet(MASTER_TAB_NAME)


def _row_to_config(row: dict) -> dict:
    return {
        "donations_sheet_id": (row.get("Donations Sheet ID") or "").strip()
        or DEFAULT_CONFIG["donations_sheet_id"],
        "donations_tab_name": (row.get("Donations Tab Name") or "").strip()
        or DEFAULT_CONFIG["donations_tab_name"],
        "alliance_members_tab_name": (row.get("Alliance Members Tab Name") or "").strip()
        or DEFAULT_CONFIG["alliance_members_tab_name"],
        "bank_name": (row.get("Bank Name") or "").strip() or DEFAULT_CONFIG["bank_name"],
        "announce_channel_id": (
            int(row["Announce Channel ID"])
            if str(row.get("Announce Channel ID") or "").strip().isdigit()
            else None
        ),
    }


def _find_row_index_sync(ws, guild_id: int) -> int | None:
    """Return the 1-based row number for guild_id, or None if not present."""
    ids = ws.col_values(COL_GUILD_ID)  # includes header
    target = str(guild_id)
    for i, value in enumerate(ids[1:], start=2):
        if value.strip() == target:
            return i
    return None


def _get_config_sync(guild_id: int) -> dict:
    ws = _get_master_ws()
    records = ws.get_all_records()
    for row in records:
        if str(row.get("Guild ID", "")).strip() == str(guild_id):
            return _row_to_config(row)
    return dict(DEFAULT_CONFIG)


async def get_config(guild_id: int) -> dict:
    """Return this guild's config, falling back to DEFAULT_CONFIG for any
    field that hasn't been set. Cached briefly to avoid hitting the Sheets
    API on every single command invocation.
    """
    cached = _cache.get(guild_id)
    if cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]
    config = await asyncio.to_thread(_get_config_sync, guild_id)
    _cache[guild_id] = (config, time.time())
    return config


def _upsert_config_sync(
    guild_id: int,
    guild_name: str,
    configured_by: str,
    **updates,
) -> None:
    """Create or update this guild's row in the master sheet. `updates` may
    contain any of: donations_sheet_id, donations_tab_name,
    alliance_members_tab_name, bank_name, announce_channel_id. Unset fields
    are left untouched (or blank on first creation).
    """
    ws = _get_master_ws()
    row_idx = _find_row_index_sync(ws, guild_id)
    now = _now_str()

    field_to_col = {
        "donations_sheet_id": COL_SHEET_ID,
        "donations_tab_name": COL_DONATIONS_TAB,
        "alliance_members_tab_name": COL_MEMBERS_TAB,
        "bank_name": COL_BANK_NAME,
        "announce_channel_id": COL_ANNOUNCE_CHANNEL,
    }

    if row_idx is None:
        new_row = [""] * len(HEADERS)
        new_row[COL_GUILD_ID - 1] = str(guild_id)
        new_row[COL_GUILD_NAME - 1] = guild_name
        new_row[COL_ADDED_ON - 1] = now
        new_row[COL_CONFIGURED_BY - 1] = configured_by
        new_row[COL_LAST_UPDATED - 1] = now
        for field, value in updates.items():
            if value is not None and field in field_to_col:
                new_row[field_to_col[field] - 1] = str(value)
        ws.append_row(new_row, value_input_option="USER_ENTERED")
    else:
        # Update only the cells that changed, plus bookkeeping columns.
        ws.update_cell(row_idx, COL_GUILD_NAME, guild_name)
        ws.update_cell(row_idx, COL_CONFIGURED_BY, configured_by)
        ws.update_cell(row_idx, COL_LAST_UPDATED, now)
        for field, value in updates.items():
            if value is not None and field in field_to_col:
                ws.update_cell(row_idx, field_to_col[field], str(value))


async def set_config(guild_id: int, guild_name: str, configured_by: str, **updates) -> None:
    """Create/update a guild's config row, and invalidate its cache entry."""
    await asyncio.to_thread(_upsert_config_sync, guild_id, guild_name, configured_by, **updates)
    _cache.pop(guild_id, None)


def _verify_sheet_access_sync(sheet_id: str) -> tuple[bool, str]:
    """Try opening `sheet_id` with the bot's own credentials, to confirm it's
    been shared with the service account before saving it as a guild's
    config. Returns (ok, message)."""
    try:
        client = _get_client()
        sheet = client.open_by_key(sheet_id)
        return True, sheet.title
    except Exception as e:
        return False, str(e)


async def verify_sheet_access(sheet_id: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_verify_sheet_access_sync, sheet_id)
