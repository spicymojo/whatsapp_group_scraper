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
from telethon import events
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
                    if isinstance(item, dict) and "search" in item:
                        res.append({
                            "search": str(item["search"]).strip(),
                            "name": str(item.get("name", item["search"])).strip()
                        })
            except json.JSONDecodeError:
                pass
        if not res:
            for part in env_val.split(","):
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    search_term, name = part.split(":", 1)
                    res.append({"search": search_term.strip(), "name": name.strip()})
                else:
                    res.append({"search": part, "name": part})

    if not res:
        legacy_search = os.getenv("SEARCH_TERM", "La Provincia Las Palmas").strip()
        res = [{"search": legacy_search, "name": "La Provincia"}]

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


TG_CLIENT = None
TG_LOOP = None
TG_LOCK = threading.Lock()


def _ensure_tg_initialized():
    """Ensure a single shared TelegramClient instance and event loop exist."""
    global TG_CLIENT, TG_LOOP
    with TG_LOCK:
        if TG_CLIENT is None:
            TG_LOOP = asyncio.new_event_loop()
            asyncio.set_event_loop(TG_LOOP)
            tg_client = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH, loop=TG_LOOP)
            tg_client.start(phone=TELEGRAM_PHONE_NUMBER)
            TG_CLIENT = tg_client
    return TG_CLIENT, TG_LOOP


async def _resolve_telegram_chat(tg_client):
    """Resolve the target Telegram chat/user destination."""
    name_str = (TELEGRAM_NEWSPAPERS_CHAT_NAME or "").lower().strip()
    id_str = (TELEGRAM_NEWSPAPERS_CHAT_ID or "").lower().strip()

    if name_str == "me" or id_str == "me" or (not TELEGRAM_NEWSPAPERS_CHAT_NAME and not TELEGRAM_NEWSPAPERS_CHAT_ID):
        print("📌 Target chat set to 'me' (Saved Messages).")
        return "me"

    raw_target = TELEGRAM_NEWSPAPERS_CHAT_ID or TELEGRAM_NEWSPAPERS_CHAT_NAME
    raw_str = str(raw_target).strip()

    # Try resolving phone number format (+34... or 34...)
    if raw_str.startswith("+") or (raw_str.isdigit() and len(raw_str) >= 11):
        phone_fmt = raw_str if raw_str.startswith("+") else f"+{raw_str}"
        try:
            entity = await tg_client.get_entity(phone_fmt)
            if entity:
                name = getattr(entity, "first_name", getattr(entity, "title", phone_fmt))
                print(f"📌 Found target recipient by phone: {name} ({phone_fmt})")
                return entity
        except Exception:
            pass

    # Try integer chat ID
    if id_str.lstrip("-").isdigit():
        target_id = int(TELEGRAM_NEWSPAPERS_CHAT_ID)
        async for dialog in tg_client.iter_dialogs():
            if dialog.id == target_id or dialog.id == int(f"-100{target_id}") or dialog.id == -target_id:
                print(f"📌 Found chat by ID: {dialog.name} ({dialog.id})")
                return dialog
        try:
            entity = await tg_client.get_entity(target_id)
            if entity:
                return entity
        except Exception:
            pass

    # Try dialog name
    if TELEGRAM_NEWSPAPERS_CHAT_NAME:
        async for dialog in tg_client.iter_dialogs():
            if TELEGRAM_NEWSPAPERS_CHAT_NAME in dialog.name:
                print(f"📌 Found chat by name: {dialog.name} (ID: {dialog.id})")
                return dialog

    print(f"⚠️ Target chat '{raw_str}' not found. Falling back to 'Saved Messages' (me).")
    return "me"


def _pretty_print_date(dt):
    """Format a date in Spanish like the newspapers_telegram_bot: '1 de Mayo'."""
    months = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
              "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")
    return f"{dt.day} de {months[dt.month - 1]}"


