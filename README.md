# 📰 WhatsApp Group Scraper

A robust, automated WhatsApp bot that monitors a specific group for a daily newspaper PDF, downloads it, renames it with a clean Spanish date format, and forwards it to a Telegram chat. Built with Python, [neonize](https://github.com/krypton-byte/neonize) (WhatsApp), and [Telethon](https://github.com/LonamiWebs/Telethon) (Telegram).

## ✨ Features

* **Multi-Newspaper Monitoring:** Scans specific WhatsApp groups for multiple target newspapers (e.g. `La Provincia`, `Canarias7`, `El País`), each independently tracked.
* **Interactive Telegram Bot Commands:** Use `/status` to check today's progress, `/list` to view active target newspapers, `/add` or `/remove` to change newspapers in real time, and `/help` for command list.
* **Telegram QR Login Notifications:** Automatically renders WhatsApp QR authentication codes as PNG images and sends them directly to your Admin Telegram chat so you can log in without accessing container logs.
* **Telegram Error & Disconnect Alerts:** Sends instant alert notifications to your Admin Telegram chat if WhatsApp requires re-authentication or encounters download issues.
* **Smart Renaming:** Automatically converts raw filenames into a clean format (e.g., `La Provincia, 16 de Marzo.pdf`).
* **Telegram Delivery:** Forwards the downloaded PDF to a configured Telegram chat.
* **Day Header:** Automatically sends a date marker (e.g., `# 1 de Mayo`) to the Telegram chat on the first send of each day, matching the `newspapers_telegram_bot` style.
* **Duplicate Detection:** Before sending, checks the last 10 messages in the Telegram chat — if the file was already sent today, it skips and treats it as success.
* **Resilient Downloading:** Uses a 3-tier fallback strategy (Raw Message → Pointer → Low-Level Decryption) to handle WhatsApp download issues.
* **Daily Lockdown:** Tracks sent status per newspaper in a persistent `last_sent.json` log to ensure each paper is only forwarded once per day.
* **Dev Mode:** Use `SKIP_DATE_CHECK=true` or `--skip-date-check` to bypass the once-a-day restriction during development.

## 🐳 Docker / Unraid Deployment (Recommended)

### Folder Structure

```
/mnt/user/appdata/bots/
├── compose.yaml
├── .env                          # Shared or per-bot env config
└── whatsapp_group_scraper/       # This repo (git clone)
    ├── Dockerfile
    ├── scraper.py
    ├── naming_utils.py
    ├── session.db                # Created after WhatsApp QR scan
    └── telegram_session.session  # Created after Telegram auth
```

### 1. Clone the repo on Unraid

```bash
cd /mnt/user/appdata/bots
git clone https://github.com/spicymojo/whatsapp_group_scraper.git
```

### 2. Create your `.env` file

Create `.env` at the `bots/` level (or inside the project folder):

```ini
TARGET_GROUP_ID=120363402800142448@g.us

# Target newspapers (comma-separated search:name pairs, or JSON array)
TARGET_NEWSPAPERS=La Provincia Las Palmas:La Provincia, Canarias7:Canarias7, El Pais:El País

# Telegram Worker Account (Secondary number running the bot to upload files)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE_NUMBER=+34600000000
TELEGRAM_SESSION_PATH=telegram_session

# Telegram Bot Token (from @BotFather for interactive commands: /status, /list, /add, /remove)
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather

# Admin Account (Your main personal phone number or @username for QR login, error alerts)
TELEGRAM_ADMIN_CHAT=+34684059686

# Destination Channel / Group (Where daily newspaper PDFs are delivered)
TELEGRAM_NEWSPAPERS_CHAT_NAME=Prensa de Ivaj
TELEGRAM_NEWSPAPERS_CHAT_ID=1548654539

SKIP_DATE_CHECK=false
```

### 3. Compose config

In your `compose.yaml`:

```yaml
services:
  whatsapp-newspaper:
    build: ./whatsapp_group_scraper
    container_name: whatsapp-newspaper
    restart: unless-stopped
    stdin_open: true
    tty: true
    volumes:
      - ./whatsapp_group_scraper:/app
    environment:
      - PYTHONUNBUFFERED=1
      - PYTHONIOENCODING=utf-8
    env_file:
      - ./.env
```

### 4. First-time authentication

Both WhatsApp and Telegram need a one-time interactive login.

#### Telegram session (do this first)

Create the Telegram session by running a one-off interactive container:

```bash
docker compose run -it whatsapp-newspaper python -c "
from telethon.sync import TelegramClient
import os
c = TelegramClient('telegram_session', int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'])
c.start(phone=os.environ['TELEGRAM_PHONE_NUMBER'])
print('Session created!')
c.disconnect()
"
```

Enter the code Telegram sends you when prompted. The `telegram_session.session` file is saved in the volume.

#### WhatsApp session

Start the bot and scan the QR code from the Docker logs:

```bash
docker compose up -d --build
docker logs -f whatsapp-newspaper
```

Scan the QR code with WhatsApp → Settings → Linked Devices. The `session.db` file is saved in the volume.

After both one-time auths, the sessions are persisted — no need to re-authenticate unless they expire.

### 5. Updating the code

```bash
cd /mnt/user/appdata/bots/whatsapp_group_scraper
git pull
cd ..
docker compose down
docker compose up -d --build
```

### 6. Manual Retry

If the bot missed today's newspaper (e.g. a download failed), you can trigger a retry without restarting:

#### Option A: Restart with `--retry` flag

```bash
docker compose down
docker compose run whatsapp-newspaper python scraper.py --retry
```

This scans the last 50 messages in the target group for today's newspaper, processes it, and then keeps listening.

#### Option B: Send a signal to the running container (no restart)

```bash
docker exec whatsapp-newspaper kill -USR1 1
```

This sends a harmless signal that tells the bot to re-scan recent messages. The bot **keeps running** — nothing is stopped or restarted.

## 💻 Local Development

1. **Install dependencies:**
   ```bash
   pip install neonize python-dotenv telethon pytz
   ```

2. **Configure `.env`** — copy `.env.example` to `.env` and fill in your values.

3. **Run:**
   ```bash
   python scraper.py                    # Production mode
   python scraper.py --skip-date-check  # Dev mode (bypasses daily limit)
   python scraper.py --retry            # Retry: scan recent messages for today's paper
   ```

## 📂 File Structure

* `scraper.py` — Main bot logic, event listeners, and download/upload strategies.
* `naming_utils.py` — Helper to format newspaper names with Spanish dates.
* `Dockerfile` — Container image definition.
* `docker-compose.yml` — Standalone compose config (for running inside the project folder).
* `.env` — Private configuration (not committed to git).
* `last_sent.txt` — Tracks the date of the last successful forward.
* `downloads/` — Auto-created folder where PDFs are temporarily stored.

## ⚠️ Disclaimer

This project is for educational purposes and personal automation. Please ensure you comply with WhatsApp's Terms of Service regarding automated messaging.