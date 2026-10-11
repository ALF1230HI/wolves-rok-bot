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
import ai_helper

RED_HEX = "#FF0000"

# Proof-screenshot uploads can legitimately be a few MB on modern phones
# (especially PNG screenshots, which aren't as compressed as JPEGs), so the
# default aiohttp request-body cap of 1MB is far too small and was rejecting
# real donations with a raw "Maximum request body size exceeded" error.
# 20MB comfortably covers normal phone screenshots while staying under
# Discord's own attachment limits.
MAX_UPLOAD_MB = 20
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0505">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
  body {{
    margin: 0; padding: 0;
    background: radial-gradient(circle at top, #1b0e0e 0%, #0b0505 60%, #050202 100%);
    color: #f1e9e9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
    min-height: 100dvh;
    display: flex; justify-content: center;
    overflow-x: hidden;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    width: 100%; max-width: 560px;
    padding: max(20px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(40px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  }}
  .banner {{
    background: #3a1d00; border: 1px solid #ffb020; color: #ffd98a;
    padding: 10px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 18px;
    text-align: center;
  }}
  .card {{
    background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01));
    border: 1px solid rgba(255,70,70,0.35);
    border-radius: 16px;
    padding: 24px 18px;
    box-shadow: 0 10px 40px rgba(255,0,0,0.08);
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; color: #ffffff; line-height: 1.3; }}
  h1 .wolf {{ margin-right: 6px; }}
  .sub {{ color: #cf9f9f; font-size: 14px; margin: 0 0 20px; line-height: 1.5; }}
  h2 {{ font-size: 15px; color: {red}; text-transform: uppercase; letter-spacing: 0.06em; margin: 26px 0 10px; }}
  ol {{ margin: 0; padding-left: 22px; color: #eadcdc; font-size: 15px; line-height: 1.75; }}
  ol li {{ margin-bottom: 4px; }}
  ol li b {{ color: #fff; }}
  .note {{ font-size: 13px; color: #a98484; margin-top: 14px; line-height: 1.6; }}
  label {{ display: block; font-size: 14px; color: #e9c9c9; margin: 14px 0 7px; font-weight: 600; }}
  input[type=number], input[type=text], textarea {{
    width: 100%; padding: 13px 14px; border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.15); background: #180d0d; color: #fff;
    font-size: 16px; font-family: inherit; -webkit-appearance: none; appearance: none;
    min-height: 48px;
  }}
  textarea {{ min-height: 110px; resize: vertical; line-height: 1.5; }}
  input[type=number] {{ -moz-appearance: textfield; }}
  input[type=number]::-webkit-outer-spin-button,
  input[type=number]::-webkit-inner-spin-button {{ -webkit-appearance: none; margin: 0; }}
  input:focus, textarea:focus {{ outline: none; border-color: {red}; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  @media (max-width: 380px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  .file-drop {{
    margin-top: 6px; border: 1.5px dashed rgba(255,255,255,0.25); border-radius: 12px;
    padding: 20px 16px; text-align: center; font-size: 14px; color: #d8b9b9;
    background: rgba(255,255,255,0.02); cursor: pointer; display: block;
    min-height: 48px; touch-action: manipulation; position: relative;
  }}
  .file-drop:active {{ background: rgba(255,255,255,0.05); }}
  .file-drop-text {{ display: block; pointer-events: none; }}
  .file-drop .filename {{ display: block; margin-top: 8px; font-size: 13px; color: #ff9a9a; font-weight: 600; word-break: break-all; pointer-events: none; }}
  .file-drop input {{
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
  }}
  button {{
    margin-top: 22px; width: 100%; padding: 15px; border: none; border-radius: 12px;
    background: {red}; color: #fff; font-size: 16px; font-weight: 700; letter-spacing: 0.02em;
    cursor: pointer; min-height: 50px; touch-action: manipulation; -webkit-appearance: none;
  }}
  button:active {{ filter: brightness(0.9); transform: scale(0.99); }}
  @media (hover: hover) and (pointer: fine) {{
    button:hover {{ filter: brightness(1.08); }}
  }}
  .footer {{ text-align: center; font-size: 11px; color: #7a5656; margin-top: 18px; }}
  .pill {{ display: inline-block; background: rgba(255,0,0,0.12); border: 1px solid rgba(255,0,0,0.3);
    color: #ff9a9a; font-size: 12px; padding: 4px 10px; border-radius: 999px; margin-bottom: 14px; }}
  .result {{ text-align: center; padding: 10px 0 4px; }}
  .result .big {{ font-size: 42px; margin-bottom: 8px; }}
  .amounts {{ list-style: none; padding: 0; margin: 16px 0; }}
  .amounts li {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 15px; gap: 12px; }}
  a.btn-link {{
    display: flex; align-items: center; justify-content: center; text-align: center;
    margin-top: 18px; padding: 14px; border-radius: 12px; min-height: 48px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15);
    color: #fff; text-decoration: none; font-size: 15px; touch-action: manipulation;
  }}
  a.btn-link:active {{ background: rgba(255,255,255,0.1); }}
  .error {{ color: #ff8a8a; font-size: 13px; margin-top: 10px; line-height: 1.5; }}
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
<script>
  window.__wolvesFileChange = function(input) {{
    var label = input.closest('.file-drop');
    if (!label) return;
    var existing = label.querySelector('.filename');
    var name = (input.files && input.files[0]) ? input.files[0].name : '';
    if (!name) {{
      if (existing) existing.remove();
      return;
    }}
    if (!existing) {{
      existing = document.createElement('span');
      existing.className = 'filename';
      label.appendChild(existing);
    }}
    existing.textContent = '✅ ' + name;
  }};
</script>
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
            <input type="text" name="food" value="0" inputmode="numeric" pattern="[0-9]*" autocomplete="off">
          </div>
          <div>
            <label>🪵 Wood</label>
            <input type="text" name="wood" value="0" inputmode="numeric" pattern="[0-9]*" autocomplete="off">
          </div>
          <div>
            <label>🪨 Stone</label>
            <input type="text" name="stone" value="0" inputmode="numeric" pattern="[0-9]*" autocomplete="off">
          </div>
          <div>
            <label>🪙 Gold</label>
            <input type="text" name="gold" value="0" inputmode="numeric" pattern="[0-9]*" autocomplete="off">
          </div>
        </div>

        <h2>Proof screenshot</h2>
        <label class="file-drop">
          <span class="file-drop-text">📸 Tap to choose your Assistance Report screenshot</span>
          <input type="file" name="proof" accept="image/*" required onchange="window.__wolvesFileChange(this)">
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
          placeholder="e.g. the submit button isn't doing anything, or my screenshot won't upload..."></textarea>

        <label>Screenshot (optional)</label>
        <label class="file-drop">
          <span class="file-drop-text">📸 Tap to attach a screenshot — the error, or your Assistance Report</span>
          <input type="file" name="screenshot" accept="image/*" onchange="window.__wolvesFileChange(this)">
        </label>

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


async def create_support_ticket(tickets_channel_id: int, user_id: int, display_name: str, issue: str, from_channel_id: int = None, image: dict = None):
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

        # Same AI-first-response behavior as the in-Discord /ticket command:
        # try to help automatically, and if not confident, loop the human in
        # immediately. The "Still need help?" button is handled by the bot's
        # own gateway connection (matched purely by custom_id), so it works
        # the same whether the ticket was opened here or in Discord.
        images = ai_helper.prepare_images([image]) if image else []
        ai_result = await ai_helper.get_ai_response(issue, images=images)
        history_text = issue + (f"\n[📎 {len(images)} image attachment(s) included]" if images else "")
        ai_helper.start_session(thread_id, user_id, history_text, ai_result["reply"])
        ai_embed = {
            "title": "🤖 Automated Suggestion",
            "description": ai_result["reply"],
            "color": 0xFF0000,
        }
        if ai_result["confident"]:
            ai_embed["footer"] = {"text": "Didn't fix it? Click below to bring in a human."}
            ai_body = {
                "embeds": [ai_embed],
                "components": [ai_helper.still_need_help_component(thread_id)],
            }
        else:
            ai_embed["footer"] = {"text": "Looping in a human for this one."}
            ai_body = {"embeds": [ai_embed]}

        async with session.post(
            f"{DISCORD_API}/channels/{thread_id}/messages", json=ai_body, headers=headers
        ) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print(f"[webapp] posting AI ticket response failed ({resp.status}): {text}")

        if not ai_result["confident"]:
            # Stop the AI from replying to any more of the opener's
            # messages in this thread now that a human needs to look at
            # it -- same behavior as the in-Discord /ticket path.
            ai_helper.pause_session(thread_id)

        if not ai_result["confident"] and ai_helper.should_ping_human(thread_id):
            async with session.post(
                f"{DISCORD_API}/channels/{thread_id}/messages",
                json={"content": f"{ai_helper.escalate_mention()} — this ticket needs a human look, please take a peek! 🙏"},
                headers=headers,
            ) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    print(f"[webapp] posting ticket escalation ping failed ({resp.status}): {text}")

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

    screenshot = data.get("screenshot")
    image = None
    if screenshot is not None and getattr(screenshot, "filename", None):
        image = {
            "filename": screenshot.filename,
            "mime_type": screenshot.content_type or "",
            "data": screenshot.file.read(),
        }

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

    ok = await create_support_ticket(tickets_channel_id, user_id, display_name, issue, from_channel_id, image=image)
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


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPRequestEntityTooLarge:
        body = f"""
          <div class="result">
            <div class="big">📦</div>
            <h1>Screenshot too large</h1>
            <p class="sub">That image is bigger than the {MAX_UPLOAD_MB}MB limit for proof screenshots.</p>
          </div>
          <p class="note">Try a smaller screenshot — cropping it to just the Assistance Report entry
          usually helps — then go back and try again.</p>
          <a class="btn-link" href="javascript:history.back()">&larr; Back</a>
        """
        return web.Response(
            text=render_page("Screenshot too large", body, demo=False),
            content_type="text/html",
            status=413,
        )
    except web.HTTPException:
        raise
    except Exception as e:
        print(f"[webapp] unhandled error on {request.path}: {e!r}")
        body = """
          <div class="result">
            <div class="big">⚠️</div>
            <h1>Something went wrong</h1>
            <p class="sub">Please go back and try again. If it keeps happening, open a ticket from
            Discord and let us know.</p>
          </div>
        """
        return web.Response(
            text=render_page("Something went wrong", body, demo=False),
            content_type="text/html",
            status=500,
        )


def build_app() -> web.Application:
    app = web.Application(client_max_size=MAX_UPLOAD_BYTES, middlewares=[error_middleware])
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
