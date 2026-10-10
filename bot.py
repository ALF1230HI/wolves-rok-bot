"""
WOLVES | BRAVIA 3953 - Rise of Kingdoms Alliance Discord Bot
--------------------------------------------------------------
Features:
  /shop      -> Posts the resource shop price list
  /announce  -> (Admin) Posts a custom red-accent announcement embed
  /kvk       -> (Admin) Posts a KvK date/time announcement with auto timezone conversion
  /ping      -> Basic health check

Setup:
  1. pip install -r requirements.txt
  2. Copy .env.example to .env and fill in your bot token
  3. python bot.py
"""

import os
import re
import time
import discord
import aiohttp
from aiohttp import web as aiohttp_web
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Union
from dotenv import load_dotenv

import stats_db
import sheets
import server_config
import donate_token
import webapp
import ai_helper

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Optional: set for instant slash command sync (otherwise global sync can take
# up to an hour to show the first time). Accepts either a single ID via
# GUILD_ID, or multiple comma-separated IDs via GUILD_IDS, e.g.
# GUILD_IDS=1513550074473480394,1386443294644633721
_guild_ids_raw = os.getenv("GUILD_IDS") or os.getenv("GUILD_ID") or ""
GUILD_IDS = [int(g.strip()) for g in _guild_ids_raw.split(",") if g.strip()]
GUILD_ID = GUILD_IDS[0] if GUILD_IDS else None  # kept for backwards compatibility

RED = 0xFF0000
PAYPAL_EMOJI = "<:paypallogo:1551673165233790996>"  # update with your server's emoji id if different

# /announce posts into the announcement channel belonging to whichever server
# the command was run in. Add an entry here for every server the bot is in.
ANNOUNCE_CHANNELS = {
    1513550074473480394: 1514659627936120992,  # "Bleasy's ROK server"
    1386443294644633721: 1386520678295015536,  # "WOLVES 🐺 | BRAVIA 3953"
}
# Fallback used if a server isn't in the map above (keeps old behavior working).
ANNOUNCE_CHANNEL_ID = 1514659627936120992

# ---- Resource shop pricing (edit these anytime) ----
SHOP_ITEMS = [
    ("Food", "$2 per 100M"),
    ("Wood", "$2 per 100M"),
    ("Stone", "$2 per 100M"),
    ("Gold", "$4 per 100M"),
    ("Mixed Bundle (100M each)", "$10"),
    ("Mixed Bundle (500M each)", "$50"),
]

# ---- Timezones offered in the /kvk dropdown menu ----
TIMEZONE_CHOICES = [
    ("🌐 UTC", "UTC"),
    ("🇺🇸 US Eastern (New York)", "America/New_York"),
    ("🇺🇸 US Central (Chicago)", "America/Chicago"),
    ("🇺🇸 US Mountain (Denver)", "America/Denver"),
    ("🇺🇸 US Pacific (Los Angeles)", "America/Los_Angeles"),
    ("🇬🇧 UK (London)", "Europe/London"),
    ("🇮🇪 Ireland (Dublin)", "Europe/Dublin"),
    ("🇪🇺 Central Europe (Berlin/Paris)", "Europe/Berlin"),
    ("🇪🇺 Eastern Europe (Athens)", "Europe/Athens"),
    ("🇹🇷 Turkey (Istanbul)", "Europe/Istanbul"),
    ("🇷🇺 Russia (Moscow)", "Europe/Moscow"),
    ("🇦🇪 UAE (Dubai)", "Asia/Dubai"),
    ("🇮🇳 India (Delhi)", "Asia/Kolkata"),
    ("🇨🇳 China (Shanghai)", "Asia/Shanghai"),
    ("🇸🇬 Singapore", "Asia/Singapore"),
    ("🇵🇭 Philippines (Manila)", "Asia/Manila"),
    ("🇮🇩 Indonesia (Jakarta)", "Asia/Jakarta"),
    ("🇯🇵 Japan (Tokyo)", "Asia/Tokyo"),
    ("🇰🇷 South Korea (Seoul)", "Asia/Seoul"),
    ("🇦🇺 Australia East (Sydney)", "Australia/Sydney"),
    ("🇦🇺 Australia West (Perth)", "Australia/Perth"),
    ("🇳🇿 New Zealand (Auckland)", "Pacific/Auckland"),
    ("🇧🇷 Brazil (Sao Paulo)", "America/Sao_Paulo"),
    ("🇲🇽 Mexico (Mexico City)", "America/Mexico_City"),
    ("🇿🇦 South Africa (Johannesburg)", "Africa/Johannesburg"),
]

# ---- Common timezone abbreviations -> IANA zone name ----
# Note: some abbreviations are ambiguous in real life (e.g. CST/IST/EST have multiple
# meanings worldwide). We map each to the most common interpretation for our playerbase.
TZ_ABBREVIATIONS = {
    "UTC": "UTC",
    "GMT": "UTC",
    "BST": "Europe/London",          # British Summer Time
    "IST": "Asia/Kolkata",           # Indian Standard Time (most common meaning)
    "WET": "Europe/Lisbon",
    "CET": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "EET": "Europe/Athens",
    "EEST": "Europe/Athens",
    "MSK": "Europe/Moscow",
    "TRT": "Europe/Istanbul",
    "GST": "Asia/Dubai",             # Gulf Standard Time
    "PKT": "Asia/Karachi",
    "BDT": "Asia/Dhaka",
    "ICT": "Asia/Bangkok",
    "SGT": "Asia/Singapore",
    "MYT": "Asia/Kuala_Lumpur",
    "WIB": "Asia/Jakarta",
    "PHT": "Asia/Manila",
    "HKT": "Asia/Hong_Kong",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "ACST": "Australia/Adelaide",
    "AWST": "Australia/Perth",
    "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage",
    "HST": "Pacific/Honolulu",
    "BRT": "America/Sao_Paulo",
    "ART": "America/Argentina/Buenos_Aires",
    "CLT": "America/Santiago",
    "SAST": "Africa/Johannesburg",
}


def resolve_timezone(abbr_or_name: str):
    """Resolve a user-typed timezone string to an IANA ZoneInfo, or None if unrecognized."""
    cleaned = abbr_or_name.strip()
    upper = cleaned.upper()
    if upper in TZ_ABBREVIATIONS:
        return ZoneInfo(TZ_ABBREVIATIONS[upper])
    # Also allow people to type a full IANA name directly, e.g. "Europe/London"
    try:
        return ZoneInfo(cleaned)
    except Exception:
        return None


# ---- KvK schedule ----
# Kingdom 3953's most recent (confirmed) KvK season started September 11, 2026
# at 12:00 UTC and ran ~52 days (through ~November 2, 2026), followed by a
# roughly month-long off-season before the next one begins. That gives a
# start-to-start cycle of about 82 days, which is what /kvk uses to project
# future start dates. Update KVK_LAST_START (and the lengths below, if
# leadership announces a different pattern) whenever a new KvK is confirmed
# in-game, so the projection keeps anchoring off the real, most recent date
# rather than drifting further from reality every cycle.
KVK_LAST_START = datetime(2026, 9, 11, 12, 0, tzinfo=ZoneInfo("UTC"))
KVK_SEASON_LENGTH_DAYS = 52   # Sep 11 -> Nov 2, based on the confirmed KvK Active period
KVK_OFF_SEASON_DAYS = 30      # typical gap observed before the next KvK begins
KVK_CYCLE_DAYS = KVK_SEASON_LENGTH_DAYS + KVK_OFF_SEASON_DAYS  # ~82 days start-to-start


def get_next_kvk(now: datetime = None) -> datetime:
    """Return the next upcoming KvK start date/time (UTC), based on the
    confirmed KVK_LAST_START plus the observed season+off-season cycle length.
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    cycle = timedelta(days=KVK_CYCLE_DAYS)
    next_kvk = KVK_LAST_START
    while next_kvk < now:
        next_kvk += cycle
    return next_kvk



TIMEAPI_BASE = "https://timeapi.io/api"


async def fetch_time_conversion(from_tz: str, dt: datetime, to_tz: str):
    """Call the free TimeAPI.io service to convert a datetime between IANA timezones.

    Returns a dict with the conversion result, or None if the API call fails
    (in which case the caller should fall back to local zoneinfo math).
    """
    payload = {
        "fromTimeZone": from_tz,
        "dateTime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "toTimeZone": to_tz,
        "dstAmbiguity": "",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TIMEAPI_BASE}/conversion/converttimezone",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                if resp.status != 200:
                    print(f"[timeapi] non-200 response: {resp.status}")
                    return None
                return await resp.json()
    except Exception as e:
        print(f"[timeapi] request failed: {e!r}")
        return None


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Public base URL of the donation website (this same service, with a web port
# exposed). /donate links point here. Set via the WEBSITE_BASE_URL env var
# once a public domain has been generated for this Railway service.
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "").rstrip("/")

_web_server_started = False


async def start_web_server():
    """Run the donation website (webapp.py) alongside the bot, in the same
    process/event loop, bound to the PORT Railway assigns this service."""
    port = int(os.getenv("PORT", "8080"))
    app = webapp.build_app()
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] donation site listening on 0.0.0.0:{port}")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.application_command:
        name = interaction.data.get("name") if interaction.data else "?"
        age = time.time() - interaction.created_at.timestamp()
        print(f"[interaction] {time.strftime('%H:%M:%S')} /{name} received (age={age:.2f}s) from {interaction.user}")
    elif interaction.type == discord.InteractionType.component:
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.startswith("escalate_ticket:"):
            await handle_escalate_button(interaction)


async def handle_escalate_button(interaction: discord.Interaction):
    """Handles clicks on the 'Still need help?' button attached to an AI
    ticket response. Matches purely on the custom_id string (no registered
    View needed), so it keeps working even across bot restarts, and
    regardless of whether the message was sent by this process's
    discord.py Client or posted via the website's raw REST calls."""
    try:
        await interaction.response.edit_message(view=None)
    except Exception as e:
        print(f"[ticket] failed to clear escalate button: {e!r}")
    try:
        await interaction.channel.send(
            f"{ai_helper.escalate_mention()} — {interaction.user.mention} still needs help "
            f"with this ticket, please take a look! 🙏"
        )
    except Exception as e:
        print(f"[ticket] failed to escalate after button click: {e!r}")


@bot.event
async def on_ready():
    global _web_server_started
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await stats_db.init_db()

    if not _web_server_started:
        _web_server_started = True
        bot.loop.create_task(start_web_server())

    try:
        if GUILD_IDS:
            total = 0
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                # Copy the locally-defined commands into each guild...
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                total += len(synced)
                print(f"Synced {len(synced)} slash command(s) to guild {gid}.")
            # ...then clear and push an empty global command list so any
            # previously-registered global commands stop showing up (avoids
            # duplicates; global removal can take up to an hour to propagate).
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            print(f"Synced {total} slash command(s) total across {len(GUILD_IDS)} guild(s).")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash command sync failed: {e}")


@bot.tree.command(name="ping", description="Check if the bot is online")
async def ping(interaction: discord.Interaction):
    t0 = time.monotonic()
    print(f"[ping] invoked, created_at age = {time.time() - interaction.created_at.timestamp():.2f}s")
    await interaction.response.send_message("🐺 Pong! WOLVES bot is online.", ephemeral=True)
    print(f"[ping] responded in {time.monotonic() - t0:.2f}s")


@bot.tree.command(name="list", description="Show every command this bot has")
async def list_commands(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📋 WOLVES Bot — Command List",
        description="Here's everything I can do:",
        color=RED,
    )

    # Startup sync moves command registrations into guild scope (and clears
    # the global list to avoid duplicates — see on_ready), so we must look
    # up commands in the guild this was actually invoked from, not the
    # (now-empty) global scope, or this list would always come back blank.
    if interaction.guild_id and interaction.guild_id in GUILD_IDS:
        lookup_guild = discord.Object(id=interaction.guild_id)
    elif GUILD_IDS:
        lookup_guild = discord.Object(id=GUILD_IDS[0])
    else:
        lookup_guild = None
    commands_sorted = sorted(bot.tree.get_commands(guild=lookup_guild), key=lambda c: c.name)
    for cmd in commands_sorted:
        params = " ".join(f"[{p.name}]" for p in cmd.parameters)
        usage = f"/{cmd.name} {params}".strip()
        embed.add_field(name=usage, value=cmd.description or "No description", inline=False)

    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • [param] = optional/required option for that command")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="shop", description="Show the WOLVES resource shop price list")
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛍️ WOLVES Resource Shop – Kingdom 3953",
        description="Need resources fast? Order below! **Minimum order: 100M per resource.** Payments via PayPal only.",
        color=RED,
    )
    for name, price in SHOP_ITEMS:
        embed.add_field(name=f"{PAYPAL_EMOJI} {name}", value=price, inline=True)
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • DM the seller to place your order!")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="donate", description="Get your personal link to log a donation to the alliance bank")
async def donate(interaction: discord.Interaction):
    if not WEBSITE_BASE_URL:
        await interaction.response.send_message(
            "⚠️ The donation website isn't configured yet (missing `WEBSITE_BASE_URL`). Contact the bot operator.",
            ephemeral=True,
        )
        return

    # Defer immediately: server_config.get_config() below can hit the Google
    # Sheets API, which can easily take longer than Discord's 3-second
    # response window, especially on a cold call. Deferring first (like the
    # old /donate did before its sheets.append_donation() call) buys up to
    # 15 minutes instead, avoiding the "Unknown interaction" / "The
    # application did not respond" error from an expired interaction token.
    await interaction.response.defer(ephemeral=True, thinking=True)

    config = await server_config.get_config(interaction.guild_id)
    token = donate_token.generate_token({
        "g": interaction.guild_id,
        "c": interaction.channel_id,
        "u": interaction.user.id,
        "n": interaction.user.display_name,
        "bank": config["bank_name"],
    })
    link = f"{WEBSITE_BASE_URL}/donate?token={token}"

    embed = discord.Embed(
        title="🏦 Donate to the Alliance Bank",
        description=(
            "Tap the button below to open your personal donation page. It walks you through "
            "exactly how to donate, lets you enter what you sent, and attach your proof screenshot.\n\n"
            "This link is just for you and expires in **30 minutes**."
        ),
        color=RED,
    )
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Alliance Bank Donations Tracker")

    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(label="Open Donation Form", style=discord.ButtonStyle.link, url=link, emoji="📝")
    )

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)




