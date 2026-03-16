import os
import sys
import time
from datetime import datetime, date
from dotenv import load_dotenv
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv
from neonize.utils import build_jid
from naming_utils import get_newspaper_name

# --- CONFIGURATION (Loaded from .env) ---
load_dotenv()

TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID")
TARGET_RECIPIENT = os.getenv("TARGET_RECIPIENT")
SEARCH_TERM = os.getenv("SEARCH_TERM", "La Provincia Las Palmas")

DOWNLOAD_PATH = "downloads"
SENT_LOG_FILE = "last_sent.txt"

# --- STATE TRACKING ---
PROCESSED_MESSAGES = set()
LAST_SENT_DATE = None

if os.path.exists(SENT_LOG_FILE):
    with open(SENT_LOG_FILE, "r") as f:
        LAST_SENT_DATE = f.read().strip()

client = NewClient("session.db")


def save_sent_date():
    today_str = str(date.today())
    with open(SENT_LOG_FILE, "w") as f:
        f.write(today_str)
    return today_str


def send_to_target(client, file_path, custom_name):
    """Builds the document and sends it to a properly formatted JID."""
    print(f"📤 Sending '{custom_name}' to {TARGET_RECIPIENT}...")
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()

        doc_msg = client.build_document_message(
            file_data,
            filename=custom_name,
            caption=f"Here is your newspaper: {custom_name}",
            mimetype="application/pdf"
        )

        # Format the phone number properly
        clean_number = TARGET_RECIPIENT.replace("@s.whatsapp.net", "")
        target_jid = build_jid(clean_number)

        client.send_message(target_jid, message=doc_msg)

        print(f"🚀 Sent successfully!")
        return True
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        return False


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
        if send_to_target(client, path, custom_name):
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

                    if send_to_target(client, path, custom_name):
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
        if str(date.today()) == LAST_SENT_DATE:
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
    print(f"🚀 Monitoring Group: {TARGET_GROUP_ID}")
    status = "Pending" if LAST_SENT_DATE != str(date.today()) else "Completed"
    print(f"📅 Today's status: {status}")


if __name__ == "__main__":
    try:
        client.connect()
    except KeyboardInterrupt:
        print("\n👋 Shutting down safely...")
        sys.exit(0)