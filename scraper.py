import os
import sys
import time
import signal
import argparse
import json
import io
import threading
import asyncio
import pytz
import qrcode
from datetime import datetime, date
from dotenv import load_dotenv
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv, DisconnectedEv, LoggedOutEv, ConnectFailureEv
from neonize.utils import build_jid
from telethon import events, Button
from telethon.sync import TelegramClient
from naming_utils import get_newspaper_name

# Reconfigure stdout/stderr for Unicode/emoji support on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- CONFIGURATION (Loaded from .env) ---
load_dotenv()

TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID")


NEWSPAPER_CONFIG_FILE = "config_newspapers.json"


def save_target_newspapers(newspapers_list):
    """Save the active newspapers list to config_newspapers.json."""
    try:
        with open(NEWSPAPER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(newspapers_list, f, indent=2, ensure_ascii=False)
        print(f"💾 Saved {len(newspapers_list)} newspaper configuration(s) to '{NEWSPAPER_CONFIG_FILE}'.")
    except Exception as e:
        print(f"⚠️ Failed to save newspaper config: {e}")


def get_search_aliases(np_entry: dict) -> list:
    """Returns a list of search term aliases for a newspaper entry, with surrounding whitespace stripped."""
    search_val = np_entry.get("search", "")
    if isinstance(search_val, (list, tuple)):
        return [str(s).strip() for s in search_val if str(s).strip()]
    return [s.strip() for s in str(search_val).split(",") if s.strip()]


def matches_newspaper(text: str, np_entry: dict) -> bool:
    """Checks if any search alias or display name matches the input text (case-insensitive and trimmed)."""
    if not text or not np_entry:
        return False
    text_lower = text.strip().lower()
    name_clean = np_entry.get("name", "").strip().lower()
    if name_clean and name_clean in text_lower:
        return True
    for alias in get_search_aliases(np_entry):
        alias_clean = alias.strip().lower()
        if alias_clean and alias_clean in text_lower:
            return True
    return False


def load_target_newspapers():
    """Load target newspapers configuration from config_newspapers.json or environment variables."""
    if os.path.exists(NEWSPAPER_CONFIG_FILE):
        try:
            with open(NEWSPAPER_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    print(f"📰 Loaded {len(data)} newspaper(s) from '{NEWSPAPER_CONFIG_FILE}'.")
                    return data
        except Exception as e:
            print(f"⚠️ Error reading '{NEWSPAPER_CONFIG_FILE}': {e}")

    env_val = os.getenv("TARGET_NEWSPAPERS", "").strip()
    res = []
    if env_val:
        if env_val.startswith("["):
            try:
                items = json.loads(env_val)
                for item in items:
                    if isinstance(item, dict) and "name" in item:
                        res.append({
                            "name": str(item["name"]).strip(),
                            "search": str(item.get("search", item["name"])).strip()
                        })
            except json.JSONDecodeError:
                pass
        if not res:
            raw_items = env_val.replace("\n", ";").split(";")
            for part in raw_items:
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    name_part, search_part = part.split(":", 1)
                    res.append({"name": name_part.strip(), "search": search_part.strip()})
                else:
                    res.append({"name": part, "search": part})

    if not res:
        legacy_search = os.getenv("SEARCH_TERM", "La Provincia Las Palmas").strip()
        res = [{"name": "La Provincia", "search": f"La Provincia, {legacy_search}"}]

    save_target_newspapers(res)
    return res


TARGET_NEWSPAPERS = load_target_newspapers()

# Telegram config
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE_NUMBER = os.getenv("TELEGRAM_PHONE_NUMBER", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_CHAT = os.getenv("TELEGRAM_ADMIN_CHAT", os.getenv("TARGET_RECIPIENT", "")).strip()
TELEGRAM_NEWSPAPERS_CHAT_ID = os.getenv("TELEGRAM_NEWSPAPERS_CHAT_ID", "")
TELEGRAM_NEWSPAPERS_CHAT_NAME = os.getenv("TELEGRAM_NEWSPAPERS_CHAT_NAME", "")
TELEGRAM_SESSION_PATH = os.getenv("TELEGRAM_SESSION_PATH", "telegram_session")

DOWNLOAD_PATH = "downloads"
SENT_LOG_FILE = "last_sent.json"
LEGACY_SENT_LOG_FILE = "last_sent.txt"
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


def load_sent_log():
    data = {}
    if os.path.exists(LEGACY_SENT_LOG_FILE):
        try:
            with open(LEGACY_SENT_LOG_FILE, "r") as f:
                legacy_date = f.read().strip()
                if legacy_date:
                    default_name = TARGET_NEWSPAPERS[0]["name"] if TARGET_NEWSPAPERS else "La Provincia"
                    data[default_name] = legacy_date
        except Exception:
            pass
    if os.path.exists(SENT_LOG_FILE):
        try:
            with open(SENT_LOG_FILE, "r") as f:
                data.update(json.load(f))
        except Exception:
            pass
    return data


SENT_LOG_DATA = load_sent_log()
client = NewClient("session.db")


def already_sent_today(newspaper_name: str) -> bool:
    """Check if we already sent today's paper for a given newspaper (respects skip flag)."""
    if SKIP_DATE_CHECK:
        return False
    return SENT_LOG_DATA.get(newspaper_name) == str(date.today())


def save_sent_date(newspaper_name: str):
    today_str = str(date.today())
    SENT_LOG_DATA[newspaper_name] = today_str
    with open(SENT_LOG_FILE, "w") as f:
        json.dump(SENT_LOG_DATA, f, indent=2)
    return today_str


TG_BOT_CLIENT = None
TG_USER_CLIENT = None
TG_LOOP = None
TG_LOCK = threading.Lock()


def _ensure_tg_initialized():
    """Ensure Telegram clients (Bot and User Account) exist on a single shared event loop."""
    global TG_BOT_CLIENT, TG_USER_CLIENT, TG_LOOP, TG_CLIENT
    with TG_LOCK:
        if TG_LOOP is None:
            TG_LOOP = asyncio.new_event_loop()
            asyncio.set_event_loop(TG_LOOP)

        # 1. Initialize Bot Client for commands & Plan B uploads if bot token exists
        if TG_BOT_CLIENT is None and TELEGRAM_BOT_TOKEN:
            try:
                bot_c = TelegramClient("bot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH, loop=TG_LOOP)
                bot_c.start(bot_token=TELEGRAM_BOT_TOKEN)
                TG_BOT_CLIENT = bot_c
                print("🤖 Telegram Bot Client ready!")
            except Exception as e:
                print(f"⚠️ Could not start Telegram Bot Client: {e}")

        # 2. Initialize User Client for channel history & user posting if phone exists
        if TG_USER_CLIENT is None and TELEGRAM_PHONE_NUMBER:
            try:
                user_c = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH, loop=TG_LOOP)
                def _code_callback():
                    if not sys.stdin.isatty():
                        raise RuntimeError(
                            "Telegram user session is not authorized and standard input is non-interactive. "
                            "Please run interactive login to authorize the session."
                        )
                    return input("Please enter the code you received: ")
                user_c.start(phone=TELEGRAM_PHONE_NUMBER, code_callback=_code_callback)
                TG_USER_CLIENT = user_c
                print("📱 Telegram User Account Client ready!")
            except Exception as e:
                print(f"⚠️ Could not start Telegram User Client: {e}")

        # Primary client for history and channel sending: User Client first, fallback to Bot Client
        TG_CLIENT = TG_USER_CLIENT or TG_BOT_CLIENT
    return TG_CLIENT, TG_LOOP


async def _resolve_telegram_chat(tg_client):
    """Resolve the target Telegram chat/user destination."""
    name_str = (TELEGRAM_NEWSPAPERS_CHAT_NAME or "").lower().strip()
    id_str = (TELEGRAM_NEWSPAPERS_CHAT_ID or "").lower().strip()

    if name_str == "me" or id_str == "me" or (not TELEGRAM_NEWSPAPERS_CHAT_NAME and not TELEGRAM_NEWSPAPERS_CHAT_ID):
        print("📌 Target chat set to 'me' (Saved Messages).")
        return "me"

    # 1. Try username format (@channelname)
    for raw in [TELEGRAM_NEWSPAPERS_CHAT_NAME, TELEGRAM_NEWSPAPERS_CHAT_ID]:
        if raw and str(raw).strip().startswith("@"):
            try:
                entity = await tg_client.get_entity(str(raw).strip())
                if entity:
                    print(f"📌 Found target channel by username: {raw}")
                    return entity
            except Exception:
                pass

    # 2. Try integer chat ID candidates (raw ID, -100 channel ID, - group ID)
    if id_str.lstrip("-").isdigit():
        raw_id = int(TELEGRAM_NEWSPAPERS_CHAT_ID)
        candidates = [raw_id]
        if raw_id > 0:
            candidates.append(int(f"-100{raw_id}"))
            candidates.append(-raw_id)

        for cid in candidates:
            try:
                entity = await tg_client.get_entity(cid)
                if entity:
                    print(f"📌 Resolved Telegram target chat ID: {cid}")
                    return entity
            except Exception:
                pass

    # 3. Fallback to channel ID integer
    if id_str.lstrip("-").isdigit():
        raw_id = int(TELEGRAM_NEWSPAPERS_CHAT_ID)
        target = int(f"-100{raw_id}") if raw_id > 0 else raw_id
        return target

    print(f"⚠️ Target chat '{TELEGRAM_NEWSPAPERS_CHAT_NAME or TELEGRAM_NEWSPAPERS_CHAT_ID}' not found. Falling back to 'me'.")
    return "me"


def _pretty_print_date(dt):
    """Format a date in Spanish like the newspapers_telegram_bot: '1 de Mayo'."""
    months = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
              "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")
    return f"{dt.day} de {months[dt.month - 1]}"


async def _send_day_header(tg_client, chat):
    """Send a day marker message if none has been sent today."""
    tz = pytz.timezone('Atlantic/Canary')
    now = datetime.now(tz)

    # 1. Local persistent tracking check
    if not SKIP_DATE_CHECK and already_sent_today("__DAY_HEADER__"):
        print("📅 Day header already sent today (local log), skipping.")
        return

    # 2. Check channel history for header (#)
    try:
        async for msg in tg_client.iter_messages(chat, limit=25):
            if msg and msg.date:
                msg_date = msg.date.astimezone(tz).date()
                if msg_date == now.date():
                    text = getattr(msg, "message", "") or getattr(msg, "text", "") or ""
                    if "#" in text:
                        print("📅 Day header already exists in channel today (from channel history), skipping.")
                        save_sent_date("__DAY_HEADER__")
                        return
    except Exception:
        pass

    try:
        header = "# " + _pretty_print_date(now)
        await tg_client.send_message(chat, header)
        save_sent_date("__DAY_HEADER__")
        print(f"📅 Sent day header: {header}")
    except Exception as e:
        print(f"💡 Note: Day header skipped ({e}).")


async def _file_already_sent_today(tg_client, chat, custom_name):
    """Check if a file with this name was already sent to the chat today."""
    if SKIP_DATE_CHECK:
        return False

    newspaper_name = custom_name.split(",")[0].strip()

    # 1. Check local persistent tracking (last_sent.json)
    if already_sent_today(newspaper_name):
        print(f"✅ '{newspaper_name}' is already recorded as sent today in last_sent.json.")
        return True

    # 2. Inspect channel history (file names + message text captions)
    try:
        tz = pytz.timezone('Atlantic/Canary')
        now = datetime.now(tz)
        target_clean = custom_name.replace(".pdf", "").split(",")[0].strip().lower()

        async for msg in tg_client.iter_messages(chat, limit=30):
            if msg and msg.date:
                msg_date = msg.date.astimezone(tz).date()
                if msg_date == now.date():
                    file_name = getattr(msg.file, "name", "") if msg.file else ""
                    msg_text = getattr(msg, "message", "") or getattr(msg, "text", "") or ""
                    haystack = f"{file_name} {msg_text}".lower()

                    if target_clean in haystack:
                        print(f"✅ '{newspaper_name}' detected in Telegram channel history for today.")
                        save_sent_date(newspaper_name)
                        return True
    except Exception as ex:
        print(f"💡 Note: Channel history check skipped ({ex}).")

    return False


def send_to_telegram(file_path, custom_name, is_manual: bool = False):
    """Send the downloaded newspaper PDF to the Telegram newspapers chat."""
    print(f"📤 Sending '{custom_name}' to Telegram...")
    try:
        tg_client, loop = _ensure_tg_initialized()

        async def _do_send():
            target_chat = await _resolve_telegram_chat(tg_client)
            if target_chat is None:
                print(f"❌ Could not find Telegram chat '{TELEGRAM_NEWSPAPERS_CHAT_NAME}'")
                return False

            if not SKIP_DATE_CHECK and await _file_already_sent_today(tg_client, target_chat, custom_name):
                print(f"✅ '{custom_name}' already sent today, skipping duplicate delivery.")
                return True

            await _send_day_header(tg_client, target_chat)
            await tg_client.send_file(target_chat, file_path)
            print(f"🚀 Sent to Telegram successfully!")
            return True

        future = asyncio.run_coroutine_threadsafe(_do_send(), loop)
        return future.result(timeout=60)
    except Exception as e:
        print(f"❌ Failed to send to Telegram: {e}")
        return False


async def _resolve_admin_chat(tg_client):
    """Resolve the admin user/chat destination for alerts, QR codes, and admin commands."""
    if not TELEGRAM_ADMIN_CHAT:
        print("📌 No TELEGRAM_ADMIN_CHAT configured. Falling back to 'me' (Saved Messages).")
        return "me"

    raw_str = str(TELEGRAM_ADMIN_CHAT).strip()
    if raw_str.lower() == "me":
        return "me"

    # Try resolving phone number format (+34... or 34...)
    if raw_str.startswith("+") or (raw_str.isdigit() and len(raw_str) >= 11):
        phone_fmt = raw_str if raw_str.startswith("+") else f"+{raw_str}"
        try:
            entity = await tg_client.get_entity(phone_fmt)
            if entity:
                name = getattr(entity, "first_name", getattr(entity, "title", phone_fmt))
                print(f"📌 Found Admin recipient by phone: {name} ({phone_fmt})")
                return entity
        except Exception:
            pass

    # Try integer user ID or username
    try:
        if raw_str.lstrip("-").isdigit():
            entity = await tg_client.get_entity(int(raw_str))
        else:
            entity = await tg_client.get_entity(raw_str)
        if entity:
            return entity
    except Exception:
        pass

    print(f"⚠️ Admin target '{raw_str}' not found. Falling back to 'me'.")
    return "me"


def send_telegram_alert(message_text: str, file_bytes: bytes = None, filename: str = None):
    """Send an alert text message or image file directly to the Admin Telegram account."""
    print(f"📢 Dispatching alert to Admin Telegram...")
    try:
        tg_client, loop = _ensure_tg_initialized()

        async def _do_send_alert():
            admin_chat = await _resolve_admin_chat(tg_client)
            if file_bytes and filename:
                file_obj = io.BytesIO(file_bytes)
                file_obj.name = filename
                await tg_client.send_file(admin_chat, file_obj, caption=message_text)
                print(f"📱 Sent Telegram notification with image '{filename}' to Admin successfully!")
            else:
                await tg_client.send_message(admin_chat, message_text)
                print(f"📱 Sent Telegram alert message to Admin successfully!")
            return True

        future = asyncio.run_coroutine_threadsafe(_do_send_alert(), loop)
        return future.result(timeout=60)
    except Exception as e:
        print(f"❌ Failed to send Telegram alert: {e}")
        return False


@client.qr
def on_qr(client: NewClient, qr_data: bytes):
    """Triggered when WhatsApp requires a QR code for login."""
    try:
        qr_str = qr_data.decode("utf-8") if isinstance(qr_data, bytes) else str(qr_data)
        print(f"\n📲 WhatsApp QR Code generated! Rendering image for Telegram...")

        # Render QR string to PNG in memory
        qr_img = qrcode.make(qr_str)
        img_buffer = io.BytesIO()
        qr_img.save(img_buffer, format="PNG")
        img_bytes = img_buffer.getvalue()

        caption = "📱 *WhatsApp QR Login Required*\nPlease scan this QR code with WhatsApp (Linked Devices) to authorize the scraper."
        send_telegram_alert(caption, file_bytes=img_bytes, filename="whatsapp_qr.png")
    except Exception as e:
        print(f"⚠️ Failed to process/send WhatsApp QR code to Telegram: {e}")


def download_file(client, message_ev, newspaper_name: str, is_manual: bool = False):
    if not is_manual and not SKIP_DATE_CHECK and already_sent_today(newspaper_name):
        print(f"✅ '{newspaper_name}' has already been sent today, skipping download.")
        return True

    doc = message_ev.Message.documentMessage
    ts = message_ev.Info.Timestamp
    if ts > 9999999999: ts /= 1000
    msg_date = datetime.fromtimestamp(ts).date()

    custom_name = get_newspaper_name(newspaper_name, msg_date)
    path = os.path.join(DOWNLOAD_PATH, custom_name)

    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"📦 File already exists locally ({custom_name}). Attempting to send...")
        if send_to_telegram(path, custom_name, is_manual=is_manual):
            save_sent_date(newspaper_name)
            if os.path.exists(path):
                try: os.remove(path)
                except Exception: pass
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

                    if send_to_telegram(path, custom_name, is_manual=is_manual):
                        save_sent_date(newspaper_name)
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                                print(f"🧹 Cleaned up local temp file '{custom_name}'.")
                            except Exception: pass
                        return True
                    else:
                        if os.path.exists(path): os.remove(path)
                        return False
            except Exception as e:
                print(f"⚠️ Strategy {name} failed: {e}")

        if cycle < 3:
            time.sleep(5)

    print(f"❌ Failed to download '{custom_name}' after 3 cycles.")
    send_telegram_alert(f"⚠️ *WhatsApp Scraper Download Error*\nFailed to download newspaper '{custom_name}' after 3 retry cycles.")
    return False


PENDING_WA_SELECTIONS = {}


def _send_wa_reply(client: NewClient, message_ev: MessageEv, text: str):
    """Helper to send a text reply back to the chat on WhatsApp using neonize."""
    try:
        chat_jid = message_ev.Info.MessageSource.Chat
        client.send_message(chat_jid, text)
    except Exception as e:
        print(f"⚠️ Failed to send WhatsApp reply: {e}")


def process_bot_command(text: str) -> str:
    """Processes bot commands (/status, /list, /add, /remove, /help, /start) and returns the formatted response string."""
    text = text.strip()
    if not text.startswith("/"):
        return None

    cmd_parts = text.split(maxsplit=1)
    cmd = cmd_parts[0].lower().split("@")[0]
    arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""

    if cmd == "/status":
        status_lines = ["📊 *Today's Newspaper Status*:"]
        for np in TARGET_NEWSPAPERS:
            st = "✅ Completed" if already_sent_today(np["name"]) else "⏳ Pending"
            status_lines.append(f"• *{np['name']}*: {st}")
        mode = "DEV (skip-date-check)" if SKIP_DATE_CHECK else "PRODUCTION"
        status_lines.append(f"\n⚙️ Mode: `{mode}`")
        return "\n".join(status_lines)

    elif cmd == "/list":
        if not TARGET_NEWSPAPERS:
            return "📰 No newspapers are currently configured."
        lines = ["📰 *Configured Target Newspapers*:"]
        for i, np in enumerate(TARGET_NEWSPAPERS, 1):
            st = " (Completed today)" if already_sent_today(np["name"]) else " (Pending)"
            aliases = ", ".join(f"`{a}`" for a in get_search_aliases(np))
            lines.append(f"{i}. *{np['name']}*{st}\n   • Search aliases: {aliases}")
        lines.append("\n💡 *Usage*: `/add Name:search1, search2` or `/remove Name`")
        return "\n".join(lines)

    elif cmd == "/add":
        if not arg:
            return "⚠️ *Usage*: `/add DisplayName:search1, search2`\nExample: `/add La Provincia:La Provincia, La Provincia Las Palmas`"
        if ":" in arg:
            p_name, p_search = arg.split(":", 1)
            display_name, search_terms = p_name.strip(), p_search.strip()
        else:
            display_name, search_terms = arg.strip(), arg.strip()

        existing = next((np for np in TARGET_NEWSPAPERS if np["name"].lower() == display_name.lower()), None)
        if existing:
            existing["search"] = search_terms
            save_target_newspapers(TARGET_NEWSPAPERS)
            aliases_str = ", ".join(f"`{a}`" for a in get_search_aliases(existing))
            return f"✏️ Updated newspaper *{display_name}*\nSearch aliases: {aliases_str}"
        else:
            new_np = {"name": display_name, "search": search_terms}
            TARGET_NEWSPAPERS.append(new_np)
            save_target_newspapers(TARGET_NEWSPAPERS)
            aliases_str = ", ".join(f"`{a}`" for a in get_search_aliases(new_np))
            return f"✅ Added newspaper *{display_name}*\nSearch aliases: {aliases_str}"

    elif cmd == "/remove":
        if not arg:
            return "⚠️ *Usage*: `/remove DisplayName`\nExample: `/remove Canarias7`"
        target = arg.lower()
        matching = [np for np in TARGET_NEWSPAPERS if np["name"].lower() == target or np["search"].lower() == target]
        if not matching:
            return f"❌ Newspaper *{arg}* not found in active list.\nUse `/list` to view active newspapers."
        for np in matching:
            TARGET_NEWSPAPERS.remove(np)
        save_target_newspapers(TARGET_NEWSPAPERS)
        return f"🗑️ Removed *{arg}* from target newspapers list."

    elif cmd in ("/help", "/start"):
        return (
            "🤖 *Newspaper Scraper Commands*:\n\n"
            "• `/status` - Show today's delivery status for all newspapers\n"
            "• `/list` - List all active target newspapers\n"
            "• `/add SearchTerm:DisplayName` - Add or update a target newspaper\n"
            "• `/remove DisplayName` - Remove a newspaper from the list\n"
            "• `/help` - Show this help message\n\n"
            "📄 *Plan B*: Upload a PDF directly to this chat to forward it to your Telegram channel!"
        )

    return None


@client.event(MessageEv)
def on_message(client: NewClient, message: MessageEv):
    try:
        msg_id = message.Info.ID
        if msg_id in PROCESSED_MESSAGES: return
        PROCESSED_MESSAGES.add(msg_id)

        chat_info = message.Info.MessageSource.Chat
        current_chat_id = f"{chat_info.User}@{chat_info.Server}"

        ts = message.Info.Timestamp
        if ts > 9999999999: ts /= 1000
        msg_dt = datetime.fromtimestamp(ts)
        msg_obj = message.Message

        # 1. Target Group Scraping
        if current_chat_id == TARGET_GROUP_ID:
            if hasattr(msg_obj, "documentMessage") and msg_obj.documentMessage:
                file_name = msg_obj.documentMessage.fileName or ""

                for np in TARGET_NEWSPAPERS:
                    if not SKIP_DATE_CHECK and already_sent_today(np["name"]):
                        continue
                    if matches_newspaper(file_name, np) and (SKIP_DATE_CHECK or msg_dt.date() == date.today()):
                        sender = getattr(message.Info, "PushName", "Someone")
                        print(f"\n🎯 TARGET DETECTED [{np['name']}] from {sender}: {file_name}")
                        download_file(client, message, np["name"], is_manual=False)
                        break

        # 2. Plan B: Direct WhatsApp DM / Manual Upload / Commands
        elif chat_info.Server != "g.us" or current_chat_id != TARGET_GROUP_ID:
            # Check for bot commands in WhatsApp DM (/status, /list, /add, /remove, /help)
            text_content = ""
            if hasattr(msg_obj, "conversation") and msg_obj.conversation:
                text_content = msg_obj.conversation.strip()
            elif hasattr(msg_obj, "extendedTextMessage") and msg_obj.extendedTextMessage:
                text_content = (msg_obj.extendedTextMessage.text or "").strip()

            if text_content and text_content.startswith("/"):
                cmd_reply = process_bot_command(text_content)
                if cmd_reply:
                    _send_wa_reply(client, message, cmd_reply)
                    return

            # Check if this is a pending text reply to a previous selection prompt
            if current_chat_id in PENDING_WA_SELECTIONS:
                if text_content:
                    matched_np = None
                    if text_content.isdigit():
                        idx = int(text_content) - 1
                        if 0 <= idx < len(TARGET_NEWSPAPERS):
                            matched_np = TARGET_NEWSPAPERS[idx]
                    else:
                        for np in TARGET_NEWSPAPERS:
                            if matches_newspaper(text_content, np):
                                matched_np = np
                                break

                    if matched_np:
                        saved_ev = PENDING_WA_SELECTIONS.pop(current_chat_id)
                        if not SKIP_DATE_CHECK and already_sent_today(matched_np["name"]):
                            print(f"✅ Plan B WhatsApp: '{matched_np['name']}' was already sent today. Skipping download.")
                            _send_wa_reply(client, message, f"⚠️ *Plan B WhatsApp*: `{matched_np['name']}` has already been delivered today! Skipping download.")
                            return

                        print(f"🎯 Plan B WhatsApp: Selected [{matched_np['name']}] via text reply.")
                        _send_wa_reply(client, message, f"⏳ *Plan B WhatsApp*: Processing as `{matched_np['name']}`...")
                        if download_file(client, saved_ev, matched_np["name"], is_manual=True):
                            _send_wa_reply(client, message, f"✅ *Plan B WhatsApp Success*: Delivered `{matched_np['name']}` to Telegram!")
                        else:
                            _send_wa_reply(client, message, f"❌ *Plan B WhatsApp Error*: Failed to process `{matched_np['name']}`.")
                        return
                    else:
                        _send_wa_reply(client, message, "⚠️ Invalid choice. Please reply with the number (e.g. 1) or newspaper name.")
                        return

            # Check if user sent a PDF document directly
            if hasattr(msg_obj, "documentMessage") and msg_obj.documentMessage:
                file_name = msg_obj.documentMessage.fileName or ""
                print(f"\n📩 Plan B WhatsApp: Received direct document upload '{file_name}'")

                matched_np = None
                for np in TARGET_NEWSPAPERS:
                    if matches_newspaper(file_name, np):
                        matched_np = np
                        break

                if matched_np:
                    if not SKIP_DATE_CHECK and already_sent_today(matched_np["name"]):
                        print(f"✅ Plan B WhatsApp: '{matched_np['name']}' was already sent today. Skipping download.")
                        _send_wa_reply(client, message, f"⚠️ *Plan B WhatsApp*: `{matched_np['name']}` has already been delivered today! Skipping download.")
                        return

                    print(f"🎯 Plan B WhatsApp: Auto-matched [{matched_np['name']}] for '{file_name}'")
                    _send_wa_reply(client, message, f"⏳ *Plan B WhatsApp*: Auto-matched `{matched_np['name']}`. Downloading & forwarding...")
                    if download_file(client, message, matched_np["name"], is_manual=True):
                        _send_wa_reply(client, message, f"✅ *Plan B WhatsApp Success*: Delivered `{matched_np['name']}` to Telegram!")
                    else:
                        _send_wa_reply(client, message, f"❌ *Plan B WhatsApp Error*: Failed to process `{matched_np['name']}`.")
                else:
                    PENDING_WA_SELECTIONS[current_chat_id] = message
                    lines = [f"⚠️ *Plan B WhatsApp*: Could not auto-detect newspaper for `{file_name}`.\n\nPlease reply with the number or name of the newspaper:"]
                    for i, np in enumerate(TARGET_NEWSPAPERS, 1):
                        aliases = ", ".join(get_search_aliases(np))
                        lines.append(f"{i}. *{np['name']}* (Aliases: `{aliases}`)")
                    lines.append("\n💡 Example reply: `1` or `Marca`")
                    _send_wa_reply(client, message, "\n".join(lines))
    except Exception as e:
        print(f"⚠️ Error in on_message: {e}")


@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    mode = "DEV (skip-date-check)" if SKIP_DATE_CHECK else "PRODUCTION"
    print(f"🚀 Monitoring Group: {TARGET_GROUP_ID} [{mode}]")
    print("📰 Target Newspapers:")
    for np in TARGET_NEWSPAPERS:
        status = "Completed" if already_sent_today(np["name"]) else "Pending"
        aliases = ", ".join(get_search_aliases(np))
        print(f"   • {np['name']} (Aliases: '{aliases}') → {status}")
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

    # If --retry flag was passed, scan recent messages for unsent newspapers
    if RETRY_MODE:
        print("🔄 RETRY MODE: Scanning recent group messages...")
        _retry_scan(client)


@client.event(DisconnectedEv)
def on_disconnected(client: NewClient, event: DisconnectedEv):
    print("⚠️ WhatsApp disconnected!")
    send_telegram_alert("⚠️ *WhatsApp Connection Lost*\nWhatsApp connection dropped. The scraper will attempt to reconnect automatically.")


@client.event(LoggedOutEv)
def on_logged_out(client: NewClient, event: LoggedOutEv):
    print("❌ WhatsApp logged out!")
    send_telegram_alert("❌ *WhatsApp Session Logged Out*\nYour WhatsApp account session was unlinked. A new QR code will be generated to re-pair.")


@client.event(ConnectFailureEv)
def on_connect_failure(client: NewClient, event: ConnectFailureEv):
    print("❌ WhatsApp connection failure!")
    send_telegram_alert("⚠️ *WhatsApp Connection Failure*\nFailed to establish connection to WhatsApp servers.")


def _ensure_telegram_session():
    """Authenticate Telegram at startup so the code prompt works interactively."""
    print("🔑 Checking Telegram session...")
    try:
        _ensure_tg_initialized()
        print("✅ Telegram session ready!")
    except Exception as e:
        print(f"⚠️ Telegram auth failed: {e}")
        print("   The bot will retry when a newspaper is detected.")


def _retry_scan(wa_client):
    """Manually scan recent messages from the target group for today's unsent newspapers."""
    pending_nps = [np for np in TARGET_NEWSPAPERS if not already_sent_today(np["name"])]
    if not pending_nps:
        print("✅ All target newspapers have already been sent today.")
        return

    print(f"🔍 Requesting message history for {len(pending_nps)} pending newspaper(s)...")
    try:
        jid = build_jid(TARGET_GROUP_ID.split("@")[0], TARGET_GROUP_ID.split("@")[1])
        messages = wa_client.get_messages(jid, 50)  # last 50 messages
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

                if msg_dt.date() != date.today():
                    continue

                for np in list(pending_nps):
                    if matches_newspaper(file_name, np):
                        print(f"🎯 Found today's newspaper [{np['name']}] in history: {file_name}")
                        if download_file(wa_client, msg_info, np["name"]):
                            pending_nps.remove(np)
                        break
            except Exception:
                continue

        if pending_nps:
            pending_names = ", ".join(np["name"] for np in pending_nps)
            print(f"❌ Pending newspaper(s) not found in recent history: {pending_names}")
            print("   The bot will keep listening for new messages.")
        else:
            print("✅ Retry scan complete! All newspapers processed.")
    except Exception as e:
        print(f"⚠️ Retry scan failed: {e}")
        print("   The bot will keep listening for new messages.")


def _handle_retry_signal(signum, frame):
    """Handle SIGUSR1 signal to trigger a retry scan at runtime."""
    print("\n🔄 Received retry signal! Scanning recent messages...")
    _retry_scan(client)


def run_telegram_listener():
    """Run Telegram command listener in a background daemon thread.
    Uses the unified TG_CLIENT instance (Bot API or User account)."""
    try:
        tg_client, loop = _ensure_tg_initialized()
        asyncio.set_event_loop(loop)

        async def _init_and_run():
            me = await tg_client.get_me()
            is_bot = getattr(me, "bot", False)
            username = getattr(me, "username", "Bot" if is_bot else "User")

            if is_bot:
                print(f"🤖 Official Telegram Bot active (@{username})! Send /status, /list, /add, /remove, or /help to @{username}.")
            else:
                print(f"🤖 Telegram listener active (@{username})! Send /status, /list, /add, /remove, or /help in private DMs.")

            async def _process_uploaded_telegram_document(event):
                """Process a PDF/document sent directly to the Telegram bot as Plan B fallback."""
                file = event.message.file
                if not file:
                    return

                orig_name = file.name or ""
                caption_text = (event.raw_text or "").strip()
                search_haystack = f"{orig_name} {caption_text}".lower()

                # Find matching target newspaper automatically
                matched_np = None
                for np in TARGET_NEWSPAPERS:
                    if matches_newspaper(search_haystack, np):
                        matched_np = np
                        break

                # If no auto-match, prompt with interactive inline buttons to select newspaper
                if not matched_np:
                    buttons = []
                    for idx, np in enumerate(TARGET_NEWSPAPERS):
                        buttons.append([Button.inline(f"📰 {np['name']} (Search: '{np['search']}')", data=f"select_np:{idx}:{event.message.id}")])

                    await event.reply(
                        f"⚠️ *Plan B Upload*: Could not auto-detect newspaper for `{orig_name}`.\n\n"
                        f"👇 *Please tap the newspaper this file belongs to*:",
                        buttons=buttons
                    )
                    return

                newspaper_name = matched_np["name"]
                msg_dt = event.message.date or datetime.now()
                custom_name = get_newspaper_name(newspaper_name, msg_dt)
                save_path = os.path.join(DOWNLOAD_PATH, custom_name)

                target_chat = await _resolve_telegram_chat(tg_client)
                if target_chat and not SKIP_DATE_CHECK and await _file_already_sent_today(tg_client, target_chat, custom_name):
                    print(f"✅ Plan B: '{newspaper_name}' was already sent today. Skipping download.")
                    await event.reply(f"⚠️ *Plan B*: `{newspaper_name}` has already been delivered today! Skipping download.")
                    return

                status_msg = await event.reply(f"⏳ *Plan B Activated*: Downloading `{custom_name}`...")

                try:
                    last_edit = [0]
                    async def dl_progress(current, total):
                        now = time.time()
                        if total > 0 and (now - last_edit[0] >= 30.0 or current == total):
                            last_edit[0] = now
                            pct = int((current / total) * 100)
                            mb_cur = current / (1024 * 1024)
                            mb_tot = total / (1024 * 1024)
                            try:
                                await status_msg.edit(f"⏳ *Downloading*: `{custom_name}`\n📊 *Progress*: `{pct}%` ({mb_cur:.1f} / {mb_tot:.1f} MB)")
                            except Exception:
                                pass

                    await tg_client.download_media(event.message, file=save_path, progress_callback=dl_progress)
                    print(f"✅ Plan B: Downloaded '{custom_name}' directly from Telegram upload.")

                    target_chat = await _resolve_telegram_chat(tg_client)
                    if target_chat is None:
                        await status_msg.edit(f"❌ Could not resolve Telegram channel `{TELEGRAM_NEWSPAPERS_CHAT_NAME}`.")
                        return

                    await status_msg.edit(f"📤 *Uploading to Channel*: `{custom_name}`...")
                    await _send_day_header(tg_client, target_chat)
                    await tg_client.send_file(target_chat, save_path)
                    save_sent_date(newspaper_name)
                    print(f"🚀 Plan B: Sent '{custom_name}' to Telegram channel successfully!")

                    if os.path.exists(save_path):
                        try:
                            os.remove(save_path)
                            print(f"🧹 Cleaned up local file '{custom_name}'.")
                        except Exception: pass

                    target_display = TELEGRAM_NEWSPAPERS_CHAT_NAME or TELEGRAM_NEWSPAPERS_CHAT_ID or "Target Channel"
                    await status_msg.edit(
                        f"✅ *Plan B Success*!\n\n"
                        f"📰 *Newspaper*: *{newspaper_name}*\n"
                        f"📄 *File*: `{custom_name}`\n"
                        f"📨 Delivered to `{target_display}`."
                    )
                except Exception as e:
                    print(f"❌ Plan B failed: {e}")
                    await status_msg.edit(f"❌ *Plan B Error*: Failed to process `{custom_name}`: {e}")

            @tg_client.on(events.CallbackQuery(pattern=r"^select_np:(\d+):(\d+)$"))
            async def handle_newspaper_button_selection(event):
                try:
                    np_idx = int(event.pattern_match.group(1))
                    doc_msg_id = int(event.pattern_match.group(2))

                    if np_idx >= len(TARGET_NEWSPAPERS):
                        await event.answer("❌ Invalid selection.", alert=True)
                        return

                    matched_np = TARGET_NEWSPAPERS[np_idx]
                    await event.answer(f"Selected {matched_np['name']}!")

                    messages = await tg_client.get_messages(event.chat_id, ids=doc_msg_id)
                    if not messages or not messages.file:
                        await event.edit("❌ Could not locate the original uploaded file.")
                        return

                    newspaper_name = matched_np["name"]
                    msg_dt = messages.date or datetime.now()
                    custom_name = get_newspaper_name(newspaper_name, msg_dt)
                    save_path = os.path.join(DOWNLOAD_PATH, custom_name)

                    target_chat = await _resolve_telegram_chat(tg_client)
                    if target_chat and not SKIP_DATE_CHECK and await _file_already_sent_today(tg_client, target_chat, custom_name):
                        print(f"✅ Plan B: '{newspaper_name}' was already sent today. Skipping download.")
                        await event.edit(f"⚠️ *Plan B*: `{newspaper_name}` has already been delivered today! Skipping download.")
                        return

                    await event.edit(f"⏳ *Plan B Activated*: Matched as `{newspaper_name}`...\nDownloading `{custom_name}`...")

                    last_edit = [0]
                    async def dl_progress(current, total):
                        now = time.time()
                        if total > 0 and (now - last_edit[0] >= 30.0 or current == total):
                            last_edit[0] = now
                            pct = int((current / total) * 100)
                            mb_cur = current / (1024 * 1024)
                            mb_tot = total / (1024 * 1024)
                            try:
                                await event.edit(f"⏳ *Downloading*: `{custom_name}`\n📊 *Progress*: `{pct}%` ({mb_cur:.1f} / {mb_tot:.1f} MB)")
                            except Exception:
                                pass

                    await tg_client.download_media(messages, file=save_path, progress_callback=dl_progress)
                    print(f"✅ Plan B: Force-matched '{custom_name}' from Telegram upload button click.")

                    target_chat = await _resolve_telegram_chat(tg_client)
                    if target_chat is None:
                        await event.edit(f"❌ Could not resolve Telegram channel `{TELEGRAM_NEWSPAPERS_CHAT_NAME}`.")
                        return

                    await event.edit(f"📤 *Uploading to Channel*: `{custom_name}`...")
                    await _send_day_header(tg_client, target_chat)
                    await tg_client.send_file(target_chat, save_path)
                    save_sent_date(newspaper_name)
                    print(f"🚀 Plan B: Force-matched and sent '{custom_name}' to Telegram channel successfully!")

                    if os.path.exists(save_path):
                        try:
                            os.remove(save_path)
                            print(f"🧹 Cleaned up local file '{custom_name}'.")
                        except Exception: pass

                    target_display = TELEGRAM_NEWSPAPERS_CHAT_NAME or TELEGRAM_NEWSPAPERS_CHAT_ID or "Target Channel"
                    await event.edit(
                        f"✅ *Plan B Success*!\n\n"
                        f"📰 *Matched Newspaper*: *{newspaper_name}*\n"
                        f"📄 *File*: `{custom_name}`\n"
                        f"📨 Delivered to `{target_display}`."
                    )
                except Exception as e:
                    print(f"❌ Force selection failed: {e}")
                    await event.edit(f"❌ *Plan B Selection Error*: {e}")

            admin_chat = await _resolve_admin_chat(tg_client)
            admin_id = getattr(admin_chat, "id", None) if hasattr(admin_chat, "id") else None
            me_id = getattr(me, "id", None)

            @tg_client.on(events.NewMessage)
            async def handle_telegram_command(event):
                # Forward caching removed (no longer needed)

                # Handle Direct File Upload (Plan B)
                if event.message.file and (event.is_private or is_bot):
                    await _process_uploaded_telegram_document(event)
                    return

                text = (event.raw_text or "").strip()
                if not text.startswith("/"):
                    return

                if not is_bot and admin_id and event.chat_id != admin_id and event.chat_id != me_id and not event.is_private:
                    return

                reply = process_bot_command(text)
                if reply:
                    await event.reply(reply)
                    return

            await tg_client.run_until_disconnected()

        loop.run_until_complete(_init_and_run())
    except Exception as e:
        print(f"⚠️ Telegram listener error: {e}")


def _start_telegram_listener():
    t = threading.Thread(target=run_telegram_listener, daemon=True, name="TelegramListener")
    t.start()


if __name__ == "__main__":
    try:
        # On Linux/Docker, register SIGUSR1 for runtime retry
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, _handle_retry_signal)
            print("💡 Send SIGUSR1 to trigger a retry at runtime: kill -USR1 <pid>")
            print("   In Docker: docker exec <container> kill -USR1 1")

        _ensure_telegram_session()
        _start_telegram_listener()
        client.connect()
    except KeyboardInterrupt:
        print("\n👋 Shutting down safely...")
        os._exit(0)