@bot.tree.command(name="config", description="(Admin) View or change this server's bot settings")
@app_commands.describe(
    donations_sheet="Google Sheet URL or ID that /donate should read/write (must be shared with the bot's service account as Editor)",
    donations_tab="Tab name in that sheet with the donation table (default: 'Donations')",
    alliance_members_tab="Tab name with the member list used by Donation Summary formulas (default: 'Alliance Members')",
    bank_name="Your alliance bank's exact in-game name, shown to members in proof-screenshot instructions",
    announce_channel="Channel where /announce should post for this server",
    tickets_channel="Channel where /ticket (and the donation site's 'Get Help' button) should create private support threads",
)
@app_commands.checks.has_permissions(administrator=True)
async def config(
    interaction: discord.Interaction,
    donations_sheet: str = None,
    donations_tab: str = None,
    alliance_members_tab: str = None,
    bank_name: str = None,
    announce_channel: discord.TextChannel = None,
    tickets_channel: Union[discord.TextChannel, discord.ForumChannel] = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    # No options given -> just show the server's current settings.
    if not any([donations_sheet, donations_tab, alliance_members_tab, bank_name, announce_channel, tickets_channel]):
        current = await server_config.get_config(interaction.guild_id)
        embed = discord.Embed(
            title="⚙️ Current Server Configuration",
            color=RED,
        )
        embed.add_field(
            name="Donations Sheet ID", value=f"`{current['donations_sheet_id']}`", inline=False
        )
        embed.add_field(name="Donations Tab", value=current["donations_tab_name"], inline=True)
        embed.add_field(
            name="Alliance Members Tab", value=current["alliance_members_tab_name"], inline=True
        )
        embed.add_field(name="Bank Name", value=current["bank_name"], inline=True)
        embed.add_field(
            name="Announce Channel",
            value=f"<#{current['announce_channel_id']}>" if current["announce_channel_id"] else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Tickets Channel",
            value=f"<#{current['tickets_channel_id']}>" if current["tickets_channel_id"] else "Not set",
            inline=True,
        )
        embed.set_footer(text="Run /config with an option to change a setting, e.g. /config donations_sheet:<url>")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    updates = {}

    if donations_sheet:
        # Accept either a raw Sheet ID or a full URL and extract the ID.
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", donations_sheet)
        sheet_id = match.group(1) if match else donations_sheet.strip()

        ok, result = await server_config.verify_sheet_access(sheet_id)
        if not ok:
            service_email = sheets.get_service_account_email()
            await interaction.followup.send(
                f"⚠️ Couldn't open that spreadsheet: {result}\n\n"
                f"Make sure you've shared it with **`{service_email}`** as an **Editor** "
                f"(Share button → paste that email → Editor → Send), then try again.",
                ephemeral=True,
            )
            return
        updates["donations_sheet_id"] = sheet_id

    if donations_tab:
        updates["donations_tab_name"] = donations_tab
    if alliance_members_tab:
        updates["alliance_members_tab_name"] = alliance_members_tab
    if bank_name:
        updates["bank_name"] = bank_name
    if announce_channel:
        updates["announce_channel_id"] = announce_channel.id
    if tickets_channel:
        updates["tickets_channel_id"] = tickets_channel.id

    try:
        await server_config.set_config(
            guild_id=interaction.guild_id,
            guild_name=interaction.guild.name if interaction.guild else "Unknown",
            configured_by=str(interaction.user),
            **updates,
        )
    except Exception as e:
        print(f"[config] failed to save: {e!r}")
        await interaction.followup.send(f"⚠️ Failed to save configuration: {e}", ephemeral=True)
        return

    changed = ", ".join(updates.keys())
    embed = discord.Embed(
        title="✅ Configuration Updated",
        description=f"Updated: **{changed}**",
        color=RED,
    )
    embed.set_footer(text="Run /config with no options to see all current settings.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="ticket", description="Open a private support ticket for an issue (e.g. /donate or the donation site isn't working)")
@app_commands.describe(issue="Briefly describe the problem you're having")
async def ticket(interaction: discord.Interaction, issue: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    config = await server_config.get_config(interaction.guild_id)
    tickets_channel_id = config.get("tickets_channel_id")

    if not tickets_channel_id:
        await interaction.followup.send(
            "⚠️ This server hasn't set up a tickets channel yet. An admin needs to run "
            "`/config tickets_channel:#your-tickets-channel` first.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(tickets_channel_id) if interaction.guild else None
    if channel is None:
        await interaction.followup.send(
            "⚠️ The configured tickets channel no longer exists. An admin needs to run "
            "`/config tickets_channel:#your-tickets-channel` again to fix it.",
            ephemeral=True,
        )
        return

    thread_name = f"ticket-{interaction.user.name}"[:100]
    embed = discord.Embed(
        title="🎫 New Support Ticket",
        description=issue,
        color=RED,
    )
    embed.add_field(name="Opened by", value=interaction.user.mention, inline=True)
    embed.add_field(
        name="From channel",
        value=interaction.channel.mention if interaction.channel else "Unknown",
        inline=True,
    )
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Support Tickets")

    try:
        if isinstance(channel, discord.ForumChannel):
            # Forum channels don't support private threads — each ticket is a
            # normal forum post, with the embed as its starter message.
            thread_with_message = await channel.create_thread(
                name=thread_name,
                embed=embed,
                reason=f"Support ticket opened by {interaction.user}",
            )
            thread = thread_with_message.thread
        else:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                reason=f"Support ticket opened by {interaction.user}",
            )
            await thread.send(embed=embed)
        await thread.add_user(interaction.user)
    except discord.Forbidden:
        print(f"[ticket] missing permissions to create a ticket in #{channel}")
        await interaction.followup.send(
            "⚠️ I don't have permission to create tickets in the tickets channel. "
            "An admin needs to grant me **Create Posts**/**Create Private Threads** and "
            "**Manage Threads** there.",
            ephemeral=True,
        )
        return
    except Exception as e:
        print(f"[ticket] failed to create thread: {e!r}")
        await interaction.followup.send(f"⚠️ Failed to open a ticket: {e}", ephemeral=True)
        return

    await post_ai_ticket_response(thread, issue)

    await interaction.followup.send(f"🎫 Ticket created: {thread.mention}", ephemeral=True)


async def post_ai_ticket_response(thread, issue: str):
    """Post an AI-generated first response in a freshly-created ticket
    thread/forum post, and immediately loop in the human helper if the AI
    isn't confident it solved things. Shared by /ticket and the website's
    'Get Help' flow (via webapp.py's own REST equivalent of this)."""
    ai_result = await ai_helper.get_ai_response(issue)

    ai_embed = discord.Embed(
        title="🤖 Automated Suggestion",
        description=ai_result["reply"],
        color=RED,
    )

    if ai_result["confident"]:
        ai_embed.set_footer(text="Didn't fix it? Click below to bring in a human.")
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="🙋 Still need help?",
                style=discord.ButtonStyle.secondary,
                custom_id=f"escalate_ticket:{thread.id}",
            )
        )
        await thread.send(embed=ai_embed, view=view)
    else:
        ai_embed.set_footer(text="Looping in a human for this one.")
        await thread.send(embed=ai_embed)
        await thread.send(
            f"{ai_helper.escalate_mention()} — this ticket needs a human look, please take a peek! 🙏"
        )



def fmt_num(n: int) -> str:
    """Format a number with commas, and a compact M/B suffix for readability."""
    if n >= 1_000_000_000:
        return f"{n:,} ({n/1_000_000_000:.2f}B)"
    if n >= 1_000_000:
        return f"{n:,} ({n/1_000_000:.2f}M)"
    return f"{n:,}"


def fmt_delta(n: int) -> str:
    sign = "+" if n >= 0 else "-"
    n_abs = abs(n)
    if n_abs >= 1_000_000_000:
        return f"{sign}{n_abs/1_000_000_000:.2f}B"
    if n_abs >= 1_000_000:
        return f"{sign}{n_abs/1_000_000:.2f}M"
    return f"{sign}{n_abs:,}"


# ---- Stat tracking commands (StatsMaster-style) ----

@bot.tree.command(name="submitstats", description="Submit your current Power/Kills/Deaths for tracking")
@app_commands.describe(
    power="Current Power",
    kills="Total Kills (kill points)",
    deaths="Total Deaths",
    label="Optional checkpoint label, e.g. 'KvK Start' or 'Week 1'",
)
async def submitstats(interaction: discord.Interaction, power: int, kills: int, deaths: int, label: str = None):
    t0 = time.monotonic()
    print(f"[submitstats] {time.strftime('%H:%M:%S')} invoked, created_at age = {time.time() - interaction.created_at.timestamp():.2f}s")
    await interaction.response.defer(thinking=True)
    print(f"[submitstats] defer() completed in {time.monotonic() - t0:.2f}s")
    await stats_db.add_snapshot(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        display_name=interaction.user.display_name,
        power=power,
        kills=kills,
        deaths=deaths,
        label=label,
        submitted_by=interaction.user.id,
    )
    embed = discord.Embed(
        title="📊 Stats Recorded",
        description=f"Snapshot saved for **{interaction.user.display_name}**"
        + (f" (checkpoint: `{label}`)" if label else ""),
        color=RED,
    )
    embed.add_field(name="Power", value=fmt_num(power), inline=True)
    embed.add_field(name="Kills", value=fmt_num(kills), inline=True)
    embed.add_field(name="Deaths", value=fmt_num(deaths), inline=True)
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Stat Tracker")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="profile", description="View a governor's latest stats and growth since their first submission")
@app_commands.describe(member="Governor to look up (defaults to you)")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    t0 = time.monotonic()
    print(f"[profile] invoked, created_at age = {time.time() - interaction.created_at.timestamp():.2f}s")
    await interaction.response.defer(thinking=True)
    print(f"[profile] defer() completed in {time.monotonic() - t0:.2f}s")
    target = member or interaction.user
    latest = await stats_db.get_latest(interaction.guild_id, target.id)
    if not latest:
        await interaction.followup.send(
            f"⚠️ No stats found for **{target.display_name}**. Use `/submitstats` first.", ephemeral=True
        )
        return

    first = await stats_db.get_first(interaction.guild_id, target.id)

    embed = discord.Embed(
        title=f"🐺 Governor Profile – {latest['display_name']}",
        color=RED,
    )
    embed.add_field(name="Power", value=fmt_num(latest["power"]), inline=True)
    embed.add_field(name="Kills", value=fmt_num(latest["kills"]), inline=True)
    embed.add_field(name="Deaths", value=fmt_num(latest["deaths"]), inline=True)

    if first and first["id"] != latest["id"]:
        embed.add_field(name="Power Growth", value=fmt_delta(latest["power"] - first["power"]), inline=True)
        embed.add_field(name="Kills Growth", value=fmt_delta(latest["kills"] - first["kills"]), inline=True)
        embed.add_field(name="Deaths Growth", value=fmt_delta(latest["deaths"] - first["deaths"]), inline=True)
        first_date = datetime.fromtimestamp(first["created_at"]).strftime("%Y-%m-%d")
        embed.add_field(name="Tracking Since", value=first_date, inline=False)

    updated = datetime.fromtimestamp(latest["created_at"]).strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"WOLVES 🐺 | BRAVIA 3953 • Last updated {updated}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="compare", description="Compare two governors' latest stats")
@app_commands.describe(member1="First governor", member2="Second governor")
async def compare(interaction: discord.Interaction, member1: discord.Member, member2: discord.Member):
    t0 = time.monotonic()
    print(f"[compare] invoked, created_at age = {time.time() - interaction.created_at.timestamp():.2f}s")
    await interaction.response.defer(thinking=True)
    print(f"[compare] defer() completed in {time.monotonic() - t0:.2f}s")
    s1 = await stats_db.get_latest(interaction.guild_id, member1.id)
    s2 = await stats_db.get_latest(interaction.guild_id, member2.id)

    if not s1 or not s2:
        missing = member1.display_name if not s1 else member2.display_name
        await interaction.followup.send(
            f"⚠️ No stats found for **{missing}**. They need to use `/submitstats` first.", ephemeral=True
        )
        return

    embed = discord.Embed(title="⚔️ Governor Comparison", color=RED)
    embed.add_field(name=s1["display_name"], value=(
        f"Power: {fmt_num(s1['power'])}\nKills: {fmt_num(s1['kills'])}\nDeaths: {fmt_num(s1['deaths'])}"
    ), inline=True)
    embed.add_field(name=s2["display_name"], value=(
        f"Power: {fmt_num(s2['power'])}\nKills: {fmt_num(s2['kills'])}\nDeaths: {fmt_num(s2['deaths'])}"
    ), inline=True)
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Stat Tracker")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="rank", description="Show the alliance leaderboard for a stat")
@app_commands.describe(stat="Which stat to rank by")
@app_commands.choices(stat=[
    app_commands.Choice(name="Power", value="power"),
    app_commands.Choice(name="Kills", value="kills"),
    app_commands.Choice(name="Deaths", value="deaths"),
])
async def rank(interaction: discord.Interaction, stat: app_commands.Choice[str]):
    await interaction.response.defer(thinking=True)
    board = await stats_db.get_leaderboard(interaction.guild_id, stat.value, limit=10)

    if not board:
        await interaction.followup.send(
            "⚠️ No stats submitted yet. Members can use `/submitstats` to get started.", ephemeral=True
        )
        return

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(board):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{medal} **{row['display_name']}** — {fmt_num(row[stat.value])}")

    embed = discord.Embed(
        title=f"🏆 WOLVES Leaderboard – {stat.name}",
        description="\n".join(lines),
        color=RED,
    )
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Based on each governor's latest /submitstats")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="kvkgains", description="Show KvK point gains between two labeled checkpoints for a governor")
@app_commands.describe(
    member="Governor to check (defaults to you)",
    start_label="Label used at KvK start, e.g. 'KvK Start'",
    end_label="Label used at KvK end, e.g. 'KvK End' (defaults to latest submission)",
)
async def kvkgains(interaction: discord.Interaction, start_label: str, member: discord.Member = None, end_label: str = None):
    await interaction.response.defer(thinking=True)
    target = member or interaction.user

    start = await stats_db.get_by_label(interaction.guild_id, target.id, start_label)
    if not start:
        await interaction.followup.send(
            f"⚠️ No snapshot found for **{target.display_name}** with label `{start_label}`.", ephemeral=True
        )
        return

    if end_label:
        end = await stats_db.get_by_label(interaction.guild_id, target.id, end_label)
        if not end:
            await interaction.followup.send(
                f"⚠️ No snapshot found for **{target.display_name}** with label `{end_label}`.", ephemeral=True
            )
            return
    else:
        end = await stats_db.get_latest(interaction.guild_id, target.id)

    kills_gain = end["kills"] - start["kills"]
    deaths_gain = end["deaths"] - start["deaths"]
    power_change = end["power"] - start["power"]

    embed = discord.Embed(
        title=f"⚔️ KvK Gains – {target.display_name}",
        description=f"Comparing `{start_label}` → `{end_label or 'latest'}`",
        color=RED,
    )
    embed.add_field(name="Kills Gained", value=fmt_delta(kills_gain), inline=True)
    embed.add_field(name="Deaths Gained", value=fmt_delta(deaths_gain), inline=True)
    embed.add_field(name="Power Change", value=fmt_delta(power_change), inline=True)
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Stat Tracker")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="announce", description="(Admin) Post an alliance announcement")
@app_commands.describe(title="Announcement title", message="The announcement text")
@app_commands.checks.has_permissions(manage_messages=True)
async def announce(interaction: discord.Interaction, title: str, message: str):
    # Acknowledge immediately so Discord doesn't time out (3s limit) while we fetch the channel/send.
    await interaction.response.defer(ephemeral=True, thinking=True)

    # Post into the announcement channel that belongs to whichever server this
    # command was run in: check this server's /config setting first, falling
    # back to the legacy hardcoded map (for the original WOLVES servers), and
    # finally the overall default.
    config = await server_config.get_config(interaction.guild_id)
    target_channel_id = (
        config.get("announce_channel_id")
        or ANNOUNCE_CHANNELS.get(interaction.guild_id, ANNOUNCE_CHANNEL_ID)
    )

    channel = bot.get_channel(target_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(target_channel_id)
        except discord.HTTPException as e:
            print(f"[announce] fetch_channel failed: {e}")
            channel = None

    if channel is None:
        await interaction.followup.send(
            "⚠️ Could not find the announcement channel for this server. "
            "An admin can set one with `/config announce_channel:#channel-name`.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title=title, description=message, color=RED)
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Alliance Announcement")
    banner = f"```ansi\n\u001b[1;31m📣 {title}\u001b[0m\n```"
    content = f"@everyone\n{banner}"

    try:
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "🚫 I don't have permission to post in the announcement channel.", ephemeral=True
        )
        return
    except discord.HTTPException as e:
        print(f"[announce] channel.send failed: {e}")
        await interaction.followup.send(f"⚠️ Failed to send announcement: {e}", ephemeral=True)
        return

    if interaction.channel_id == target_channel_id:
        await interaction.followup.send("✅ Announcement posted above.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"✅ Announcement posted in <#{target_channel_id}>.", ephemeral=True
        )



@bot.tree.command(name="kvk", description="See when the next KvK is in your own timezone")
@app_commands.describe(timezone="Your timezone abbreviation or IANA name, e.g. BST, EST, PST, SGT, IST, Europe/London")
async def kvk(interaction: discord.Interaction, timezone: str):
    t0 = time.monotonic()
    print(f"[kvk] invoked, created_at age = {time.time() - interaction.created_at.timestamp():.2f}s")
    await interaction.response.defer(thinking=True)
    print(f"[kvk] defer() completed in {time.monotonic() - t0:.2f}s")

    tz_name = TZ_ABBREVIATIONS.get(timezone.strip().upper(), timezone.strip())
    tz = resolve_timezone(timezone)
    if tz is None:
        await interaction.followup.send(
            f"⚠️ I don't recognize the timezone `{timezone}`. Try something like `BST`, `EST`, `PST`, `SGT`, "
            f"`IST`, `AEST`, `CET`, or a full IANA zone name like `Europe/London`.",
            ephemeral=True,
        )
        return

    next_kvk_utc = get_next_kvk()

    # Try the live TimeAPI.io conversion first; fall back to local zoneinfo math
    # if the API is unreachable, so the command never fully breaks.
    api_result = await fetch_time_conversion("UTC", next_kvk_utc.replace(tzinfo=None), tz_name)

    if api_result and "conversionResult" in api_result:
        cr = api_result["conversionResult"]
        local_dt = datetime(cr["year"], cr["month"], cr["day"], cr["hour"], cr["minute"])
        local_time_str = local_dt.strftime("%I:%M %p").lstrip("0")
        local_day_str = local_dt.strftime("%A, %B %d, %Y")
        source = "🌐 via TimeAPI.io"
    else:
        converted = next_kvk_utc.astimezone(tz)
        local_time_str = converted.strftime("%I:%M %p").lstrip("0")
        local_day_str = converted.strftime("%A, %B %d, %Y")
        source = "🧮 calculated locally (API unavailable)"

    utc_time_str = next_kvk_utc.strftime("%I:%M %p").lstrip("0")
    utc_day_str = next_kvk_utc.strftime("%A, %B %d, %Y")

    offset = next_kvk_utc.astimezone(tz).utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    offset_str = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")

    embed = discord.Embed(
        title="⚔️ Next KvK – Kingdom 3953",
        description=f"Timezone: **{timezone.upper()}** ({offset_str})\n{source}",
        color=RED,
    )
    embed.add_field(
        name=f"📍 Your Time ({timezone.upper()})",
        value=f"{local_day_str}\n**{local_time_str}**",
        inline=True,
    )
    embed.add_field(
        name="🌐 Server Time (UTC)",
        value=f"{utc_day_str}\n**{utc_time_str} UTC**",
        inline=True,
    )
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Be online, be ready, be Wolves!")
    await interaction.followup.send(embed=embed)


async def _send_error(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException as e:
        print(f"[error-handler] failed to notify user: {e}")


@announce.error
async def announce_error(interaction: discord.Interaction, error):
    print(f"[announce] error: {error!r}")
    if isinstance(error, app_commands.MissingPermissions):
        await _send_error(interaction, "🚫 You need `Manage Messages` permission to use this command.")
    else:
        await _send_error(interaction, f"⚠️ Error: {error}")


@config.error
async def config_error(interaction: discord.Interaction, error):
    print(f"[config] error: {error!r}")
    if isinstance(error, app_commands.MissingPermissions):
        await _send_error(interaction, "🚫 You need `Administrator` permission to use this command.")
    else:
        await _send_error(interaction, f"⚠️ Error: {error}")


@submitstats.error
@profile.error
@compare.error
@rank.error
@kvkgains.error
@kvk.error
@donate.error
async def stats_command_error(interaction: discord.Interaction, error):
    print(f"[stats-command] error: {error!r}")
    await _send_error(interaction, f"⚠️ Error: {error}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
