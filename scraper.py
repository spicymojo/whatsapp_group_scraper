import os
import sys
import time
import signal
import argparse
import pytz
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
parser.add_argument(
    "--retry",
    action="store_true",
    help="Manually retry: scan recent group messages for today's newspaper"
)
args = parser.parse_args()
SKIP_DATE_CHECK = args.skip_date_check
RETRY_MODE = args.retry

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
    """Resolve the target Telegram chat by searching through dialogs."""
    target_id = int(TELEGRAM_NEWSPAPERS_CHAT_ID) if TELEGRAM_NEWSPAPERS_CHAT_ID else None

    for dialog in tg_client.iter_dialogs():
        if target_id and dialog.id == target_id:
            print(f"📌 Found chat by ID: {dialog.name} ({dialog.id})")
            return dialog
        if TELEGRAM_NEWSPAPERS_CHAT_NAME and TELEGRAM_NEWSPAPERS_CHAT_NAME in dialog.name:
            print(f"📌 Found chat by name: {dialog.name} (ID: {dialog.id})")
            return dialog

    return None


def _pretty_print_date(dt):
    """Format a date in Spanish like the newspapers_telegram_bot: '1 de Mayo'."""
    months = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
              "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")
    return f"{dt.day} de {months[dt.month - 1]}"


def _send_day_header(tg_client, chat):
    """Send a day marker message if none has been sent today (like newspapers_telegram_bot)."""
    tz = pytz.timezone('Atlantic/Canary')
    now = datetime.now(tz)
    messages = tg_client.get_messages(chat, limit=10)
    for msg in messages:
        if msg.date and now.date() == msg.date.astimezone(tz).date():
            text = getattr(msg, "message", "") or ""
            if "#" in text:
                print("📅 Day header already exists, skipping.")
                return
    header = "# " + _pretty_print_date(now)
    tg_client.send_message(chat, header)
    print(f"📅 Sent day header: {header}")


def _file_already_sent_today(tg_client, chat, custom_name):
    """Check if a file with this name was already sent to the chat today."""
    tz = pytz.timezone('Atlantic/Canary')
    now = datetime.now(tz)
    messages = tg_client.get_messages(chat, limit=10)
    for msg in messages:
        if msg.date and now.date() == msg.date.astimezone(tz).date():
            if msg.file and msg.file.name:
                sent_name = msg.file.name.split(",")[0].strip()
                if sent_name == custom_name.replace(".pdf", "").split(",")[0].strip():
                    return True
                # Also check exact filename match
                if msg.file.name == custom_name:
                    return True
    return False


def send_to_telegram(file_path, custom_name):
    """Send the downloaded newspaper PDF to the Telegram newspapers chat.
    Sends a day header if it's the first message of the day.
    Skips sending if the file was already sent today."""
    print(f"📤 Sending '{custom_name}' to Telegram...")
    tg_client = None
    try:
        tg_client = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        tg_client.start(phone=TELEGRAM_PHONE_NUMBER)

        target_chat = _resolve_telegram_chat(tg_client)
        if target_chat is None:
            print(f"❌ Could not find Telegram chat '{TELEGRAM_NEWSPAPERS_CHAT_NAME}'")
            return False

        # Check if already sent today (avoid duplicates)
        if _file_already_sent_today(tg_client, target_chat, custom_name):
            print(f"✅ '{custom_name}' already sent today, skipping.")
            return True  # Treat as success — no need to re-send

        # Send day header if first message of the day
        _send_day_header(tg_client, target_chat)

        tg_client.send_file(target_chat, file_path)
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

    # Log all groups to help discover group IDs
    print("\n📋 Your WhatsApp groups:")
    try:
        groups = client.get_joined_groups()
        for g in groups:
            gid = f"{g.JID.User}@{g.JID.Server}"
            name = g.GroupName.Name if hasattr(g.GroupName, 'Name') else 'Unknown'
            print(f"   • {name} → {gid}")
    except Exception as e:
        print(f"   ⚠️ Could not list groups: {e}")
    print()

    # If --retry flag was passed, scan recent messages for today's paper
    if RETRY_MODE:
        print("🔄 RETRY MODE: Scanning recent group messages...")
        _retry_scan(client)


def _ensure_telegram_session():
    """Authenticate Telegram at startup so the code prompt works interactively."""
    print("🔑 Checking Telegram session...")
    tg_client = None
    try:
        tg_client = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        tg_client.start(phone=TELEGRAM_PHONE_NUMBER)
        print("✅ Telegram session ready!")
    except Exception as e:
        print(f"⚠️ Telegram auth failed: {e}")
        print("   The bot will retry when a newspaper is detected.")
    finally:
        if tg_client:
            tg_client.disconnect()


def _retry_scan(wa_client):
    """Manually scan recent messages from the target group for today's newspaper."""
    global LAST_SENT_DATE
    print("🔍 Requesting message history from the target group...")
    try:
        jid = build_jid(TARGET_GROUP_ID.split("@")[0], TARGET_GROUP_ID.split("@")[1])
        messages = wa_client.get_messages(jid, 50)  # last 50 messages
        found = False
        for msg_info in messages:
            try:
                msg_obj = msg_info.Message
                if not hasattr(msg_obj, "documentMessage") or not msg_obj.documentMessage:
                    continue
                file_name = msg_obj.documentMessage.fileName or ""
                ts = msg_info.Info.Timestamp
                if ts > 9999999999:
                    ts /= 1000
                msg_dt = datetime.fromtimestamp(ts)

                if SEARCH_TERM.lower() in file_name.lower() and msg_dt.date() == date.today():
                    print(f"🎯 Found today's newspaper in history: {file_name}")
                    if download_file(wa_client, msg_info):
                        found = True
                        break
            except Exception:
                continue

        if not found:
            print("❌ No matching newspaper found in recent messages.")
            print("   The bot will keep listening for new messages.")
        else:
            print("✅ Retry successful!")
    except Exception as e:
        print(f"⚠️ Retry scan failed: {e}")
        print("   The bot will keep listening for new messages.")


def _handle_retry_signal(signum, frame):
    """Handle SIGUSR1 signal to trigger a retry scan at runtime."""
    print("\n🔄 Received retry signal! Scanning recent messages...")
    _retry_scan(client)


if __name__ == "__main__":
    try:
        # On Linux/Docker, register SIGUSR1 for runtime retry
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, _handle_retry_signal)
            print("💡 Send SIGUSR1 to trigger a retry at runtime: kill -USR1 <pid>")
            print("   In Docker: docker exec <container> kill -USR1 1")

        _ensure_telegram_session()
        client.connect()
    except KeyboardInterrupt:
        print("\n👋 Shutting down safely...")
        sys.exit(0)