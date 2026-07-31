# 📰 WhatsApp Group Scraper

A robust, automated WhatsApp bot that monitors a specific group for daily newspaper PDFs, downloads them, renames them with a clean Spanish date format, and forwards them to a Telegram channel. Built with Python, [neonize](https://github.com/krypton-byte/neonize) (WhatsApp), and [Telethon](https://github.com/LonamiWebs/Telethon) (Telegram).

## ✨ Features

* **Multi-Newspaper & Alias Support:** Scans WhatsApp groups for multiple target newspapers (e.g. `La Provincia`, `Canarias7`, `El País`). Each newspaper supports multiple comma-separated search aliases (e.g., `La Provincia:La Provincia, La Provincia Las Palmas`).
* **Plan B Manual Fallbacks (Telegram & WhatsApp):** If group scraping misses a paper, upload the PDF directly to `@IvajNewspapersBot` on Telegram or DM your WhatsApp account.
  * **Telegram Plan B:** Auto-detects newspapers or presents interactive inline buttons (`[ 📰 La Provincia ]`, `[ 📰 Marca ]`) for 1-tap force matching. Features real-time download percentage progress updates.
  * **WhatsApp Plan B:** Auto-detects newspapers or sends a numbered text selection menu (`1. La Provincia`, `2. Marca`). Simply reply `1` or `Marca` to deliver.
* **Dual Telegram Engine Architecture:** Combines a Telegram Bot (`TELEGRAM_BOT_TOKEN`) for commands/uploads and a Telegram User Account (`TELEGRAM_PHONE_NUMBER`) for channel history inspection (`iter_messages`) and posting, bypassing Telegram Bot API history restrictions.
* **Pre-Download Duplicate Check:** Checks `last_sent.json` and Telegram channel history *before* downloading any file bytes. If the paper was already delivered today by any bot or script, it skips downloading entirely and reports: `Already sent today, skipping.`
* **Local Storage Auto-Cleanup:** Automatically deletes PDF files from local disk immediately after successful channel delivery to keep disk space clean.
* **Interactive Bot Commands (Telegram & WhatsApp DM):** Works identically on both platforms:
  * `/status` — View today's delivery status for all configured newspapers
  * `/list` — List all active target newspapers & search aliases
  * `/add DisplayName:alias1, alias2` — Add or update a target newspaper with aliases
  * `/remove DisplayName` — Remove a newspaper from the active list
  * `/help` — Display command usage and manual upload instructions
* **Telegram QR Login & Alert Notifications:** Automatically renders WhatsApp QR authentication codes as PNG images and sends them to your Admin Telegram chat for containerless scanning. Sends instant alerts for disconnects or errors.
* **Smart Renaming:** Automatically converts raw filenames into a clean format (e.g., `La Provincia, 31 de Julio.pdf`).
* **Day Header:** Sends a date marker (e.g., `# 31 de Julio`) once per day, matching `newspapers_telegram_bot` style. Only sent if no header exists for today.
* **Resilient Downloading:** Uses a 3-tier fallback strategy (Raw Message → Pointer → Low-Level Decryption) for WhatsApp downloads.
* **Dev Mode:** Set `SKIP_DATE_CHECK=true` or pass `--skip-date-check` to bypass once-a-day restrictions during testing.

## 🐳 Docker / Unraid Deployment (Recommended)

### Folder Structure

```text
/mnt/user/appdata/bots/
├── compose.yaml
├── .env                          # Shared or per-bot env config
└── whatsapp_group_scraper/       # This repo (git clone)
    ├── Dockerfile
    ├── scraper.py
    ├── naming_utils.py
    ├── config_newspapers.json    # Target newspapers & search aliases
    ├── last_sent.json            # Persistent delivery tracking log
    ├── session.db                # Created after WhatsApp QR scan
    └── telegram_session.session  # Created after Telegram auth
```

### 1. Clone the repo on Unraid

```bash
cd /mnt/user/appdata/bots
git clone https://github.com/spicymojo/whatsapp_group_scraper.git
```

### 2. Create your `.env` file

```ini
TARGET_GROUP_ID=120363402800142448@g.us

# Target newspapers configuration (name:alias1, alias2)
TARGET_NEWSPAPERS=La Provincia:La Provincia, La Provincia Las Palmas; Canarias7:Canarias7; El País:El Pais, El País; Marca:Marca

# Telegram Worker Account (Secondary number running the bot to upload files)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE_NUMBER=+34600000000
TELEGRAM_SESSION_PATH=telegram_session

# Telegram Bot Token (from @BotFather for interactive commands & Plan B uploads)
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather

# Admin Account (Your main personal phone number or @username for QR login & error alerts)
TELEGRAM_ADMIN_CHAT=+34600000000

# Destination Channel / Group (Where daily newspaper PDFs are delivered)
TELEGRAM_NEWSPAPERS_CHAT_NAME=Prensa de Ivaj
TELEGRAM_NEWSPAPERS_CHAT_ID=-1001548654539

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

#### Telegram session (do this first)

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

Enter the code Telegram sends you when prompted.

#### WhatsApp session

```bash
docker compose up -d --build
docker logs -f whatsapp-newspaper
```

Scan the QR code with WhatsApp → Linked Devices.

### 5. Manual Retry / Plan B

* **Plan B (Telegram):** Upload any PDF directly to `@IvajNewspapersBot`.
* **Plan B (WhatsApp):** Send any PDF directly to your WhatsApp account (DM).
* **Runtime Retry:** Trigger a group re-scan without restarting:
  ```bash
  docker exec whatsapp-newspaper kill -USR1 1
  ```

## 💻 Local Development

1. **Install dependencies:**
   ```bash
   pip install neonize python-dotenv telethon pytz qrcode pillow
   ```

2. **Configure `.env`** — copy `.env.example` to `.env` and fill in your values.

3. **Run:**
   ```bash
   python scraper.py                    # Production mode
   python scraper.py --skip-date-check  # Dev mode (bypasses daily limit)
   python scraper.py --retry            # Retry: scan recent messages for today's paper
   ```

## 📂 File Structure

* `scraper.py` — Main bot logic, event listeners, dual client engine, and download/upload strategies.
* `naming_utils.py` — Helper to format newspaper names with Spanish dates.
* `config_newspapers.json` — Persisted active newspaper names and search aliases.
* `last_sent.json` — Persistent delivery log tracking sent dates.
* `Dockerfile` — Container image definition.
* `docker-compose.yml` — Compose configuration.
* `.env` — Private configuration file.

## ⚠️ Disclaimer

This project is for educational purposes and personal automation. Please ensure you comply with WhatsApp's Terms of Service regarding automated messaging.