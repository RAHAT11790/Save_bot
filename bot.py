#!/usr/bin/env python3
import os
import re
import tempfile
import threading
import asyncio
import logging
from functools import wraps
from flask import Flask
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
import requests

# Load environment variables
load_dotenv()

# ------------------------
# Config
# ------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SESSION_NAME = os.getenv("SESSION_NAME", "user")
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GB
PORT = int(os.getenv("PORT", "10000"))

if not all([API_ID, API_HASH, BOT_TOKEN, OWNER_ID]):
    raise ValueError("Missing required environment variables")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telebridge")

# ------------------------
# Global state
# ------------------------
STATE = {"awaiting": None, "phone": None, "sent_code": None, "logged_in": False}

# ------------------------
# Flask app
# ------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

# ------------------------
# Telethon helper
# ------------------------
class TeleHelper:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.thread = threading.Thread(target=self.start_loop, daemon=True)
        self.thread.start()

    def start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def init_client(self):
        if not self.client:
            self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=self.loop)
            await self.client.connect()
            await self.client.start()
            logger.info("Telethon client connected & started")
        return self.client

    def send_code(self, phone):
        async def _send():
            client = await self.init_client()
            return await client.send_code_request(phone)
        return self.run(_send())

    def sign_in_code(self, phone, code, hash_):
        async def _sign():
            client = await self.init_client()
            return await client.sign_in(phone=phone, code=code, phone_code_hash=hash_)
        return self.run(_sign())

    def is_auth(self):
        async def _check():
            client = await self.init_client()
            return await client.is_user_authorized()
        return self.run(_check())

    def download_msg(self, chat, msg_id):
        async def _dl():
            client = await self.init_client()
            msg = await client.get_messages(chat, ids=msg_id)

            if not msg or not msg.media:
                return {"ok": False, "error": "No media"}

            # Determine file extension
            if "Photo" in str(msg.media):
                ext = ".jpg"
                media_type = "photo"
            elif "Video" in str(msg.media):
                ext = ".mp4"
                media_type = "video"
            else:
                ext = ".bin"
                media_type = "doc"

            # Temp file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            temp_file.close()
            file_path = await client.download_media(msg, file=temp_file.name)

            # Check size
            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                os.unlink(file_path)
                return {"ok": False, "error": "File too large"}

            return {"ok": True, "file": file_path, "media": media_type, "caption": msg.text or ""}

        return self.run(_dl())

    def upload(self, path, media, caption):
        data = {"chat_id": OWNER_ID}
        if caption:
            data["caption"] = caption[:1024]

        if media == "photo":
            files = {"photo": open(path, "rb")}
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        elif media == "video":
            files = {"video": open(path, "rb")}
            data["supports_streaming"] = True
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
        else:
            files = {"document": open(path, "rb")}
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

        try:
            r = requests.post(url, data=data, files=files, timeout=120)
        finally:
            for f in files.values():
                f.close()

        return r.status_code == 200

# ------------------------
# Initialize TeleHelper
tele = TeleHelper()

# ------------------------
# Telegram Bot
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

def owner_only(f):
    @wraps(f)
    def inner(update, context):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("❌ অনুমোদিত নন।")
            return
        return f(update, context)
    return inner

@owner_only
def start(update, context):
    update.message.reply_text("🤖 MAX SPEED BOT\nSend t.me link → Fast Download → Fast Upload")

@owner_only
def login(update, context):
    STATE["awaiting"] = "phone"
    update.message.reply_text("📱 ফোন নম্বর পাঠান (e.g +8801XXXX)")

def handle_text(update, context):
    txt = update.message.text.strip()

    # Handle login
    if STATE.get("awaiting") == "phone":
        phone = txt
        STATE["phone"] = phone
        try:
            hash_ = tele.send_code(phone)
            STATE["sent_code"] = hash_
            STATE["awaiting"] = "code"
            update.message.reply_text("✅ কোড পাঠানো হয়েছে!")
        except Exception as e:
            update.message.reply_text(f"❌ Failed: {e}")
        return

    if STATE.get("awaiting") == "code":
        code = txt
        phone = STATE.get("phone")
        hash_ = STATE.get("sent_code")
        try:
            tele.sign_in_code(phone, code, hash_)
            STATE["logged_in"] = True
            STATE["awaiting"] = None
            update.message.reply_text("🎉 লগইন সফল!")
        except Exception as e:
            update.message.reply_text(f"❌ Failed: {e}")
        return

    # Check t.me link
    m = re.search(r"https?://t\.me/(c/\d+|[\w_]+)/(\d+)", txt)
    if not m:
        update.message.reply_text("❌ সঠিক t.me লিংক পাঠান")
        return

    chat = m.group(1)
    msg_id = int(m.group(2))
    update.message.reply_text("⚡ ডাউনলোড শুরু হচ্ছে...")

    res = tele.download_msg(chat, msg_id)
    if not res.get("ok"):
        update.message.reply_text(f"❌ Download failed: {res.get('error')}")
        return

    path = res["file"]
    media = res["media"]
    caption = res.get("caption", "")

    update.message.reply_text("✅ DOWNLOAD COMPLETE")

    if tele.upload(path, media, caption):
        update.message.reply_text("🎉 UPLOAD COMPLETE")
    else:
        update.message.reply_text("❌ Upload failed")

    os.unlink(path)

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("login", login))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

# ------------------------
# Run Flask + Bot
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    updater.start_polling()
    updater.idle()
