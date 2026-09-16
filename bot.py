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
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

import stats_db

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: set for instant slash command sync in one server

RED = 0xFF0000
PAYPAL_EMOJI = "<:PayPalLOGO:1457553629547593933>"  # update with your server's emoji id if different
ANNOUNCE_CHANNEL_ID = 1514659627936120992  # /announce always posts here

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


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


class TimezoneSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"kvk_tz:(?P<epoch>[0-9]+)"):
    """Dropdown letting a member pick their own timezone to see the converted KvK time.

    Built as a DynamicItem so it keeps working even after the bot restarts —
    the KvK moment (as a UTC epoch timestamp) is encoded directly into the
    component's custom_id instead of being held only in memory.
    """

    def __init__(self, epoch: int):
        self.epoch = epoch
        options = [
            discord.SelectOption(label=label, value=tz_name)
            for label, tz_name in TIMEZONE_CHOICES
        ]
        select = discord.ui.Select(
            placeholder="🌐 Select your timezone to see your local KvK time...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"kvk_tz:{epoch}",
        )
        super().__init__(select)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["epoch"]))

    async def callback(self, interaction: discord.Interaction):
        tz_name = self.item.values[0]
        label = next((l for l, v in TIMEZONE_CHOICES if v == tz_name), tz_name)
        base_utc = datetime.fromtimestamp(self.epoch, tz=ZoneInfo("UTC"))
        converted = base_utc.astimezone(ZoneInfo(tz_name))

        local_time_str = converted.strftime("%I:%M %p").lstrip("0")
        local_day_str = converted.strftime("%A, %B %d, %Y")
        utc_time_str = base_utc.strftime("%I:%M %p").lstrip("0")
        utc_day_str = base_utc.strftime("%A, %B %d, %Y")

        # Offset display, e.g. "UTC+8" or "UTC-5"
        offset = converted.utcoffset()
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        h, m = divmod(abs(total_minutes), 60)
        offset_str = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")

        embed = discord.Embed(
            title="🕒 Your Local KvK Time",
            description=f"Showing time for **{label}** ({offset_str})",
            color=RED,
        )
        embed.add_field(
            name=f"📍 Your Time ({label})",
            value=f"{local_day_str}\n**{local_time_str}**",
            inline=True,
        )
        embed.add_field(
            name="🌐 Server Time (UTC)",
            value=f"{utc_day_str}\n**{utc_time_str} UTC**",
            inline=True,
        )
        embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TimezoneView(discord.ui.View):
    def __init__(self, epoch: int):
        super().__init__(timeout=None)  # persistent across restarts
        self.add_item(TimezoneSelect(epoch))


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.application_command:
        name = interaction.data.get("name") if interaction.data else "?"
        age = time.time() - interaction.created_at.timestamp()
        print(f"[interaction] {time.strftime('%H:%M:%S')} /{name} received (age={age:.2f}s) from {interaction.user}")


