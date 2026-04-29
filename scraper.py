import os
import sys
import time
import argparse
from datetime import datetime, date
from dotenv import load_dotenv
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv
from neonize.utils import build_jid
from telethon.sync import TelegramClient
from naming_utils import get_newspaper_name

# --- CONFIGURATION (Loaded from .env) ---
load_dotenv()

TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID")
SEARCH_TERM = os.getenv("SEARCH_TERM", "La Provincia Las Palmas")

# Telegram config
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE_NUMBER = os.getenv("TELEGRAM_PHONE_NUMBER", "")
TELEGRAM_NEWSPAPERS_CHAT_ID = os.getenv("TELEGRAM_NEWSPAPERS_CHAT_ID", "")
TELEGRAM_NEWSPAPERS_CHAT_NAME = os.getenv("TELEGRAM_NEWSPAPERS_CHAT_NAME", "")
TELEGRAM_SESSION_PATH = os.getenv("TELEGRAM_SESSION_PATH", "telegram_session")

DOWNLOAD_PATH = "downloads"
SENT_LOG_FILE = "last_sent.txt"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# --- CLI ARGUMENTS ---
parser = argparse.ArgumentParser(description="WhatsApp newspaper scraper")
parser.add_argument(
    "--skip-date-check",
    action="store_true",
    default=os.getenv("SKIP_DATE_CHECK", "false").lower() in ("true", "1", "yes"),
    help="Skip the once-a-day check (useful for development)"
)
args = parser.parse_args()
SKIP_DATE_CHECK = args.skip_date_check

# --- STATE TRACKING ---
PROCESSED_MESSAGES = set()
LAST_SENT_DATE = None

if os.path.exists(SENT_LOG_FILE):
    with open(SENT_LOG_FILE, "r") as f:
        LAST_SENT_DATE = f.read().strip()

client = NewClient("session.db")


def already_sent_today():
    """Check if we already sent today's paper (respects skip flag)."""
    if SKIP_DATE_CHECK:
        return False
    return str(date.today()) == LAST_SENT_DATE


def save_sent_date():
    today_str = str(date.today())
    with open(SENT_LOG_FILE, "w") as f:
        f.write(today_str)
    return today_str


def _resolve_telegram_chat(tg_client):
    """Resolve the target Telegram chat: use ID if available, otherwise search by name."""
    if TELEGRAM_NEWSPAPERS_CHAT_ID:
        chat_id = int(TELEGRAM_NEWSPAPERS_CHAT_ID)
        print(f"📌 Using chat ID: {chat_id}")
        return chat_id

    # Fallback: search by name
    for dialog in tg_client.iter_dialogs():
        if TELEGRAM_NEWSPAPERS_CHAT_NAME in dialog.name:
            print(f"📌 Found chat by name: {dialog.name} (ID: {dialog.id})")
            return dialog.id

    return None


def send_to_telegram(file_path, custom_name):
    """Send the downloaded newspaper PDF to the Telegram newspapers chat."""
    print(f"📤 Sending '{custom_name}' to Telegram...")
    tg_client = None
    try:
        tg_client = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        tg_client.start(phone=TELEGRAM_PHONE_NUMBER)

        target_chat = _resolve_telegram_chat(tg_client)
        if target_chat is None:
            print(f"❌ Could not find Telegram chat '{TELEGRAM_NEWSPAPERS_CHAT_NAME}'")
            return False

        tg_client.send_file(target_chat, file_path, caption=custom_name)
        print(f"🚀 Sent to Telegram successfully!")
        return True
    except Exception as e:
        print(f"❌ Failed to send to Telegram: {e}")
        return False
    finally:
        if tg_client:
            tg_client.disconnect()


def download_file(client, message_ev):
    global LAST_SENT_DATE
    doc = message_ev.Message.documentMessage
    ts = message_ev.Info.Timestamp
    if ts > 9999999999: ts /= 1000
    msg_date = datetime.fromtimestamp(ts).date()

    custom_name = get_newspaper_name(msg_date)
    path = os.path.join(DOWNLOAD_PATH, custom_name)

    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"📦 File already exists locally. Attempting to send...")
        if send_to_telegram(path, custom_name):
            LAST_SENT_DATE = save_sent_date()
            return True

    for cycle in range(1, 4):
        print(f"⏳ --- STARTING METHOD CYCLE {cycle}/3 ---")

        strategies = [
            ("2 (Raw Message)", lambda: client.download_any(message_ev.Message)),
            ("1 (Pointer)", lambda: client.download_any(doc)),
            ("3 (Low-level)", lambda: client.download_media(doc.url, doc.directPath, doc.mediaKey,
                                                            doc.fileEncSha256, doc.fileSha256, "document"))
        ]

        for name, strategy_func in strategies:
            data = None
            print(f"🔍 Trying Strategy {name}...")
            try:
                data = strategy_func()
                if data:
                    with open(path, "wb") as f:
                        f.write(data)
                    print(f"✅ Download successful via Strategy {name}.")

                    if send_to_telegram(path, custom_name):
                        LAST_SENT_DATE = save_sent_date()
                        return True
                    else:
                        if os.path.exists(path): os.remove(path)
                        return False
            except Exception as e:
                print(f"⚠️ Strategy {name} failed: {e}")

        if cycle < 3:
            time.sleep(5)

    return False


@client.event(MessageEv)
def on_message(client: NewClient, message: MessageEv):
    global LAST_SENT_DATE
    try:
        # Stop processing if we already sent today's paper
        if already_sent_today():
            return

        msg_id = message.Info.ID
        if msg_id in PROCESSED_MESSAGES: return
        PROCESSED_MESSAGES.add(msg_id)

        chat_info = message.Info.MessageSource.Chat
        current_chat_id = f"{chat_info.User}@{chat_info.Server}"

        ts = message.Info.Timestamp
        if ts > 9999999999: ts /= 1000
        msg_dt = datetime.fromtimestamp(ts)

        if current_chat_id == TARGET_GROUP_ID:
            msg_obj = message.Message
            if hasattr(msg_obj, "documentMessage"):
                file_name = msg_obj.documentMessage.fileName or ""

                if SEARCH_TERM.lower() in file_name.lower() and msg_dt.date() == date.today():
                    if msg_dt.hour < 7:
                        print(f"🕒 File detected early ({msg_dt.strftime('%H:%M')}), waiting until 07:00...")
                        return

                    sender = getattr(message.Info, "PushName", "Someone")
                    print(f"\n🎯 TARGET DETECTED from {sender}: {file_name}")
                    download_file(client, message)
    except Exception as e:
        print(f"⚠️ Error in on_message: {e}")


@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    mode = "DEV (skip-date-check)" if SKIP_DATE_CHECK else "PRODUCTION"
    print(f"🚀 Monitoring Group: {TARGET_GROUP_ID} [{mode}]")
    status = "Pending" if not already_sent_today() else "Completed"
    print(f"📅 Today's status: {status}")
    print(f"📨 Telegram target: {TELEGRAM_NEWSPAPERS_CHAT_NAME}")


if __name__ == "__main__":
    try:
        client.connect()
    except KeyboardInterrupt:
        print("\n👋 Shutting down safely...")
        sys.exit(0)