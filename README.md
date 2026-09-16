# WOLVES | BRAVIA 3953 — Rise of Kingdoms Alliance Bot

A Discord bot for the WOLVES 🐺 | BRAVIA 3953 alliance: resource shop, KvK announcements, and general alliance announcements — all with the red "WOLVES" branding.

## Commands

| Command | Who can use it | Description |
|---|---|---|
| `/ping` | Everyone | Health check — confirms the bot is online |
| `/shop` | Everyone | Posts the current resource shop price list |
| `/announce` | Manage Messages permission | Posts a custom red-branded announcement (title + message) |
| `/kvk` | Manage Messages permission | Posts a KvK date/time announcement, auto-converted into UTC, US East, US West, UK, Central Europe, and Singapore time |

## 1. Create the Discord Application (one-time, done by you on discord.com)

1. Go to https://discord.com/developers/applications
2. Click **New Application**, name it (e.g. "WOLVES BRAVIA 3953")
3. Go to the **Bot** tab → **Add Bot**
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Click **Reset Token** → copy it. This is your `DISCORD_BOT_TOKEN` — never share it publicly, treat it like a password.
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Use Slash Commands`, `Read Message History`
   - Copy the generated URL, open it in a browser, and invite the bot to your server.

## 2. Configure

```bash
cd rok_bot
cp .env.example .env
```

Edit `.env` and paste in your bot token:

```
DISCORD_BOT_TOKEN=your-actual-token-here
GUILD_ID=your-server-id   # optional, makes slash commands appear instantly while testing
```

To get your server (guild) ID: enable Developer Mode in Discord (User Settings → Advanced), then right-click your server icon → Copy Server ID.

## 3. Install & Run

```bash
pip install -r requirements.txt
python bot.py
```

If it connects successfully you'll see:

```
Logged in as WOLVES BRAVIA 3953#1234 (ID: ...)
Synced N slash command(s).
```

Slash commands appear instantly if `GUILD_ID` is set; otherwise global commands can take up to an hour to show the first time.

## 4. Customizing

- **Shop prices**: edit the `SHOP_ITEMS` list near the top of `bot.py`.
- **PayPal emoji**: update `PAYPAL_EMOJI` with your server's custom emoji (right-click the emoji in Discord → Copy ID, or type `\:emojiname:` in a Discord message and send it to see its raw code).
- **Branding color**: change the `RED` constant (hex color, e.g. `0xFF0000`).
- **Timezones shown for `/kvk`**: edit the `TIMEZONES` list.

## Hosting

This bot needs to run continuously to respond to slash commands. Recommended: **Railway** (free tier, simplest setup for a background worker like this).

### Deploy to Railway (recommended)

1. Create a free account at https://railway.app (you can sign up with GitHub).
2. Push this `rok_bot` folder to a new GitHub repository (private is fine):
   ```bash
   cd rok_bot
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```
   (The `.env` file is excluded automatically via `.gitignore` — never commit your bot token.)
3. In Railway: **New Project → Deploy from GitHub repo** → select your repo.
4. Railway auto-detects Python and installs `requirements.txt`. It will look for a start command —
   this project includes a `Procfile` (`worker: python3 bot.py`) so Railway knows how to run it.
5. Go to your Railway project → **Variables** tab → add:
   - `DISCORD_BOT_TOKEN` = your bot token
   - `GUILD_ID` = your server ID (optional, for instant command sync)
6. Deploy. Check the **Deploy Logs** tab — you should see:
   ```
   Logged in as ... 
   Synced N slash command(s).
   ```
7. Done — the bot now runs 24/7 independent of your computer or this chat session.

**Note on the database:** this bot stores governor stats in a local SQLite file (`data/stats.db`).
Railway's filesystem is ephemeral on redeploys — if you redeploy, existing stat history may reset. For
serious long-term stat tracking, consider upgrading later to a hosted database (Railway offers a free
Postgres add-on) — ask me if you want that upgrade.

### Alternative: Render, a VPS, or your own PC

- **Render**: same idea — new "Background Worker" service, connect your GitHub repo, set the same environment variables, deploy.
- **Your own PC**: just leave a terminal open running `python bot.py` (only works while your PC is on and awake).
- **A cheap VPS** (e.g. DigitalOcean, Hetzner): SSH in, clone the repo, install Python, run the bot inside a `tmux`/`screen` session or as a `systemd` service so it survives reboots.

## Security Note

Never commit or share your `.env` file or bot token. If it's ever leaked, regenerate it immediately from the Discord Developer Portal (Bot tab → Reset Token) and update the token in Railway's Variables tab.