_dynamic_items_registered = False


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await stats_db.init_db()

    global _dynamic_items_registered
    if not _dynamic_items_registered:
        bot.add_dynamic_items(TimezoneSelect)
        _dynamic_items_registered = True
        print("Registered persistent KvK timezone dropdown.")

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            # Copy the locally-defined commands into the guild first...
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            # ...then clear and push an empty global command list so any
            # previously-registered global commands stop showing up (avoids
            # duplicates; global removal can take up to an hour to propagate).
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
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

    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
        except discord.HTTPException as e:
            print(f"[announce] fetch_channel failed: {e}")
            channel = None

    if channel is None:
        await interaction.followup.send(
            "⚠️ Could not find the announcement channel. Check ANNOUNCE_CHANNEL_ID and bot permissions.",
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

    if interaction.channel_id == ANNOUNCE_CHANNEL_ID:
        await interaction.followup.send("✅ Announcement posted above.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"✅ Announcement posted in <#{ANNOUNCE_CHANNEL_ID}>.", ephemeral=True
        )



@bot.tree.command(name="kvk", description="(Admin) Announce KvK date & time with a timezone picker dropdown")
@app_commands.describe(
    date="Date, e.g. 'Saturday, September 19, 2026'",
    time_utc="Time in 24h UTC, e.g. '12:00'",
    year="Year the date falls in (defaults to current year)",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def kvk(interaction: discord.Interaction, date: str, time_utc: str, year: int = None):
    await interaction.response.defer(thinking=True)

    try:
        hour, minute = map(int, time_utc.split(":"))
    except ValueError:
        await interaction.followup.send(
            "⚠️ Time must be in HH:MM 24h format, e.g. 12:00", ephemeral=True
        )
        return

    now = datetime.utcnow()
    target_year = year or now.year
    # Anchor date is only used for accurate DST-aware conversions; the "date" field
    # shown to users is whatever text they typed in the `date` parameter.
    base_utc = datetime(target_year, now.month, now.day, hour, minute, tzinfo=ZoneInfo("UTC"))

    embed = discord.Embed(
        title="⚔️ KvK Announcement – Kingdom 3953",
        description="Get ready Wolves! **KvK is coming!**\n\nUse the dropdown below to see the time in *your* timezone. 👇",
        color=RED,
    )
    embed.add_field(name="📅 Date", value=date, inline=False)
    embed.add_field(name="🌐 Time (UTC)", value=f"{time_utc} UTC", inline=False)
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953 • Be online, be ready, be Wolves!")

    content = f"```ansi\n\u001b[1;31m⚔️ KvK Announcement – Kingdom 3953\u001b[0m\n```"
    epoch = int(base_utc.timestamp())
    view = TimezoneView(epoch)

    # Remember this as "the" upcoming KvK event for this server, so members can
    # just run /mytime with their timezone abbreviation and get an instant answer.
    await stats_db.set_kvk_event(interaction.guild_id, date, epoch)

    await interaction.followup.send(content=content, embed=embed, view=view)


@bot.tree.command(name="mytime", description="See the KvK time in your own timezone — just type your zone (e.g. BST, EST, SGT)")
@app_commands.describe(timezone="Your timezone abbreviation, e.g. BST, EST, PST, SGT, IST, AEST")
async def mytime(interaction: discord.Interaction, timezone: str):
    t0 = time.monotonic()
    print(f"[mytime] invoked, created_at age = {time.time() - interaction.created_at.timestamp():.2f}s")
    await interaction.response.defer(ephemeral=True, thinking=True)
    print(f"[mytime] defer() completed in {time.monotonic() - t0:.2f}s")

    event = await stats_db.get_kvk_event(interaction.guild_id)
    if not event:
        await interaction.followup.send(
            "⚠️ No KvK event has been announced yet. Ask an officer to run `/kvk` first.", ephemeral=True
        )
        return

    tz = resolve_timezone(timezone)
    if tz is None:
        await interaction.followup.send(
            f"⚠️ I don't recognize the timezone `{timezone}`. Try something like `BST`, `EST`, `PST`, `SGT`, "
            f"`IST`, `AEST`, `CET`, or a full zone name like `Europe/London`.",
            ephemeral=True,
        )
        return

    base_utc = datetime.fromtimestamp(event["epoch"], tz=ZoneInfo("UTC"))
    converted = base_utc.astimezone(tz)

    local_time_str = converted.strftime("%I:%M %p").lstrip("0")
    local_day_str = converted.strftime("%A, %B %d, %Y")
    utc_time_str = base_utc.strftime("%I:%M %p").lstrip("0")
    utc_day_str = base_utc.strftime("%A, %B %d, %Y")

    offset = converted.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    offset_str = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")

    embed = discord.Embed(
        title="🕒 Your KvK Time",
        description=f"Timezone: **{timezone.upper()}** ({offset_str})",
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
    embed.set_footer(text="WOLVES 🐺 | BRAVIA 3953")
    await interaction.followup.send(embed=embed, ephemeral=True)


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


@kvk.error
async def kvk_error(interaction: discord.Interaction, error):
    print(f"[kvk] error: {error!r}")
    if isinstance(error, app_commands.MissingPermissions):
        await _send_error(interaction, "🚫 You need `Manage Messages` permission to use this command.")
    else:
        await _send_error(interaction, f"⚠️ Error: {error}")


@submitstats.error
@profile.error
@compare.error
@rank.error
@kvkgains.error
@mytime.error
async def stats_command_error(interaction: discord.Interaction, error):
    print(f"[stats-command] error: {error!r}")
    await _send_error(interaction, f"⚠️ Error: {error}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
