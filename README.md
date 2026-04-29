# 📰 WhatsApp Group Scraper

A robust, automated WhatsApp bot that monitors a specific group for a daily newspaper PDF, downloads it, renames it with a clean Spanish date format, and forwards it to a Telegram chat. Built with Python, [neonize](https://github.com/krypton-byte/neonize) (WhatsApp), and [Telethon](https://github.com/LonamiWebs/Telethon) (Telegram).

## ✨ Features

* **Targeted Monitoring:** Scans specific WhatsApp groups for files matching a keyword (e.g., "La Provincia Las Palmas").
* **Smart Renaming:** Automatically converts raw filenames into a clean format (e.g., `La Provincia, 16 de Marzo.pdf`).
* **Telegram Delivery:** Forwards the downloaded PDF to a configured Telegram chat.
* **Resilient Downloading:** Uses a 3-tier fallback strategy (Raw Message → Pointer → Low-Level Decryption) to handle WhatsApp download issues.
* **Daily Lockdown:** Creates a persistent `last_sent.txt` log to ensure the file is only forwarded once per day, even if the script restarts.
* **Quiet Hours:** Ignores files sent before 7:00 AM to avoid premature triggers.
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
SEARCH_TERM=La Provincia Las Palmas

# Telegram delivery config
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE_NUMBER=+34600000000
TELEGRAM_NEWSPAPERS_CHAT_NAME=Your Chat Name
TELEGRAM_NEWSPAPERS_CHAT_ID=1234567890
TELEGRAM_SESSION_PATH=telegram_session

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

## 💻 Local Development

1. **Install dependencies:**
   ```bash
   pip install neonize python-dotenv telethon
   ```

2. **Configure `.env`** — copy `.env.example` to `.env` and fill in your values.

3. **Run:**
   ```bash
   python scraper.py                  # Production mode
   python scraper.py --skip-date-check  # Dev mode (bypasses daily limit)
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