#!/usr/bin/env python3
"""
Telegram Media Bridge - MAXIMUM SPEED OPTIMIZED
Optimized for Render.com free tier
"""
import os
import re
import time
import logging
import tempfile
import threading
import asyncio
from functools import wraps
from flask import Flask, request, jsonify
from telethon import TelegramClient, errors
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
# বাকি কোড এখানে...
# Load environment
load_dotenv()

API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
OWNER_ID = int(os.environ.get('OWNER_ID'))
SESSION_NAME = os.environ.get('SESSION_NAME', 'user')
PORT = int(os.environ.get('PORT', 10000))

MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("telebridge")

STATE = {"phone": None, "sent_code": None, "awaiting": None, "logged_in": False}
DOWNLOAD_PROGRESS = {}
UPLOAD_PROGRESS = {}

app = Flask(__name__)

# -------------------
# TeleHelper
# -------------------
class TeleHelper:
    def __init__(self, api_id, api_hash, session_name):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        self.client = None
        self.thread.start()
        while not self.loop.is_running():
            time.sleep(0.01)

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def _init_client(self):
        if self.client is None:
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash, loop=self.loop)
            await self.client.connect()
        return self.client

    def send_code_request(self, phone):
        async def _send():
            client = await self._init_client()
            return await client.send_code_request(phone)
        return self.run_coro(_send())

    def sign_in_with_code(self, phone, code, phone_code_hash):
        async def _sign():
            client = await self._init_client()
            try:
                me = await client.sign_in(phone, code, phone_code_hash)
                return "ok", me
            except errors.SessionPasswordNeededError:
                return "password_needed", None
        return self.run_coro(_sign())

    def sign_in_with_password(self, password):
        async def _signpwd():
            client = await self._init_client()
            return await client.sign_in(password=password)
        return self.run_coro(_signpwd())

    def is_user_authorized(self):
        async def _check():
            client = await self._init_client()
            return await client.is_user_authorized()
        return self.run_coro(_check())

    def fetch_message_and_download(self, chat, msg_id):
        async def _fetch():
            client = await self._init_client()
            msg = await client.get_messages(chat, ids=msg_id)
            if not msg or not msg.media:
                return {"ok": False, "error": "No media"}

            ext = ".jpg" if "Photo" in str(msg.media) else ".mp4" if "Video" in str(msg.media) else ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                path = f.name

            await client.download_media(msg, file=path)
            size = os.path.getsize(path)
            if size > MAX_FILE_SIZE:
                os.unlink(path)
                return {"ok": False, "error": "File too large"}
            return {"ok": True, "file_path": path, "media_type": "photo" if ext==".jpg" else "video" if ext==".mp4" else "document", "text": msg.text or ""}
        return self.run_coro(_fetch())

    def upload_to_telegram_bot(self, file_path, caption, media_type):
        files = {}
        data = {"chat_id": OWNER_ID, "caption": caption[:1024]}

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        if media_type == "photo":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            files["photo"] = open(file_path, "rb")
        elif media_type == "video":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
            files["video"] = open(file_path, "rb")
        else:
            files["document"] = open(file_path, "rb")

        res = requests.post(url, data=data, files=files)
        for f in files.values(): f.close()
        if res.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": res.text}

# -------------------
tele = TeleHelper(API_ID, API_HASH, SESSION_NAME)
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

# -------------------
# Bot handlers
# -------------------
def owner_only(handler):
    @wraps(handler)
    def inner(update, context, *args, **kwargs):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("❌ অনুমোদিত নয়")
            return
        return handler(update, context, *args, **kwargs)
    return inner

@owner_only
def start(update, context):
    update.message.reply_text("🤖 MAX SPEED BOT Ready")

@owner_only
def login_cmd(update, context):
    STATE["awaiting"] = "phone"
    update.message.reply_text("📱 ফোন নম্বর পাঠান")

@owner_only
def status_cmd(update, context):
    status = "✅ লগইন" if tele.is_user_authorized() else "❌ লগআউট"
    update.message.reply_text(f"Status: {status}")

def text_handler(update, context):
    txt = update.message.text.strip()
    if STATE.get("awaiting") == "phone":
        phone = txt
        phone_hash = tele.send_code_request(phone)
        STATE.update({"phone": phone, "sent_code": phone_hash, "awaiting": "code"})
        update.message.reply_text("✅ কোড পাঠানো হয়েছে")
        return
    if STATE.get("awaiting") == "code":
        code = txt
        res, _ = tele.sign_in_with_code(STATE["phone"], code, STATE["sent_code"])
        if res == "ok":
            STATE.update({"logged_in": True, "awaiting": None})
            update.message.reply_text("🎉 লগইন সফল")
        elif res=="password_needed":
            STATE["awaiting"]="password"
            update.message.reply_text("🔒 পাসওয়ার্ড পাঠান")
        return
    if STATE.get("awaiting") == "password":
        pwd = txt
        tele.sign_in_with_password(pwd)
        STATE.update({"logged_in": True, "awaiting": None})
        update.message.reply_text("🎉 লগইন সফল")
        return

    # Parse t.me link
    m = re.search(r"https?://t\.me/((?:c/)?(\d+|[A-Za-z0-9_]+)/(\d+))", txt)
    if not m:
        update.message.reply_text("❌ সঠিক t.me লিংক পাঠান")
        return
    full_path = m.group(1)
    chat_part = m.group(2)
    msg_id = int(m.group(3))
    if full_path.startswith("c/"):
        chat = int("-100"+chat_part)
    else:
        chat = chat_part if chat_part.startswith("@") else f"@{chat_part}"

    update.message.reply_text("⚡ ডাউনলোড শুরু...")
    res = tele.fetch_message_and_download(chat, msg_id)
    if not res["ok"]:
        update.message.reply_text(f"❌ ডাউনলোড ব্যর্থ: {res.get('error')}")
        return
    file_path = res["file_path"]
    media_type = res["media_type"]
    caption = res["text"] or ""
    update.message.reply_text("🚀 আপলোড শুরু...")
    upl = tele.upload_to_telegram_bot(file_path, caption, media_type)
    if upl["ok"]:
        update.message.reply_text("🎉 আপলোড সম্পন্ন!")
    else:
        update.message.reply_text(f"❌ আপলোড ব্যর্থ: {upl.get('error')}")
    os.unlink(file_path)

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("login", login_cmd))
dp.add_handler(CommandHandler("status", status_cmd))
dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_handler))

# -------------------
@app.route("/")
def home(): return jsonify({"status":"active"})
@app.route("/health")
def health(): return jsonify({"status":"healthy"})

def start_bot():
    updater.start_polling()
    logger.info("✅ Bot ready")

if __name__=="__main__":
    start_bot()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
