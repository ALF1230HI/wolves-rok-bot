# WOLVES | BRAVIA 3953 — Rise of Kingdoms Alliance Bot

A Discord bot for the WOLVES 🐺 | BRAVIA 3953 alliance: resource shop, KvK announcements, and general alliance announcements — all with the red "WOLVES" branding.

## Commands

| Command | Who can use it | Description |
|---|---|---|
| `/ping` | Everyone | Health check — confirms the bot is online |
| `/shop` | Everyone | Posts the current resource shop price list |
| `/donate` | Everyone | Logs a resource donation (Food/Wood/Stone/Gold) to the Alliance Bank Donations Tracker Google Sheet — requires a proof screenshot that must show a transport to the alliance bank |
| `/announce` | Manage Messages permission | Posts a custom red-branded announcement (title + message) |
| `/kvk` | Everyone | Shows the next KvK date/time converted to your own timezone, using live TimeAPI.io conversion |

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

## 4. Set up /donate (Google Sheets integration)

`/donate` writes a new row to the **Alliance Bank Donations Tracker** Google Sheet every time
a member logs a donation. This requires a free Google Cloud **service account** — a robot
account Google issues for exactly this kind of automation.

1. Go to https://console.cloud.google.com/ and create a new project (any name).
2. In the search bar, search for **"Google Sheets API"** → open it → click **Enable**.
3. Go to **APIs & Services → Credentials** → **Create Credentials → Service Account**.
   Give it any name, click through the defaults, then **Done**.
4. Click into the new service account → **Keys** tab → **Add Key → Create new key → JSON**.
   This downloads a `.json` credentials file.
5. Save that file in the bot's folder as `google_credentials.json` (or set
   `GOOGLE_CREDENTIALS_PATH` in `.env` to wherever you put it). **Never commit this file** —
   it's already excluded via `.gitignore`.
6. Open the downloaded JSON and copy the `client_email` value
   (looks like `xxxx@xxxx.iam.gserviceaccount.com`).
7. Open your Google Sheet → click **Share** → paste that email → give it **Editor** access → Send.
8. In `.env`, set:
   ```
   GOOGLE_CREDENTIALS_PATH=google_credentials.json
   DONATIONS_SHEET_ID=1spszkPihGZ9IIbe_v3Q2KtEai2awX1JITRxE09EzY4E
   DONATIONS_TAB_NAME=Donations
   ```
   (`DONATIONS_SHEET_ID` is the long ID from the sheet's URL; `DONATIONS_TAB_NAME` is the tab
   at the bottom of the spreadsheet — check it matches exactly, including capitalization.)

**On Railway**: you can't upload the JSON file directly, so instead paste its entire contents
into a `GOOGLE_CREDENTIALS_JSON` environment variable in Railway's **Variables** tab:

1. Open your downloaded `google_credentials.json` file in a text editor.
2. Copy the **entire contents** (the whole `{ ... }` JSON blob, all on however many lines it is).
3. In Railway → your project → **Variables** tab → **New Variable**.
4. Name: `GOOGLE_CREDENTIALS_JSON`. Value: paste the whole JSON blob in.
5. Save — Railway will redeploy automatically. The bot checks `GOOGLE_CREDENTIALS_JSON` first
   and uses it if present, falling back to a `google_credentials.json` file otherwise (which is
   what happens automatically when running locally).

The sheet's expected column layout (row 1 headers) is:

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| Date | Alliance Member | Food | Wood | Stone | Gold |

### Proof screenshot verification

`/donate` requires members to attach a screenshot of their in-game **Assistance Report**
(Alliance → Assistance → Assistance Report). The bot uses OCR (`pytesseract`) plus simple
icon-color classification to:

1. Scan every entry in the screenshot's history that was sent to the alliance bank (not just
   the newest one — older entries further down the list count too).
2. Read each entry's resource type (Food/Wood/Stone/Gold, based on the icon's color) and amount.
3. For each resource the member typed, check whether any bank entry of that type in the
   screenshot matches the amount (within a small tolerance for OCR noise).

Donations are rejected if: no entry was sent to the bank at all, a typed resource has no
matching bank entry of that type, or none of the bank entries of that type have a matching
amount. Since each report row only shows one resource, a multi-resource `/donate` (e.g.
`food` + `gold` in one call) is verified by finding a matching bank entry for each resource
independently — they don't need to be adjacent or the newest entries.

- The bank's exact in-game name is set via the `BANK_NAME` environment variable
  (default: `KD Bank 53`). If the bank is ever renamed, update this variable — no code
  changes needed.
- This requires the `tesseract-ocr` system package to be installed (not just the Python
  library). Locally: `sudo apt-get install tesseract-ocr` (Debian/Ubuntu) or the equivalent
  for your OS. **On Railway**, this is already handled — `railpack.json` in this repo tells
  Railway's build system to install `tesseract-ocr` automatically, so no extra setup is
  needed there.
- OCR isn't perfect — very blurry or unusually cropped screenshots can occasionally be
  misread. If a member is incorrectly rejected despite a valid donation, an officer can log
  it manually in the sheet.

## 5. Customizing

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
5. Go to your Railway project → **Variables** tab → add every variable the bot needs. At minimum:
   - `DISCORD_BOT_TOKEN` = your bot token
   - `GUILD_ID` = your server ID (optional, for instant command sync)
   - `GOOGLE_CREDENTIALS_JSON` = the full contents of your service account JSON key file
     (see the `/donate` setup section above) — required for `/donate` to work on Railway.
   - `DONATIONS_SHEET_ID` and `DONATIONS_TAB_NAME` only if you want to override the defaults
     already baked into `sheets.py`.

   **This step is the #1 reason a Railway deploy "doesn't sync commands" or crashes on
   startup** — if `DISCORD_BOT_TOKEN` is missing/wrong, the bot can't log in at all, so it
   never gets to the point of syncing slash commands.
6. Under **Settings → Deploy**, Railway should already be set to auto-redeploy whenever you
   push to the `main` branch on GitHub. Every `git push` from now on will trigger a fresh
   deploy automatically — no manual redeploy needed.
7. Check the **Deploy Logs** tab (or **View Logs**) — you should see:
   ```
   Logged in as ... 
   Synced N slash command(s).
   ```
   If you instead see a crash/traceback, the log will usually point straight at a missing
   environment variable or a `ModuleNotFoundError` (usually fixed by making sure
   `requirements.txt` lists everything the bot imports).
8. Done — the bot now runs 24/7 on Railway's servers, independent of your computer, this chat
   session, or whether your PC is on or off. You should be able to close your laptop and the
   bot stays online.

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
