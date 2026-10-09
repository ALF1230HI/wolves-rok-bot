"""Standalone preview/prototype of the /donate web form.

This is NOT wired into the live bot yet. It's a self-contained aiohttp web
app you can run on its own to see and test the full flow:

  1. /donate command (in the real bot, eventually) would send the user a
     personal link to this site instead of making them type amounts into
     the slash command itself.
  2. That link opens here: a full guide on how to donate, then a form to
     pick resource amounts and upload the proof screenshot.
  3. Submitting writes a row to the alliance's donation spreadsheet (same
     Google Sheet /donate already uses) and posts the usual confirmation
     embed back into the Discord channel the link was generated from.

DEMO MODE
---------
Visiting the site with no token (e.g. just opening the root URL) auto-
generates a fake "Test Donor" link and marks the session as a demo: no
real spreadsheet write and no real Discord message happen, so you can
click through the entire flow risk-free. A "TEST MODE" banner makes this
obvious at every step. Once you're happy with it, say so and this gets
wired into bot.py for real (generating one real link per /donate run) and
deployed.

Run it with:  python webapp.py
"""

import os
import html
import asyncio

from aiohttp import web, ClientSession, FormData

import donate_token
import sheets
import server_config

RED_HEX = "#FF0000"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_API = "https://discord.com/api/v10"


def fmt_num(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n:,} ({n/1_000_000_000:.2f}B)"
    if n >= 1_000_000:
        return f"{n:,} ({n/1_000_000:.2f}M)"
    return f"{n:,}"