async def _send_day_header(tg_client, chat):
    """Send a day marker message if none has been sent today (like newspapers_telegram_bot)."""
    tz = pytz.timezone('Atlantic/Canary')
    now = datetime.now(tz)
    messages = await tg_client.get_messages(chat, limit=10)
    for msg in messages:
        if msg.date and now.date() == msg.date.astimezone(tz).date():
            text = getattr(msg, "message", "") or ""
            if "#" in text:
                print("📅 Day header already exists, skipping.")
                return
    header = "# " + _pretty_print_date(now)
    await tg_client.send_message(chat, header)
    print(f"📅 Sent day header: {header}")


async def _file_already_sent_today(tg_client, chat, custom_name):
    """Check if a file with this name was already sent to the chat today."""
    tz = pytz.timezone('Atlantic/Canary')
    now = datetime.now(tz)
    messages = await tg_client.get_messages(chat, limit=10)
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
    """Send the downloaded newspaper PDF to the Telegram newspapers chat."""
    print(f"📤 Sending '{custom_name}' to Telegram...")
    try:
        tg_client, loop = _ensure_tg_initialized()

        async def _do_send():
            target_chat = await _resolve_telegram_chat(tg_client)
            if target_chat is None:
                print(f"❌ Could not find Telegram chat '{TELEGRAM_NEWSPAPERS_CHAT_NAME}'")
                return False

            if await _file_already_sent_today(tg_client, target_chat, custom_name):
                print(f"✅ '{custom_name}' already sent today, skipping.")
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


def download_file(client, message_ev, newspaper_name: str):
    doc = message_ev.Message.documentMessage
    ts = message_ev.Info.Timestamp
    if ts > 9999999999: ts /= 1000
    msg_date = datetime.fromtimestamp(ts).date()

    custom_name = get_newspaper_name(newspaper_name, msg_date)
    path = os.path.join(DOWNLOAD_PATH, custom_name)

    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"📦 File already exists locally ({custom_name}). Attempting to send...")
        if send_to_telegram(path, custom_name):
            save_sent_date(newspaper_name)
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
                        save_sent_date(newspaper_name)
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


@client.event(MessageEv)
def on_message(client: NewClient, message: MessageEv):
    try:
        # Stop processing if all target newspapers have already been sent today
        if not SKIP_DATE_CHECK and all(already_sent_today(np["name"]) for np in TARGET_NEWSPAPERS):
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

                for np in TARGET_NEWSPAPERS:
                    if already_sent_today(np["name"]):
                        continue
                    if np["search"].lower() in file_name.lower() and msg_dt.date() == date.today():
                        sender = getattr(message.Info, "PushName", "Someone")
                        print(f"\n🎯 TARGET DETECTED [{np['name']}] from {sender}: {file_name}")
                        download_file(client, message, np["name"])
                        break
    except Exception as e:
        print(f"⚠️ Error in on_message: {e}")


