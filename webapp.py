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
import json
import asyncio
from datetime import datetime, timezone as dt_timezone

from aiohttp import web, ClientSession, FormData

import donate_token
import sheets
import server_config
import ai_helper

RED_HEX = "#FF0000"
WEEKLY_MINIMUM_PER_RESOURCE = 10_000_000
# The "All-Time" tab reads from the same running-totals tab the old
# all-time leaderboard used, before it was replaced by the weekly-reset
# one -- still handy here as a Hall of Fame view alongside the live week.
DONATION_SUMMARY_TAB_NAME = os.getenv("DONATION_SUMMARY_TAB_NAME", "Donation Summary")

# Tiny inline wolf-head favicon -- a data: URI so it works even inside the
# sandboxed, network-less preview iframe (no external file request needed).
FAVICON_DATA_URI = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Crect width='100' height='100' rx='20' fill='%230b0505'/%3E"
    "%3Ctext x='50' y='68' font-size='58' text-anchor='middle'%3E%F0%9F%90%BA%3C/text%3E%3C/svg%3E"
)

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


def fmt_num(n) -> str:
    """Formats a resource amount with thousands separators and a friendly
    M/B shorthand. Accepts ints or floats (the weekly-leaderboard sheet
    reads come back as floats) and always displays a whole number -- a
    donation amount is never fractional."""
    n = round(n)
    if n >= 1_000_000_000:
        return f"{n:,.0f} ({n/1_000_000_000:.2f}B)"
    if n >= 1_000_000:
        return f"{n:,.0f} ({n/1_000_000:.2f}M)"
    return f"{n:,.0f}"


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
<link rel="icon" href="{favicon}">
<meta name="description" content="{description}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{description}">
<title>{title}</title>
<style>
  @keyframes wolvesFadeUp {{
    from {{ opacity: 0; transform: translateY(10px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  @keyframes wolvesPop {{
    0% {{ transform: scale(0.6); opacity: 0; }}
    60% {{ transform: scale(1.08); opacity: 1; }}
    100% {{ transform: scale(1); }}
  }}
  @keyframes wolvesSpin {{ to {{ transform: rotate(360deg); }} }}
  @keyframes wolvesShimmer {{
    0% {{ background-position: -200px 0; }}
    100% {{ background-position: calc(200px + 100%) 0; }}
  }}
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
  body {{
    margin: 0; padding: 0;
    background:
      radial-gradient(circle at top, rgba(27,14,14,0.55) 0%, rgba(11,5,5,0.82) 55%, rgba(5,2,2,0.95) 100%),
      url('/assets/site_background.jpg') center top / cover no-repeat fixed,
      #0b0505;
    color: #f1e9e9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
    min-height: 100dvh;
    display: flex; justify-content: center;
    overflow-x: hidden;
    -webkit-font-smoothing: antialiased;
  }}
  /* background-attachment: fixed is unreliable on iOS Safari, where it can
     make backgrounds disappear while scrolling -- fall back to normal
     scroll attachment there instead. */
  @supports (-webkit-touch-callout: none) {{
    body {{
      background:
        radial-gradient(circle at top, rgba(27,14,14,0.55) 0%, rgba(11,5,5,0.82) 55%, rgba(5,2,2,0.95) 100%),
        url('/assets/site_background.jpg') center top / cover no-repeat scroll,
        #0b0505;
    }}
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
    animation: wolvesFadeUp 0.45s ease both;
  }}
  .stats-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 18px; }}
  @media (max-width: 380px) {{ .stats-row {{ grid-template-columns: 1fr; }} }}
  .stat-box {{
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px; padding: 12px 14px; text-align: center;
  }}
  .stat-box .stat-value {{ font-size: 18px; font-weight: 800; color: #fff; line-height: 1.2; }}
  .stat-box .stat-label {{ font-size: 11px; color: #cf9f9f; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 3px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; color: #ffffff; line-height: 1.3; }}
  h1 .wolf {{ margin-right: 6px; }}
  .sub {{ color: #cf9f9f; font-size: 14px; margin: 0 0 20px; line-height: 1.5; }}
  h2 {{ font-size: 15px; color: {red}; text-transform: uppercase; letter-spacing: 0.06em; margin: 26px 0 10px; }}
  ol {{ margin: 0; padding-left: 22px; color: #eadcdc; font-size: 15px; line-height: 1.75; }}
  ol li {{ margin-bottom: 4px; }}
  ol li b {{ color: #fff; }}
  .note {{ font-size: 13px; color: #a98484; margin-top: 14px; line-height: 1.6; }}
  .voluntary-note {{
    display: flex; align-items: flex-start; gap: 10px; margin: 16px 0 4px;
    padding: 12px 14px; border-radius: 12px; font-size: 13px; line-height: 1.55;
    background: rgba(90,200,140,0.08); border: 1px solid rgba(90,200,140,0.3); color: #bfe8cf;
  }}
  .voluntary-note .icon {{ flex-shrink: 0; font-size: 16px; line-height: 1.4; }}
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
  .field-block {{ position: relative; }}
  .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }}
  .chip {{
    width: auto; margin-top: 0; appearance: none; -webkit-appearance: none; border: 1px solid rgba(255,255,255,0.18);
    background: rgba(255,255,255,0.05); color: #e9c9c9; font-size: 11.5px; font-weight: 700;
    padding: 5px 10px; border-radius: 999px; cursor: pointer; touch-action: manipulation;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
  }}
  .chip:active {{ transform: scale(0.96); }}
  .chip.active, .chip:active {{ background: {red}; border-color: {red}; color: #fff; }}
  .total-row {{
    display: flex; justify-content: space-between; align-items: center; margin-top: 16px;
    padding: 12px 14px; border-radius: 12px; background: rgba(255,0,0,0.08);
    border: 1px solid rgba(255,0,0,0.25); font-size: 14px; color: #ffd0d0;
  }}
  .total-row .total-value {{ font-size: 17px; font-weight: 800; color: #fff; }}
  .file-drop {{
    margin-top: 6px; border: 1.5px dashed rgba(255,255,255,0.25); border-radius: 12px;
    padding: 20px 16px; text-align: center; font-size: 14px; color: #d8b9b9;
    background: rgba(255,255,255,0.02); cursor: pointer; display: block;
    min-height: 48px; touch-action: manipulation; position: relative;
    transition: border-color 0.15s, background 0.15s;
  }}
  .file-drop:active {{ background: rgba(255,255,255,0.05); }}
  .file-drop.drag-over {{ border-color: {red}; background: rgba(255,0,0,0.06); }}
  .file-drop.has-file {{ border-style: solid; border-color: rgba(60,220,130,0.5); }}
  .file-drop-text {{ display: block; pointer-events: none; }}
  .file-drop .filename {{ display: block; margin-top: 8px; font-size: 13px; color: #ff9a9a; font-weight: 600; word-break: break-all; pointer-events: none; }}
  .file-drop .preview-thumb {{
    display: block; max-width: 100%; max-height: 160px; margin: 10px auto 0; border-radius: 8px;
    pointer-events: none; animation: wolvesPop 0.3s ease both;
  }}
  .file-drop input {{
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
  }}
  button {{
    margin-top: 22px; width: 100%; padding: 15px; border: none; border-radius: 12px;
    background: {red}; color: #fff; font-size: 16px; font-weight: 700; letter-spacing: 0.02em;
    cursor: pointer; min-height: 50px; touch-action: manipulation; -webkit-appearance: none;
    transition: filter 0.15s, transform 0.1s, opacity 0.15s;
    display: flex; align-items: center; justify-content: center; gap: 9px;
  }}
  button:active {{ filter: brightness(0.9); transform: scale(0.99); }}
  button:disabled {{ opacity: 0.75; cursor: default; }}
  @media (hover: hover) and (pointer: fine) {{
    button:hover:not(:disabled) {{ filter: brightness(1.08); }}
  }}
  .spinner {{
    display: none; width: 17px; height: 17px; border-radius: 50%;
    border: 2.5px solid rgba(255,255,255,0.35); border-top-color: #fff;
    animation: wolvesSpin 0.7s linear infinite; flex-shrink: 0;
  }}
  button.loading .spinner {{ display: inline-block; }}
  .footer {{ text-align: center; font-size: 11px; color: #7a5656; margin-top: 18px; }}
  .pill {{ display: inline-block; background: rgba(255,0,0,0.12); border: 1px solid rgba(255,0,0,0.3);
    color: #ff9a9a; font-size: 12px; padding: 4px 10px; border-radius: 999px; margin-bottom: 14px; }}
  .result {{ text-align: center; padding: 10px 0 4px; }}
  .result .big {{ font-size: 42px; margin-bottom: 8px; animation: wolvesPop 0.5s ease both; }}
  .amounts {{ list-style: none; padding: 0; margin: 16px 0; }}
  .amounts li {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 15px; gap: 12px; }}
  .progress-block {{ margin: 18px 0 6px; }}
  .progress-block h2 {{ margin-top: 0; }}
  .progress-item {{ margin-bottom: 14px; }}
  .progress-item .progress-top {{ display: flex; justify-content: space-between; font-size: 13px; color: #e9c9c9; margin-bottom: 6px; }}
  .progress-item .progress-top .done {{ color: #7be37b; font-weight: 700; }}
  .progress-track {{ height: 10px; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden; }}
  .progress-fill {{
    height: 100%; border-radius: 999px; background: linear-gradient(90deg, {red}, #ff8a4c);
    transition: width 0.8s cubic-bezier(.22,1,.36,1); width: 0%;
  }}
  .progress-fill.complete {{ background: linear-gradient(90deg, #2ecf6f, #7be37b); }}
  a.btn-link {{
    display: flex; align-items: center; justify-content: center; text-align: center;
    margin-top: 18px; padding: 14px; border-radius: 12px; min-height: 48px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15);
    color: #fff; text-decoration: none; font-size: 15px; touch-action: manipulation;
    transition: background 0.15s;
  }}
  a.btn-link:active {{ background: rgba(255,255,255,0.1); }}
  .error {{ color: #ff8a8a; font-size: 13px; margin-top: 10px; line-height: 1.5; animation: wolvesFadeUp 0.25s ease both; }}
  #confetti-canvas, #embers-canvas {{ position: fixed; inset: 0; pointer-events: none; }}
  #embers-canvas {{ z-index: 0; }}
  #confetti-canvas {{ z-index: 999; }}
  .wrap {{ position: relative; z-index: 1; }}
  .stat-box {{ animation: wolvesPop 0.5s ease both; }}
  .stat-box:nth-child(2) {{ animation-delay: 0.08s; }}
  .mini-board {{ margin: 0 0 18px; }}
  .mini-board h2 {{ margin-top: 0; display: flex; align-items: center; justify-content: space-between; }}
  .mini-board .week-range {{ font-size: 10.5px; color: #a98484; text-transform: none; letter-spacing: 0; font-weight: 400; }}
  .mini-board ol {{ list-style: none; padding: 0; margin: 0; }}
  .mini-board li {{
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 9px 12px; border-radius: 10px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); margin-bottom: 6px; font-size: 13.5px;
    animation: wolvesFadeUp 0.4s ease both;
  }}
  .mini-board li:nth-child(1) {{ animation-delay: 0.05s; }}
  .mini-board li:nth-child(2) {{ animation-delay: 0.12s; }}
  .mini-board li:nth-child(3) {{ animation-delay: 0.19s; }}
  .mini-board .medal {{ font-size: 15px; margin-right: 2px; }}
  .mini-board .donor-name {{ flex: 1; color: #f1e9e9; font-weight: 600; }}
  .mini-board .donor-total {{ color: #ff9a9a; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .tier-badge {{
    display: inline-flex; align-items: center; gap: 7px; margin: 14px auto 0; padding: 8px 18px;
    border-radius: 999px; font-weight: 800; font-size: 14px; animation: wolvesPop 0.5s ease 0.15s both;
  }}
  .share-btn {{
    display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 10px;
    padding: 13px; border-radius: 12px; min-height: 46px; width: 100%;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15);
    color: #fff; font-size: 14.5px; font-weight: 600; cursor: pointer; touch-action: manipulation;
  }}
  .share-btn:active {{ background: rgba(255,255,255,0.1); }}
  .toast {{
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%) translateY(20px);
    background: #1b0e0e; border: 1px solid rgba(255,255,255,0.15); color: #fff; font-size: 13.5px;
    padding: 11px 18px; border-radius: 999px; z-index: 1000; opacity: 0; transition: opacity 0.25s, transform 0.25s;
    pointer-events: none; white-space: nowrap;
  }}
  .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
  .tab-bar {{
    display: flex; gap: 6px; overflow-x: auto; margin-bottom: 20px; padding-bottom: 2px;
    -ms-overflow-style: none; scrollbar-width: none;
  }}
  .tab-bar::-webkit-scrollbar {{ display: none; }}
  .tab-btn {{
    flex: 0 0 auto; width: auto; margin-top: 0; white-space: nowrap; appearance: none; -webkit-appearance: none;
    border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.03);
    color: #cf9f9f; font-size: 13px; font-weight: 700; padding: 9px 14px; border-radius: 999px;
    cursor: pointer; touch-action: manipulation; transition: background 0.15s, color 0.15s, border-color 0.15s;
  }}
  .tab-btn.active {{ background: {red}; border-color: {red}; color: #fff; }}
  .tab-btn:active {{ transform: scale(0.97); }}
  .tab-panel {{ display: none; animation: wolvesFadeUp 0.3s ease both; }}
  .tab-panel.active {{ display: block; }}
  .board-list {{ list-style: none; padding: 0; margin: 0 0 4px; }}
  .board-list li {{
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 11px 14px; border-radius: 10px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); margin-bottom: 7px; font-size: 14px;
    animation: wolvesFadeUp 0.35s ease both;
  }}
  .board-list .rank {{ font-size: 15px; width: 26px; flex-shrink: 0; color: #cf9f9f; font-weight: 700; }}
  .board-list .donor-name {{ flex: 1; color: #f1e9e9; font-weight: 600; }}
  .board-list .donor-total {{ color: #ff9a9a; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .compliance-hero {{ text-align: center; margin-bottom: 18px; }}
  .compliance-hero .big-ratio {{ font-size: 40px; font-weight: 800; color: #fff; }}
  .compliance-hero .big-sub {{ font-size: 13px; color: #cf9f9f; margin-top: 4px; }}
  .compliance-track {{ height: 12px; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden; margin-top: 12px; }}
  .compliance-fill {{
    height: 100%; border-radius: 999px; background: linear-gradient(90deg, #2ecf6f, #7be37b);
    transition: width 0.8s cubic-bezier(.22,1,.36,1); width: 0%;
  }}
  .compliant-grid {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 16px; }}
  .compliant-chip {{
    background: rgba(46,207,111,0.12); border: 1px solid rgba(46,207,111,0.35); color: #9cf0b8;
    font-size: 12.5px; font-weight: 600; padding: 6px 11px; border-radius: 999px;
  }}
  .empty-state {{ text-align: center; padding: 24px 10px; color: #a98484; font-size: 14px; }}
  @media (prefers-reduced-motion: reduce) {{
    .card, .stat-box, .mini-board li, .tier-badge, .error, .result .big, .tab-panel, .board-list li {{ animation: none !important; }}
  }}
</style>
</head>
<body>
<canvas id="embers-canvas"></canvas>
<canvas id="confetti-canvas"></canvas>
<div class="wrap">
{banner}
<div class="card">
{body}
</div>
<div class="footer">WOLVES 🐺 | BRAVIA 3953 &bull; Alliance Bank Donations</div>
</div>
<div class="toast" id="wolves-toast"></div>
<script>
  var WOLVES_MAX_UPLOAD_BYTES = {max_upload_bytes};

  // Tab switching -- all tab panels are rendered server-side up front;
  // this just toggles which one is visible, with the active tab
  // remembered per-page-type (donate vs ticket) in sessionStorage so a
  // refresh (e.g. after submitting) comes back to the same tab.
  window.__wolvesTab = function(name, btn) {{
    var bar = btn.closest('.tab-bar');
    if (bar) bar.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(function(p) {{
      p.classList.toggle('active', p.id === 'tab-' + name);
    }});
    if (window.__wolvesVibrate) window.__wolvesVibrate(8);
    try {{ sessionStorage.setItem('wolvesActiveTab', name); }} catch (e) {{}}
  }};
  (function restoreTab() {{
    try {{
      var saved = sessionStorage.getItem('wolvesActiveTab');
      var btn = saved && document.querySelector('.tab-btn[data-tab="' + saved + '"]');
      if (btn) window.__wolvesTab(saved, btn);
    }} catch (e) {{}}
  }})();

  window.__wolvesFileChange = function(input) {{
    var label = input.closest('.file-drop');
    if (!label) return;
    var existing = label.querySelector('.filename');
    var thumb = label.querySelector('.preview-thumb');
    var errEl = label.parentElement.querySelector('.client-file-error');
    var file = (input.files && input.files[0]) ? input.files[0] : null;

    if (errEl) errEl.remove();
    if (!file) {{
      if (existing) existing.remove();
      if (thumb) thumb.remove();
      label.classList.remove('has-file');
      return;
    }}

    if (file.size > WOLVES_MAX_UPLOAD_BYTES) {{
      input.value = '';
      if (existing) existing.remove();
      if (thumb) thumb.remove();
      label.classList.remove('has-file');
      var msg = document.createElement('div');
      msg.className = 'error client-file-error';
      msg.textContent = '⚠️ That file is too large — please pick one under ' +
        Math.round(WOLVES_MAX_UPLOAD_BYTES / (1024*1024)) + 'MB.';
      label.insertAdjacentElement('afterend', msg);
      return;
    }}

    label.classList.add('has-file');
    if (!existing) {{
      existing = document.createElement('span');
      existing.className = 'filename';
      label.appendChild(existing);
    }}
    existing.textContent = '✅ ' + file.name;

    if (file.type && file.type.indexOf('image/') === 0 && window.FileReader) {{
      var reader = new FileReader();
      reader.onload = function(e) {{
        if (!thumb) {{
          thumb = document.createElement('img');
          thumb.className = 'preview-thumb';
          label.appendChild(thumb);
        }}
        thumb.src = e.target.result;
      }};
      reader.readAsDataURL(file);
    }}
  }};

  // Drag-and-drop support on top of the existing tap-to-choose file inputs.
  document.querySelectorAll('.file-drop').forEach(function(label) {{
    var input = label.querySelector('input[type=file]');
    if (!input) return;
    ['dragenter', 'dragover'].forEach(function(evt) {{
      label.addEventListener(evt, function(e) {{
        e.preventDefault(); e.stopPropagation();
        label.classList.add('drag-over');
      }});
    }});
    ['dragleave', 'drop'].forEach(function(evt) {{
      label.addEventListener(evt, function(e) {{
        e.preventDefault(); e.stopPropagation();
        label.classList.remove('drag-over');
      }});
    }});
    label.addEventListener('drop', function(e) {{
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {{
        input.files = files;
        window.__wolvesFileChange(input);
      }}
    }});
  }});

  // Quick-pick amount chips: click to fill (or clear) a resource field.
  window.__wolvesChip = function(btn, inputName, value) {{
    var input = document.querySelector('input[name="' + inputName + '"]');
    if (!input) return;
    var row = btn.closest('.chip-row');
    var already = btn.classList.contains('active');
    if (row) row.querySelectorAll('.chip').forEach(function(c) {{ c.classList.remove('active'); }});
    input.value = already ? '0' : String(value);
    if (!already) btn.classList.add('active');
    window.__wolvesVibrate(12);
    window.__wolvesUpdateTotal && window.__wolvesUpdateTotal();
  }};

  // Short haptic buzz on supported mobile browsers -- a no-op everywhere
  // else (desktop browsers simply don't have navigator.vibrate).
  window.__wolvesVibrate = function(ms) {{
    try {{ if (navigator.vibrate) navigator.vibrate(ms); }} catch (e) {{}}
  }};

  window.__wolvesReducedMotion = function() {{
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }};

  // Small bottom toast, used by the share button's clipboard-copy fallback.
  window.__wolvesToast = function(message) {{
    var el = document.getElementById('wolves-toast');
    if (!el) return;
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(window.__wolvesToastTimer);
    window.__wolvesToastTimer = setTimeout(function() {{ el.classList.remove('show'); }}, 2200);
  }};

  // Success-page "brag about it" button: native share sheet where
  // available (mobile), clipboard copy + toast everywhere else.
  window.__wolvesShare = function(text) {{
    window.__wolvesVibrate(10);
    if (navigator.share) {{
      navigator.share({{ text: text }}).catch(function() {{}});
      return;
    }}
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(function() {{
        window.__wolvesToast('📋 Copied to clipboard!');
      }}).catch(function() {{
        window.__wolvesToast('Could not copy — try manually selecting the text.');
      }});
    }}
  }};

  // Live running total under the amounts grid.
  window.__wolvesUpdateTotal = function() {{
    var el = document.getElementById('wolves-total-value');
    if (!el) return;
    var sum = 0;
    ['food', 'wood', 'stone', 'gold'].forEach(function(n) {{
      var input = document.querySelector('input[name="' + n + '"]');
      if (input) {{
        var v = parseInt((input.value || '0').replace(/[^0-9]/g, ''), 10);
        if (!isNaN(v)) sum += v;
      }}
    }});
    el.textContent = sum.toLocaleString('en-US');
  }};
  document.querySelectorAll('input[name=food], input[name=wood], input[name=stone], input[name=gold]')
    .forEach(function(input) {{
      input.addEventListener('input', function() {{
        var row = input.closest('.field-block');
        if (row) row.querySelectorAll('.chip').forEach(function(c) {{ c.classList.remove('active'); }});
        window.__wolvesUpdateTotal();
      }});
    }});
  window.__wolvesUpdateTotal();

  // Loading state on submit so a slow upload can't be double-submitted.
  document.querySelectorAll('form').forEach(function(form) {{
    form.addEventListener('submit', function() {{
      var btn = form.querySelector('button[type=submit]');
      if (!btn || btn.classList.contains('loading')) return;
      btn.classList.add('loading');
      btn.disabled = true;
      var label = btn.querySelector('.btn-label');
      if (label) label.textContent = 'Submitting…';
    }});
  }});

  // Lightweight canvas confetti burst -- no external library, just a
  // couple hundred particles falling under gravity for ~2.5s. Triggered
  // from the success page only (see render_success).
  window.__wolvesConfetti = function() {{
    if (window.__wolvesReducedMotion()) return;
    var canvas = document.getElementById('confetti-canvas');
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var W = canvas.width = window.innerWidth;
    var H = canvas.height = window.innerHeight;
    var colors = ['#ff3b3b', '#ff8a4c', '#ffd166', '#f1e9e9', '#ff6b6b'];
    var pieces = [];
    for (var i = 0; i < 140; i++) {{
      pieces.push({{
        x: Math.random() * W, y: -20 - Math.random() * H * 0.5,
        w: 6 + Math.random() * 6, h: 8 + Math.random() * 10,
        color: colors[Math.floor(Math.random() * colors.length)],
        rot: Math.random() * Math.PI, vRot: (Math.random() - 0.5) * 0.3,
        vy: 2 + Math.random() * 3, vx: (Math.random() - 0.5) * 2.5,
      }});
    }}
    var start = Date.now();
    function frame() {{
      var elapsed = Date.now() - start;
      ctx.clearRect(0, 0, W, H);
      pieces.forEach(function(p) {{
        p.x += p.vx; p.y += p.vy; p.rot += p.vRot;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        ctx.restore();
      }});
      if (elapsed < 2600) {{
        requestAnimationFrame(frame);
      }} else {{
        ctx.clearRect(0, 0, W, H);
      }}
    }}
    requestAnimationFrame(frame);
  }};

  // Continuous, subtle ember particles drifting up from the bottom of the
  // screen -- echoes the embers/fire in the background artwork. Low
  // particle count and a capped device-pixel-ratio keep it cheap on
  // battery/CPU; skipped entirely for prefers-reduced-motion users.
  (function startEmbers() {{
    var canvas = document.getElementById('embers-canvas');
    if (!canvas || !canvas.getContext || window.__wolvesReducedMotion()) return;
    var ctx = canvas.getContext('2d');
    var W, H;
    function resize() {{ W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight; }}
    resize();
    window.addEventListener('resize', resize);

    var embers = [];
    var COUNT = 26;
    function spawn(initial) {{
      return {{
        x: Math.random() * W,
        y: initial ? Math.random() * H : H + 10,
        r: 1 + Math.random() * 2.2,
        speed: 0.35 + Math.random() * 0.7,
        drift: (Math.random() - 0.5) * 0.4,
        flicker: Math.random() * Math.PI * 2,
        life: 0.4 + Math.random() * 0.6,
      }};
    }}
    for (var i = 0; i < COUNT; i++) embers.push(spawn(true));

    function frame() {{
      ctx.clearRect(0, 0, W, H);
      embers.forEach(function(p) {{
        p.y -= p.speed;
        p.x += p.drift;
        p.flicker += 0.08;
        if (p.y < -10) {{ Object.assign(p, spawn(false)); }}
        var alpha = p.life * (0.5 + 0.5 * Math.sin(p.flicker));
        ctx.beginPath();
        ctx.fillStyle = 'rgba(255,' + (120 + Math.floor(60 * Math.sin(p.flicker))) + ',60,' + alpha.toFixed(2) + ')';
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }});
      requestAnimationFrame(frame);
    }}
    requestAnimationFrame(frame);
  }})();
</script>
</body>
</html>
"""


def render_page(title: str, body: str, demo: bool, description: str = None) -> str:
    banner = (
        '<div class="banner">🧪 TEST MODE — nothing on this page writes to the real '
        "spreadsheet or posts to Discord. Safe to click around.</div>"
        if demo
        else ""
    )
    return PAGE_SHELL.format(
        title=html.escape(title),
        body=body,
        banner=banner,
        red=RED_HEX,
        favicon=FAVICON_DATA_URI,
        description=html.escape(description or "Log your WOLVES alliance bank donation in a few taps."),
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )


def submit_button(label: str) -> str:
    return (
        f'<button type="submit"><span class="spinner"></span>'
        f'<span class="btn-label">{html.escape(label)}</span></button>'
    )


def render_stats_row(days_left: int, compliant: int, total_members: int) -> str:
    if days_left is None:
        return ""
    countdown_value = "Today! 🔁" if days_left == 0 else f"{days_left} day{'s' if days_left != 1 else ''}"
    compliance_value = f"{compliant}/{total_members}" if total_members else "—"
    return f"""
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-value">⏳ {countdown_value}</div>
          <div class="stat-label">Until Weekly Reset</div>
        </div>
        <div class="stat-box">
          <div class="stat-value">🧮 {compliance_value}</div>
          <div class="stat-label">Members Hit Minimum</div>
        </div>
      </div>
    """


_BOARD_MEDALS = ["🥇", "🥈", "🥉"]


def render_board_list(ranked: list, limit: int = 10) -> str:
    if not ranked:
        return '<div class="empty-state">No donations logged yet for this period — be the first! 🎉</div>'
    rows = []
    for i, (name, total) in enumerate(ranked[:limit]):
        rank_display = _BOARD_MEDALS[i] if i < len(_BOARD_MEDALS) else f"#{i + 1}"
        rows.append(
            f'<li><span class="rank">{rank_display}</span>'
            f'<span class="donor-name">{html.escape(name)}</span>'
            f'<span class="donor-total">{fmt_num(total)}</span></li>'
        )
    return f'<ul class="board-list">{"".join(rows)}</ul>'


def render_weekly_tab(ranked: list, start=None, end=None) -> str:
    week_range = f"{start.strftime('%b %-d')} – {end.strftime('%b %-d')}" if start and end else "this week"
    return f"""
      <h2>🏆 This Week's Top Donors</h2>
      <p class="note" style="margin-top:-6px;">{week_range}</p>
      {render_board_list(ranked)}
    """


def render_all_time_tab(ranked: list) -> str:
    return f"""
      <h2>👑 All-Time Hall of Fame</h2>
      <p class="note" style="margin-top:-6px;">Every donation ever logged for this alliance.</p>
      {render_board_list(ranked)}
    """


def render_compliance_tab(compliant_names: list, total_members: int, minimum: int) -> str:
    if not total_members:
        return """
          <h2>🧮 Weekly Compliance</h2>
          <div class="empty-state">No alliance members are registered yet.</div>
        """
    pct = (len(compliant_names) / total_members) * 100
    remaining = total_members - len(compliant_names)
    if remaining == 0:
        note = "Everyone has hit this week's minimum already — incredible work, WOLVES! 🎉"
    else:
        note = (
            f"<b>{remaining}</b> more member{'s' if remaining != 1 else ''} still need"
            f"{'s' if remaining == 1 else ''} to hit {fmt_num(minimum)} of EACH resource this week."
        )
    chips = "".join(f'<span class="compliant-chip">✅ {html.escape(n)}</span>' for n in compliant_names)
    chips_html = chips or '<p class="note">No one has hit the minimum yet this week — be the first! 🚀</p>'
    return f"""
      <h2>🧮 Weekly Compliance</h2>
      <div class="compliance-hero">
        <div class="big-ratio">{len(compliant_names)}/{total_members}</div>
        <div class="big-sub">members have hit this week's 10M-per-resource minimum</div>
        <div class="compliance-track"><div class="compliance-fill" style="width:{pct:.0f}%"></div></div>
      </div>
      <p class="note">{note}</p>
      <div class="compliant-grid">{chips_html}</div>
    """


def render_tabs(tabs: list) -> str:
    """`tabs` is a list of (id, label, html_content) tuples. The first tab
    is active by default; switching is pure client-side JS (see
    window.__wolvesTab in PAGE_SHELL) -- no page reload, no extra server
    round trip, since every tab's content is already rendered up front."""
    buttons = "".join(
        f'<button type="button" class="tab-btn{" active" if i == 0 else ""}" '
        f'data-tab="{tab_id}" onclick="window.__wolvesTab(\'{tab_id}\', this)">{html.escape(label)}</button>'
        for i, (tab_id, label, _content) in enumerate(tabs)
    )
    panels = "".join(
        f'<div class="tab-panel{" active" if i == 0 else ""}" id="tab-{tab_id}">{content}</div>'
        for i, (tab_id, _label, content) in enumerate(tabs)
    )
    return f'<div class="tab-bar">{buttons}</div>{panels}'


def _amount_field(label: str, name: str, emoji_chips: list) -> str:
    chips = "".join(
        f'<button type="button" class="chip" onclick="window.__wolvesChip(this, \'{name}\', {value})">{text}</button>'
        for text, value in emoji_chips
    )
    return f"""
          <div class="field-block">
            <label>{label}</label>
            <input type="text" name="{name}" value="0" inputmode="numeric" pattern="[0-9]*" autocomplete="off">
            <div class="chip-row">{chips}</div>
          </div>
    """


def render_form(
    payload: dict,
    demo: bool,
    token: str,
    error: str = None,
    days_left: int = None,
    compliant: int = None,
    total_members: int = None,
    top_donors: list = None,
    week_start=None,
    week_end=None,
    all_time_donors: list = None,
    compliant_names: list = None,
) -> str:
    display_name = html.escape(payload.get("n", "Donor"))
    bank_name = html.escape(payload.get("bank", "the alliance bank"))
    error_html = f'<div class="error">⚠️ {html.escape(error)}</div>' if error else ""
    stats_html = render_stats_row(days_left, compliant, total_members)
    quick_amounts = [("10M", 10_000_000), ("25M", 25_000_000), ("50M", 50_000_000)]
    donate_panel = f"""
      <span class="pill">Donating as {display_name}</span>
      <h1><span class="wolf">🐺</span>Alliance Bank Donation</h1>
      <p class="sub">Follow the steps below, then log exactly what you sent.</p>
      <div class="voluntary-note">
        <span class="icon">💚</span>
        <span>Donating is completely <b>voluntary</b> — it's not mandatory, and there's
        no pressure or penalty either way. The weekly minimum below is just a friendly
        target to aim for if you're able to. Every bit you give helps the alliance, but
        it's always your choice. Thank you! 🐺</span>
      </div>
      {stats_html}
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
        <p class="note" style="margin-top:0;">Tap a quick amount below or type your own exact total.</p>
        <div class="grid2">
          {_amount_field("🌾 Food", "food", quick_amounts)}
          {_amount_field("🪵 Wood", "wood", quick_amounts)}
          {_amount_field("🪨 Stone", "stone", quick_amounts)}
          {_amount_field("🪙 Gold", "gold", quick_amounts)}
        </div>
        <div class="total-row">
          <span>Total resources</span>
          <span class="total-value" id="wolves-total-value">0</span>
        </div>

        <h2>Proof screenshot</h2>
        <label class="file-drop">
          <span class="file-drop-text">📸 Tap to choose, or drag and drop your Assistance Report screenshot</span>
          <input type="file" name="proof" accept="image/*" required onchange="window.__wolvesFileChange(this)">
        </label>

        {error_html}
        {submit_button("Submit Donation")}
      </form>
      <p class="note">This link is personal to you and expires 30 minutes after
      <code>/donate</code> was run. If it stops working, just run <code>/donate</code>
      again in Discord for a fresh one.</p>
      <a class="btn-link" href="/ticket?token={html.escape(token)}">🎫 Something not working? Get help</a>
    """

    tabs = [
        ("donate", "🐺 Donate", donate_panel),
        ("week", "🏆 This Week", render_weekly_tab(top_donors or [], week_start, week_end)),
        ("alltime", "👑 All-Time", render_all_time_tab(all_time_donors or [])),
        ("compliance", "🧮 Compliance", render_compliance_tab(
            compliant_names or [], total_members or 0, WEEKLY_MINIMUM_PER_RESOURCE
        )),
    ]
    body = render_tabs(tabs)
    return render_page(
        "Donate — WOLVES Alliance Bank",
        body,
        demo,
        description=f"Log a resource donation to {payload.get('bank', 'the alliance bank')}.",
    )


def render_progress_bars(weekly_totals: dict, minimum: int = WEEKLY_MINIMUM_PER_RESOURCE) -> str:
    if not weekly_totals:
        return ""
    emoji = {"Food": "🌾", "Wood": "🪵", "Stone": "🪨", "Gold": "🪙"}
    items = []
    for name in ["Food", "Wood", "Stone", "Gold"]:
        total = weekly_totals.get(name, 0)
        pct = max(0, min(100, (total / minimum) * 100)) if minimum else 0
        complete = total >= minimum
        status = '<span class="done">✅ Done</span>' if complete else f"{fmt_num(minimum - total)} to go"
        items.append(f"""
          <div class="progress-item">
            <div class="progress-top">
              <span>{emoji.get(name, '')} {name} — {fmt_num(total)} / {fmt_num(minimum)}</span>
              <span>{status}</span>
            </div>
            <div class="progress-track"><div class="progress-fill{' complete' if complete else ''}" style="width:{pct:.0f}%"></div></div>
          </div>
        """)
    return f"""
      <div class="progress-block">
        <h2>This week's progress</h2>
        {''.join(items)}
      </div>
    """


_DONATION_TIERS = [
    (500_000_000, "👑", "Legendary Donor", "rgba(255,215,90,0.16)", "#ffd95a", "rgba(255,215,90,0.45)"),
    (100_000_000, "💎", "Diamond Donor", "rgba(120,200,255,0.16)", "#9fe0ff", "rgba(120,200,255,0.45)"),
    (40_000_000, "🥇", "Gold Donor", "rgba(255,200,80,0.14)", "#ffd27a", "rgba(255,200,80,0.4)"),
    (10_000_000, "🥈", "Silver Donor", "rgba(220,220,230,0.14)", "#e3e3ea", "rgba(220,220,230,0.4)"),
    (0, "🥉", "Bronze Donor", "rgba(205,140,90,0.14)", "#e3b08a", "rgba(205,140,90,0.4)"),
]


def _donation_tier(total: int) -> tuple:
    for threshold, emoji, label, bg, color, border in _DONATION_TIERS:
        if total >= threshold:
            return emoji, label, bg, color, border
    return _DONATION_TIERS[-1][1:]


def render_success(payload: dict, donated: dict, demo: bool, weekly_totals: dict = None) -> str:
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
    progress_html = render_progress_bars(weekly_totals)

    total_donated = sum(donated.values())
    emoji, label, bg, color, border = _donation_tier(total_donated)
    tier_html = (
        f'<span class="tier-badge" style="background:{bg};color:{color};border:1px solid {border};">'
        f'{emoji} {html.escape(label)}</span>'
    )
    share_text = (
        f"🐺 Just donated {fmt_num(total_donated)} resources to the WOLVES alliance bank! 💪 "
        f"#WolvesStrong"
    )
    share_html = (
        f'<button type="button" class="share-btn" '
        f'onclick="window.__wolvesShare({json.dumps(share_text)})">📣 Brag about it</button>'
    )

    body = f"""
      <div class="result">
        <div class="big">✅</div>
        <h1>Donation Recorded</h1>
        <p class="sub">Thank you, {display_name}!</p>
        <div>{tier_html}</div>
      </div>
      <ul class="amounts">{rows}</ul>
      {share_html}
      {progress_html}
      {note}
      <script>
        window.__wolvesConfetti && window.__wolvesConfetti();
        window.__wolvesVibrate && window.__wolvesVibrate([15, 40, 15]);
      </script>
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
        {submit_button("Open Ticket")}
      </form>
      <a class="btn-link" href="javascript:history.back()">&larr; Back</a>
    """
    return render_page("Get Help", body, demo, description="Open a support ticket with the WOLVES alliance team.")


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
            body = {"name": thread_name, "message": {"content": ai_helper.escalate_mention(), "embeds": [embed]}}
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
                f"{DISCORD_API}/channels/{thread_id}/messages",
                json={"content": ai_helper.escalate_mention(), "embeds": [embed]},
                headers=headers,
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


async def _get_week_stats(guild_id) -> dict:
    """Live countdown, weekly compliance, and leaderboard data for the
    donation form's tabs, computed with the exact same Saturday-aligned
    week window the bot's DONATION GUIDE post uses (see
    sheets.current_week_window). The three sheet reads that don't depend
    on each other (compliance, this week's board, all-time board) run
    concurrently to keep page load snappy. Returns a dict with every value
    defaulted to empty/None on failure (e.g. sheet briefly unreachable) so
    the page still renders fine, just without that tab's data."""
    empty = {
        "days_left": None, "compliant": None, "total_members": None,
        "start": None, "end": None, "top_donors": [], "all_time_donors": [], "compliant_names": [],
    }
    try:
        start, end = sheets.current_week_window()
        config = await server_config.get_config(guild_id)

        compliance_detail, top_donors, all_time_donors = await asyncio.gather(
            sheets.get_weekly_compliance_detail(
                config["donations_sheet_id"], start, end,
                donations_tab_name=config["donations_tab_name"],
                members_tab_name=config["alliance_members_tab_name"],
                minimum=WEEKLY_MINIMUM_PER_RESOURCE,
            ),
            sheets.get_weekly_top_donors(
                config["donations_sheet_id"], start, end,
                donations_tab_name=config["donations_tab_name"], limit=10,
            ),
            sheets.get_top_donors(
                config["donations_sheet_id"],
                summary_tab_name=config.get("donation_summary_tab_name", DONATION_SUMMARY_TAB_NAME),
                limit=10,
            ),
            return_exceptions=True,
        )

        if isinstance(compliance_detail, Exception):
            print(f"[webapp] compliance tab unavailable: {compliance_detail!r}")
            compliant_names, total_members = [], None
        else:
            compliant_names, total_members = compliance_detail

        if isinstance(top_donors, Exception):
            print(f"[webapp] weekly leaderboard tab unavailable: {top_donors!r}")
            top_donors = []

        if isinstance(all_time_donors, Exception):
            print(f"[webapp] all-time leaderboard tab unavailable: {all_time_donors!r}")
            all_time_donors = []

        return {
            "days_left": sheets.days_until_reset(),
            "compliant": len(compliant_names),
            "total_members": total_members,
            "start": start,
            "end": end,
            "top_donors": top_donors,
            "all_time_donors": all_time_donors,
            "compliant_names": compliant_names,
        }
    except Exception as e:
        print(f"[webapp] couldn't load weekly stats: {e!r}")
        return empty


def _demo_week_stats() -> dict:
    """Fun, static sample numbers for /demo links -- no real sheet is ever
    touched for a demo session, so every tab still needs something
    plausible to show."""
    start, end = sheets.current_week_window()
    weekly = [
        ("vanilla eve", 84_000_000.0), ("Hecate83", 20_000_000.0), ("Quackyz", 44.0),
        ("Wren", 12_500_000.0), ("Ser Roderick", 9_000_000.0),
    ]
    return {
        "days_left": 6, "compliant": 2, "total_members": 122,
        "start": start, "end": end,
        "top_donors": weekly,
        "all_time_donors": [
            ("vanilla eve", 612_000_000.0), ("Hecate83", 340_500_000.0), ("Wren", 198_200_000.0),
            ("Ser Roderick", 150_000_000.0), ("Quackyz", 88_400_000.0),
        ],
        "compliant_names": ["vanilla eve", "Hecate83"],
    }


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
      <a class="btn-link" href="/demo">🧪 Try a safe demo of the donation flow</a>
    """
    return web.Response(
        text=render_page(
            "WOLVES Alliance Bank",
            body,
            demo=False,
            description="Log alliance bank donations and support tickets for WOLVES | BRAVIA 3953.",
        ),
        content_type="text/html",
    )


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

    stats = _demo_week_stats() if demo else await _get_week_stats(payload.get("g"))

    return web.Response(
        text=render_form(
            payload, demo, token,
            days_left=stats["days_left"], compliant=stats["compliant"], total_members=stats["total_members"],
            top_donors=stats.get("top_donors"), week_start=stats.get("start"), week_end=stats.get("end"),
            all_time_donors=stats.get("all_time_donors"), compliant_names=stats.get("compliant_names"),
        ),
        content_type="text/html",
    )


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

    if not donated or not has_file:
        error = (
            "Enter at least one resource amount above 0."
            if not donated
            else "Please attach your Assistance Report screenshot."
        )
        stats = _demo_week_stats() if demo else await _get_week_stats(payload.get("g"))
        return web.Response(
            text=render_form(
                payload, demo, token, error=error,
                days_left=stats["days_left"], compliant=stats["compliant"], total_members=stats["total_members"],
                top_donors=stats.get("top_donors"), week_start=stats.get("start"), week_end=stats.get("end"),
                all_time_donors=stats.get("all_time_donors"), compliant_names=stats.get("compliant_names"),
            ),
            content_type="text/html",
        )

    file_bytes = proof.file.read()
    filename = proof.filename or "proof.png"
    display_name = payload.get("n", "Donor")
    guild_id = payload.get("g")
    channel_id = payload.get("c")

    if demo:
        # Safe preview: don't touch the real sheet or Discord. Show the
        # amounts just entered as this week's "progress" so the demo still
        # showcases the progress bars realistically.
        print(f"[webapp][DEMO] would log for guild={guild_id}: {display_name} -> {donated}")
        demo_progress = {k: resources.get(k, 0) for k in sheets.RESOURCE_COLUMNS}
        return web.Response(
            text=render_success(payload, donated, demo=True, weekly_totals=demo_progress),
            content_type="text/html",
        )

    # --- Real path ---
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

    weekly_totals = None
    try:
        start, end = sheets.current_week_window()
        weekly_totals = await sheets.get_donor_weekly_progress(
            config["donations_sheet_id"], display_name, start, end,
            donations_tab_name=config["donations_tab_name"],
        )
    except Exception as e:
        print(f"[webapp] couldn't load post-donation weekly progress: {e!r}")

    return web.Response(
        text=render_success(payload, donated, demo=False, weekly_totals=weekly_totals),
        content_type="text/html",
    )


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
    # Serves static images (e.g. the DONATION GUIDE's banner and example
    # screenshot) from a stable URL on our own domain. Discord embeds that
    # reference images this way (a plain https:// URL) never expire and --
    # unlike attachment://-based embed images -- don't trigger Discord's
    # known bug where editing a message later makes the image render twice
    # (see https://github.com/discord/discord-api-docs/issues/5675).
    app.router.add_static("/assets/", path=os.path.join(os.path.dirname(__file__), "assets"), name="assets")
    return app



if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(build_app(), host="0.0.0.0", port=port)