PAGE_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    background: radial-gradient(circle at top, #1b0e0e 0%, #0b0505 60%, #050202 100%);
    color: #f1e9e9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
    display: flex; justify-content: center;
  }}
  .wrap {{ width: 100%; max-width: 560px; padding: 28px 20px 60px; }}
  .banner {{
    background: #3a1d00; border: 1px solid #ffb020; color: #ffd98a;
    padding: 10px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 18px;
    text-align: center;
  }}
  .card {{
    background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01));
    border: 1px solid rgba(255,70,70,0.35);
    border-radius: 16px;
    padding: 24px 22px;
    box-shadow: 0 10px 40px rgba(255,0,0,0.08);
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; color: #ffffff; }}
  h1 .wolf {{ margin-right: 6px; }}
  .sub {{ color: #cf9f9f; font-size: 13px; margin: 0 0 20px; }}
  h2 {{ font-size: 15px; color: {red}; text-transform: uppercase; letter-spacing: 0.06em; margin: 26px 0 10px; }}
  ol {{ margin: 0; padding-left: 20px; color: #eadcdc; font-size: 14px; line-height: 1.7; }}
  ol li b {{ color: #fff; }}
  .note {{ font-size: 12px; color: #a98484; margin-top: 14px; line-height: 1.5; }}
  label {{ display: block; font-size: 13px; color: #e9c9c9; margin: 14px 0 6px; font-weight: 600; }}
  input[type=number], input[type=text] {{
    width: 100%; padding: 11px 12px; border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.15); background: #180d0d; color: #fff; font-size: 15px;
  }}
  input[type=number]:focus, input[type=text]:focus {{ outline: none; border-color: {red}; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .file-drop {{
    margin-top: 6px; border: 1.5px dashed rgba(255,255,255,0.25); border-radius: 12px;
    padding: 18px; text-align: center; font-size: 13px; color: #cfa; color: #d8b9b9;
    background: rgba(255,255,255,0.02); cursor: pointer;
  }}
  .file-drop input {{ display: block; margin: 10px auto 0; color: #ddd; font-size: 13px; }}
  button {{
    margin-top: 22px; width: 100%; padding: 14px; border: none; border-radius: 12px;
    background: {red}; color: #fff; font-size: 15px; font-weight: 700; letter-spacing: 0.02em;
    cursor: pointer;
  }}
  button:hover {{ filter: brightness(1.08); }}
  .footer {{ text-align: center; font-size: 11px; color: #7a5656; margin-top: 18px; }}
  .pill {{ display: inline-block; background: rgba(255,0,0,0.12); border: 1px solid rgba(255,0,0,0.3);
    color: #ff9a9a; font-size: 12px; padding: 3px 10px; border-radius: 999px; margin-bottom: 14px; }}
  .result {{ text-align: center; padding: 10px 0 4px; }}
  .result .big {{ font-size: 42px; margin-bottom: 8px; }}
  .amounts {{ list-style: none; padding: 0; margin: 16px 0; }}
  .amounts li {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 14px; }}
  a.btn-link {{ display: block; text-align:center; margin-top:18px; padding: 13px; border-radius: 12px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15); color: #fff; text-decoration: none; font-size: 14px; }}
  .error {{ color: #ff8a8a; font-size: 13px; margin-top: 10px; }}
</style>
</head>
<body>
<div class="wrap">
{banner}
<div class="card">
{body}
</div>
<div class="footer">WOLVES 🐺 | BRAVIA 3953 &bull; Alliance Bank Donations</div>
</div>
</body>
</html>
"""


def render_page(title: str, body: str, demo: bool) -> str:
    banner = (
        '<div class="banner">🧪 TEST MODE — nothing on this page writes to the real '
        "spreadsheet or posts to Discord. Safe to click around.</div>"
        if demo
        else ""
    )
    return PAGE_SHELL.format(title=html.escape(title), body=body, banner=banner, red=RED_HEX)


def render_form(payload: dict, demo: bool, token: str, error: str = None) -> str:
    display_name = html.escape(payload.get("n", "Donor"))
    bank_name = html.escape(payload.get("bank", "the alliance bank"))
    error_html = f'<div class="error">⚠️ {html.escape(error)}</div>' if error else ""
    body = f"""
      <span class="pill">Donating as {display_name}</span>
      <h1><span class="wolf">🐺</span>Alliance Bank Donation</h1>
      <p class="sub">Follow the steps below, then log exactly what you sent.</p>

      <h2>How it works</h2>
      <ol>
        <li>In-game, go to <b>Alliance &rarr; Assistance</b> and send the resources
            you want to donate to <b>{bank_name}</b>.</li>
        <li>Once it's sent, open <b>Alliance &rarr; Assistance &rarr; Assistance Report</b>
            and screenshot the entry showing what you just sent.</li>
        <li>Enter the exact amounts below, attach that screenshot, and submit.</li>
        <li>Your donation is logged to the alliance's tracker and a confirmation is
            posted back in the Discord channel you ran <code>/donate</code> from.</li>
      </ol>

      <form method="post" action="/donate/submit" enctype="multipart/form-data">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <h2>Amounts donated</h2>
        <div class="grid2">
          <div>
            <label>🌾 Food</label>
            <input type="number" name="food" min="0" value="0" inputmode="numeric">
          </div>
          <div>
            <label>🪵 Wood</label>
            <input type="number" name="wood" min="0" value="0" inputmode="numeric">
          </div>
          <div>
            <label>🪨 Stone</label>
            <input type="number" name="stone" min="0" value="0" inputmode="numeric">
          </div>
          <div>
            <label>🪙 Gold</label>
            <input type="number" name="gold" min="0" value="0" inputmode="numeric">
          </div>
        </div>

        <h2>Proof screenshot</h2>
        <label class="file-drop">
          📸 Tap to choose your Assistance Report screenshot
          <input type="file" name="proof" accept="image/*" required>
        </label>

        {error_html}
        <button type="submit">Submit Donation</button>
      </form>
      <p class="note">This link is personal to you and expires 30 minutes after
      <code>/donate</code> was run. If it stops working, just run <code>/donate</code>
      again in Discord for a fresh one.</p>
      <a class="btn-link" href="/ticket?token={html.escape(token)}">🎫 Something not working? Get help</a>
    """
    return render_page("Donate — WOLVES Alliance Bank", body, demo)


def render_success(payload: dict, donated: dict, demo: bool) -> str:
    display_name = html.escape(payload.get("n", "Donor"))
    rows = "".join(
        f'<li><span>{name}</span><span>{fmt_num(amount)}</span></li>'
        for name, amount in donated.items()
    )
    note = (
        "<p class='note'>Test mode: nothing was actually written anywhere — this is "
        "exactly what the real version would log and post.</p>"
        if demo
        else "<p class='note'>Check the Discord channel you ran /donate from for the confirmation post.</p>"
    )
    body = f"""
      <div class="result">
        <div class="big">✅</div>
        <h1>Donation Recorded</h1>
        <p class="sub">Thank you, {display_name}!</p>
      </div>
      <ul class="amounts">{rows}</ul>
      {note}
    """
    return render_page("Donation Recorded", body, demo)


def render_error(message: str, token: str = None) -> str:
    help_link = (
        f'<a class="btn-link" href="/ticket?token={html.escape(token)}">🎫 Still stuck? Get help</a>'
        if token
        else ""
    )
    body = f"""
      <div class="result">
        <div class="big">⚠️</div>
        <h1>Link expired or invalid</h1>
        <p class="sub">{html.escape(message)}</p>
      </div>
      <p class="note">Go back to Discord and run <code>/donate</code> again to get a fresh link.</p>
      {help_link}
    """
    return render_page("Link expired", body, demo=False)


def render_ticket_form(payload: dict, demo: bool, token: str, error: str = None) -> str:
    display_name = html.escape(payload.get("n", "Donor"))
    error_html = f'<div class="error">⚠️ {html.escape(error)}</div>' if error else ""
    body = f"""
      <span class="pill">Requesting help as {display_name}</span>
      <h1><span class="wolf">🎫</span>Get Help</h1>
      <p class="sub">Tell us what's going wrong and a private ticket will be opened
      with an admin — right here in Discord.</p>

      <form method="post" action="/ticket/submit" enctype="multipart/form-data">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <label>What's not working?</label>
        <textarea name="issue" rows="5" required
          style="width:100%;padding:11px 12px;border-radius:10px;border:1px solid rgba(255,255,255,0.15);
          background:#180d0d;color:#fff;font-size:15px;font-family:inherit;resize:vertical;"
          placeholder="e.g. the submit button isn't doing anything, or my screenshot won't upload..."></textarea>
        {error_html}
        <button type="submit">Open Ticket</button>
      </form>
      <a class="btn-link" href="javascript:history.back()">&larr; Back</a>
    """
    return render_page("Get Help", body, demo)


def render_ticket_success(demo: bool) -> str:
    note = (
        "<p class='note'>Test mode: no thread was actually created \u2014 in the real "
        "version this opens a private ticket thread in your server's tickets channel "
        "and adds you to it automatically.</p>"
        if demo
        else "<p class='note'>Check Discord \u2014 a private ticket thread has been created and you've been added to it.</p>"
    )
    body = f"""
      <div class="result">
        <div class="big">🎫</div>
        <h1>Ticket Opened</h1>
        <p class="sub">An admin will be with you shortly.</p>
      </div>
      {note}
    """
    return render_page("Ticket Opened", body, demo)


async def send_discord_confirmation(channel_id: int, display_name: str, donated: dict,
                                     filename: str, file_bytes: bytes):
    """Post the same confirmation embed /donate already sends, via Discord's
    REST API directly (no discord.py client needed in this process)."""
    if not DISCORD_BOT_TOKEN:
        print("[webapp] no DISCORD_BOT_TOKEN set, skipping Discord post")
        return

    embed = {
        "title": "🏦 Donation Recorded",
        "description": f"Thank you, **{display_name}**! Your donation has been logged.",
        "color": 0xFF0000,
        "fields": [
            {"name": name, "value": fmt_num(amount), "inline": True}
            for name, amount in donated.items()
        ],
        "image": {"url": f"attachment://{filename}"},
        "footer": {"text": "WOLVES 🐺 | BRAVIA 3953 • Alliance Bank Donations Tracker"},
    }

    form = FormData()
    form.add_field("payload_json", __import__("json").dumps({"embeds": [embed]}), content_type="application/json")
    form.add_field("files[0]", file_bytes, filename=filename, content_type="application/octet-stream")

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    async with ClientSession() as session:
        async with session.post(
            f"{DISCORD_API}/channels/{channel_id}/messages", data=form, headers=headers
        ) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print(f"[webapp] Discord post failed ({resp.status}): {text}")


FORUM_CHANNEL_TYPE = 15


async def create_support_ticket(tickets_channel_id: int, user_id: int, display_name: str, issue: str, from_channel_id: int = None):
    """Create a ticket in the configured tickets channel, add the reporter
    to it, and post the issue as an embed. Mirrors what the in-Discord
    /ticket command does, but via plain REST calls since this process has
    no discord.py Client of its own.

    Supports both a plain text channel (ticket = a private thread) and a
    forum channel (ticket = a forum post, which can't be made private --
    its visibility is whatever the forum channel's own permissions are)."""
    if not DISCORD_BOT_TOKEN:
        print("[webapp] no DISCORD_BOT_TOKEN set, skipping ticket creation")
        return False

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    thread_name = f"ticket-{display_name}"[:100]

    embed = {
        "title": "🎫 New Support Ticket",
        "description": issue,
        "color": 0xFF0000,
        "fields": [{"name": "Opened by", "value": f"<@{user_id}>", "inline": True}],
        "footer": {"text": "WOLVES 🐺 | BRAVIA 3953 • Support Tickets (via donation site)"},
    }
    if from_channel_id:
        embed["fields"].append({"name": "From channel", "value": f"<#{from_channel_id}>", "inline": True})

    async with ClientSession() as session:
        async with session.get(f"{DISCORD_API}/channels/{tickets_channel_id}", headers=headers) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print(f"[webapp] couldn't look up tickets channel ({resp.status}): {text}")
                return False
            channel_info = await resp.json()
        is_forum = channel_info.get("type") == FORUM_CHANNEL_TYPE

        if is_forum:
            # Forum posts carry their starter message in the same request
            # that creates them -- there's no separate "type" for private
            # forum posts, so this post's visibility is whatever the forum
            # channel's own permissions allow.
            body = {"name": thread_name, "message": {"embeds": [embed]}}
        else:
            body = {"name": thread_name, "type": 12, "auto_archive_duration": 1440}

        async with session.post(
            f"{DISCORD_API}/channels/{tickets_channel_id}/threads", json=body, headers=headers
        ) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print(f"[webapp] ticket thread creation failed ({resp.status}): {text}")
                return False
            thread = await resp.json()
            thread_id = thread["id"]

        async with session.put(
            f"{DISCORD_API}/channels/{thread_id}/thread-members/{user_id}", headers=headers
        ) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print(f"[webapp] adding user to ticket thread failed ({resp.status}): {text}")

        if not is_forum:
            # Text-channel private threads start empty -- post the embed as
            # their first message. Forum posts already got it above.
            async with session.post(
                f"{DISCORD_API}/channels/{thread_id}/messages", json={"embeds": [embed]}, headers=headers
            ) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    print(f"[webapp] posting ticket message failed ({resp.status}): {text}")

    return True


async def handle_ticket_get(request: web.Request):
    token = request.query.get("token", "")
    payload = donate_token.verify_token(token)
    demo = bool(payload.get("demo")) if payload else False
    if not payload:
        return web.Response(text=render_error("This link is invalid or has expired."), content_type="text/html")
    return web.Response(text=render_ticket_form(payload, demo, token), content_type="text/html")


async def handle_ticket_submit(request: web.Request):
    data = await request.post()
    token = data.get("token", "")
    payload = donate_token.verify_token(token)
    demo = bool(payload.get("demo")) if payload else False
    if not payload:
        return web.Response(text=render_error("This link is invalid or has expired."), content_type="text/html")

    issue = str(data.get("issue", "")).strip()
    if not issue:
        return web.Response(
            text=render_ticket_form(payload, demo, token, error="Please describe the issue first."),
            content_type="text/html",
        )

    display_name = payload.get("n", "Donor")
    user_id = payload.get("u")
    guild_id = payload.get("g")
    from_channel_id = payload.get("c")

    if demo:
        print(f"[webapp][DEMO] would open ticket for guild={guild_id}: {display_name} -> {issue!r}")
        return web.Response(text=render_ticket_success(demo=True), content_type="text/html")

    # --- Real path (used once this is wired into the live bot) ---
    config = await server_config.get_config(guild_id)
    tickets_channel_id = config.get("tickets_channel_id")
    if not tickets_channel_id:
        return web.Response(
            text=render_error(
                "This server hasn't set up a tickets channel yet. Please contact an admin directly on Discord.",
                token=token,
            ),
            content_type="text/html",
        )

    ok = await create_support_ticket(tickets_channel_id, user_id, display_name, issue, from_channel_id)
    if not ok:
        return web.Response(
            text=render_error("Couldn't open a ticket automatically. Please contact an admin directly on Discord.", token=token),
            content_type="text/html",
        )

    return web.Response(text=render_ticket_success(demo=False), content_type="text/html")


async def handle_root(request: web.Request):
    """Plain landing page for anyone who opens the bare domain directly
    instead of using their personal /donate link from Discord."""
    body = """
      <div class="result">
        <div class="big">🐺</div>
        <h1>WOLVES Alliance Bank</h1>
        <p class="sub">This page is for logging donations and support tickets.</p>
      </div>
      <p class="note">Go to Discord and run <code>/donate</code> to get your personal,
      one-time donation link, or <code>/ticket</code> if something's not working.</p>
    """
    return web.Response(text=render_page("WOLVES Alliance Bank", body, demo=False), content_type="text/html")


async def handle_demo(request: web.Request):
    """Not linked anywhere in production — a safe way to click through the
    whole flow (donation form + ticket form) without a real /donate link.
    No real spreadsheet write or Discord post ever happens through this."""
    token = donate_token.generate_token({
        "g": 0, "c": 0, "u": 0,
        "n": "Test Donor", "bank": "KD Bank 53 (demo)",
        "demo": True,
    })
    raise web.HTTPFound(f"/donate?token={token}&demo=1")


async def handle_donate_get(request: web.Request):
    token = request.query.get("token", "")
    payload = donate_token.verify_token(token)
    demo = bool(payload.get("demo")) if payload else False
    if not payload:
        return web.Response(text=render_error("This link is invalid or has expired."), content_type="text/html")
    return web.Response(text=render_form(payload, demo, token), content_type="text/html")


async def handle_donate_submit(request: web.Request):
    data = await request.post()
    token = data.get("token", "")
    payload = donate_token.verify_token(token)
    demo = bool(payload.get("demo")) if payload else False
    if not payload:
        return web.Response(text=render_error("This link is invalid or has expired."), content_type="text/html")

    def to_int(v):
        try:
            return max(0, int(str(v).strip() or 0))
        except ValueError:
            return 0

    resources = {
        "Food": to_int(data.get("food")),
        "Wood": to_int(data.get("wood")),
        "Stone": to_int(data.get("stone")),
        "Gold": to_int(data.get("gold")),
    }
    donated = {k: v for k, v in resources.items() if v > 0}

    proof = data.get("proof")
    has_file = proof is not None and getattr(proof, "filename", None)

    if not donated:
        return web.Response(
            text=render_form(payload, demo, token, error="Enter at least one resource amount above 0."),
            content_type="text/html",
        )
    if not has_file:
        return web.Response(
            text=render_form(payload, demo, token, error="Please attach your Assistance Report screenshot."),
            content_type="text/html",
        )

    file_bytes = proof.file.read()
    filename = proof.filename or "proof.png"
    display_name = payload.get("n", "Donor")
    guild_id = payload.get("g")
    channel_id = payload.get("c")

    if demo:
        # Safe preview: don't touch the real sheet or Discord.
        print(f"[webapp][DEMO] would log for guild={guild_id}: {display_name} -> {donated}")
        return web.Response(text=render_success(payload, donated, demo=True), content_type="text/html")

    # --- Real path (used once this is wired into the live bot) ---
    config = await server_config.get_config(guild_id)
    try:
        await sheets.append_donation(
            display_name,
            donated,
            sheet_id=config["donations_sheet_id"],
            donations_tab_name=config["donations_tab_name"],
            members_tab_name=config["alliance_members_tab_name"],
        )
    except Exception as e:
        return web.Response(
            text=render_error(f"Couldn't save your donation: {e}", token=token), content_type="text/html"
        )

    await send_discord_confirmation(channel_id, display_name, donated, filename, file_bytes)
    return web.Response(text=render_success(payload, donated, demo=False), content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/demo", handle_demo)
    app.router.add_get("/donate", handle_donate_get)
    app.router.add_post("/donate/submit", handle_donate_submit)
    app.router.add_get("/ticket", handle_ticket_get)
    app.router.add_post("/ticket/submit", handle_ticket_submit)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(build_app(), host="0.0.0.0", port=port)