@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    mode = "DEV (skip-date-check)" if SKIP_DATE_CHECK else "PRODUCTION"
    print(f"🚀 Monitoring Group: {TARGET_GROUP_ID} [{mode}]")
    print("📰 Target Newspapers:")
    for np in TARGET_NEWSPAPERS:
        status = "Completed" if already_sent_today(np["name"]) else "Pending"
        print(f"   • {np['name']} (Search: '{np['search']}') → {status}")
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
                    if np["search"].lower() in file_name.lower():
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
    Uses official Bot API (TELEGRAM_BOT_TOKEN) if set, or falls back to User Account listener."""
    if TELEGRAM_BOT_TOKEN:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            bot_client = TelegramClient("bot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH, loop=loop)
            bot_client.start(bot_token=TELEGRAM_BOT_TOKEN)

            async def _init_bot():
                me = await bot_client.get_me()
                bot_user = getattr(me, "username", "Bot")
                print(f"🤖 Official Telegram Bot active (@{bot_user})! Send /status, /list, /add, /remove, or /help to @{bot_user}.")

                @bot_client.on(events.NewMessage)
                async def handle_bot_command(event):
                    text = (event.raw_text or "").strip()
                    if not text.startswith("/"):
                        return

                    cmd = text.split()[0].lower().split("@")[0]
                    if cmd == "/status":
                        status_lines = ["📊 *Today's Newspaper Status*:"]
                        for np in TARGET_NEWSPAPERS:
                            st = "✅ Completed" if already_sent_today(np["name"]) else "⏳ Pending"
                            status_lines.append(f"• *{np['name']}*: {st}")
                        mode = "DEV (skip-date-check)" if SKIP_DATE_CHECK else "PRODUCTION"
                        status_lines.append(f"\n⚙️ Mode: `{mode}`")
                        await event.reply("\n".join(status_lines))

                    elif cmd == "/list":
                        if not TARGET_NEWSPAPERS:
                            await event.reply("📰 No newspapers are currently configured.")
                        else:
                            lines = ["📰 *Configured Target Newspapers*:"]
                            for i, np in enumerate(TARGET_NEWSPAPERS, 1):
                                st = " (Completed today)" if already_sent_today(np["name"]) else " (Pending)"
                                lines.append(f"{i}. *{np['name']}*{st}\n   • Search term: `{np['search']}`")
                            lines.append("\n💡 *Commands*: `/add Search:Name` or `/remove Name`")
                            await event.reply("\n".join(lines))

                    elif cmd == "/add":
                        raw_arg = text.split(" ", 1)[1].strip() if " " in text else ""
                        if not raw_arg:
                            await event.reply("⚠️ *Usage*: `/add SearchTerm:DisplayName`\nExample: `/add El Pais:El País`")
                        else:
                            if ":" in raw_arg:
                                p_search, p_name = raw_arg.split(":", 1)
                                search_term, display_name = p_search.strip(), p_name.strip()
                            else:
                                search_term, display_name = raw_arg.strip(), raw_arg.strip()

                            existing = next((np for np in TARGET_NEWSPAPERS if np["name"].lower() == display_name.lower()), None)
                            if existing:
                                existing["search"] = search_term
                                save_target_newspapers(TARGET_NEWSPAPERS)
                                await event.reply(f"✏️ Updated newspaper *{display_name}* (Search term: `{search_term}`).")
                            else:
                                TARGET_NEWSPAPERS.append({"search": search_term, "name": display_name})
                                save_target_newspapers(TARGET_NEWSPAPERS)
                                await event.reply(f"✅ Added newspaper *{display_name}* (Search term: `{search_term}`).")

                    elif cmd == "/remove":
                        raw_arg = text.split(" ", 1)[1].strip() if " " in text else ""
                        if not raw_arg:
                            await event.reply("⚠️ *Usage*: `/remove DisplayName`\nExample: `/remove Canarias7`")
                        else:
                            target = raw_arg.lower()
                            matching = [np for np in TARGET_NEWSPAPERS if np["name"].lower() == target or np["search"].lower() == target]
                            if not matching:
                                await event.reply(f"❌ Newspaper *{raw_arg}* not found in active list.\nUse `/list` to view active newspapers.")
                            else:
                                for np in matching:
                                    TARGET_NEWSPAPERS.remove(np)
                                save_target_newspapers(TARGET_NEWSPAPERS)
                                await event.reply(f"🗑️ Removed *{raw_arg}* from target newspapers list.")

                    elif cmd in ("/help", "/start"):
                        help_text = (
                            f"🤖 *WhatsApp Scraper Bot (@{bot_user})*:\n\n"
                            "• `/status` - Show today's delivery status for all newspapers\n"
                            "• `/list` - List all active target newspapers\n"
                            "• `/add SearchTerm:DisplayName` - Add or update a target newspaper\n"
                            "• `/remove DisplayName` - Remove a newspaper from the list\n"
                            "• `/help` - Show this help message"
                        )
                        await event.reply(help_text)

                await bot_client.run_until_disconnected()

            loop.run_until_complete(_init_bot())
        except Exception as e:
            print(f"⚠️ Telegram Bot error: {e}")
    else:
        print("💡 Hint: Add TELEGRAM_BOT_TOKEN to .env from @BotFather for instant Telegram bot commands.")
        try:
            tg_client, loop = _ensure_tg_initialized()
            asyncio.set_event_loop(loop)

            async def _init_and_run():
                admin_chat = await _resolve_admin_chat(tg_client)
                admin_id = getattr(admin_chat, "id", None)
                me = await tg_client.get_me()
                me_id = getattr(me, "id", None)

                print(f"🤖 Telegram user listener active! Send /status, /list, /add, /remove, or /help in private DMs.")

                @tg_client.on(events.NewMessage)
                async def handle_telegram_command(event):
                    text = (event.raw_text or "").strip()
                    if not text.startswith("/"):
                        return

                    if admin_id and event.chat_id != admin_id and event.chat_id != me_id and not event.is_private:
                        return

                    cmd = text.split()[0].lower().split("@")[0]
                    if cmd == "/status":
                        status_lines = ["📊 *Today's Newspaper Status*:"]
                        for np in TARGET_NEWSPAPERS:
                            st = "✅ Completed" if already_sent_today(np["name"]) else "⏳ Pending"
                            status_lines.append(f"• *{np['name']}*: {st}")
                        mode = "DEV (skip-date-check)" if SKIP_DATE_CHECK else "PRODUCTION"
                        status_lines.append(f"\n⚙️ Mode: `{mode}`")
                        await event.reply("\n".join(status_lines))

                    elif cmd == "/list":
                        if not TARGET_NEWSPAPERS:
                            await event.reply("📰 No newspapers are currently configured.")
                        else:
                            lines = ["📰 *Configured Target Newspapers*:"]
                            for i, np in enumerate(TARGET_NEWSPAPERS, 1):
                                st = " (Completed today)" if already_sent_today(np["name"]) else " (Pending)"
                                lines.append(f"{i}. *{np['name']}*{st}\n   • Search term: `{np['search']}`")
                            lines.append("\n💡 *Commands*: `/add Search:Name` or `/remove Name`")
                            await event.reply("\n".join(lines))

                    elif cmd == "/add":
                        raw_arg = text.split(" ", 1)[1].strip() if " " in text else ""
                        if not raw_arg:
                            await event.reply("⚠️ *Usage*: `/add SearchTerm:DisplayName`\nExample: `/add El Pais:El País`")
                        else:
                            if ":" in raw_arg:
                                p_search, p_name = raw_arg.split(":", 1)
                                search_term, display_name = p_search.strip(), p_name.strip()
                            else:
                                search_term, display_name = raw_arg.strip(), raw_arg.strip()

                            existing = next((np for np in TARGET_NEWSPAPERS if np["name"].lower() == display_name.lower()), None)
                            if existing:
                                existing["search"] = search_term
                                save_target_newspapers(TARGET_NEWSPAPERS)
                                await event.reply(f"✏️ Updated newspaper *{display_name}* (Search term: `{search_term}`).")
                            else:
                                TARGET_NEWSPAPERS.append({"search": search_term, "name": display_name})
                                save_target_newspapers(TARGET_NEWSPAPERS)
                                await event.reply(f"✅ Added newspaper *{display_name}* (Search term: `{search_term}`).")

                    elif cmd == "/remove":
                        raw_arg = text.split(" ", 1)[1].strip() if " " in text else ""
                        if not raw_arg:
                            await event.reply("⚠️ *Usage*: `/remove DisplayName`\nExample: `/remove Canarias7`")
                        else:
                            target = raw_arg.lower()
                            matching = [np for np in TARGET_NEWSPAPERS if np["name"].lower() == target or np["search"].lower() == target]
                            if not matching:
                                await event.reply(f"❌ Newspaper *{raw_arg}* not found in active list.\nUse `/list` to view active newspapers.")
                            else:
                                for np in matching:
                                    TARGET_NEWSPAPERS.remove(np)
                                save_target_newspapers(TARGET_NEWSPAPERS)
                                await event.reply(f"🗑️ Removed *{raw_arg}* from target newspapers list.")

                    elif cmd in ("/help", "/start"):
                        help_text = (
                            "🤖 *WhatsApp Scraper Commands*:\n\n"
                            "• `/status` - Show today's delivery status for all newspapers\n"
                            "• `/list` - List all active target newspapers\n"
                            "• `/add SearchTerm:DisplayName` - Add or update a target newspaper\n"
                            "• `/remove DisplayName` - Remove a newspaper from the list\n"
                            "• `/help` - Show this help message"
                        )
                        await event.reply(help_text)

                await tg_client.run_until_disconnected()

            loop.run_until_complete(_init_and_run())
        except Exception as e:
            print(f"⚠️ Telegram listener thread stopped: {e}")


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