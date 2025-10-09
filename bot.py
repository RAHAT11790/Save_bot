#!/usr/bin/env python3
"""
Telegram Media Bridge - MAX SPEED
Optimized for Render.com (Python 3.10)
"""

import os
import re
import time
import tempfile
import threading
import asyncio
import logging
from functools import wraps
from flask import Flask, jsonify
from telethon import TelegramClient, errors
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import requests

# -----------------------
# Configuration
# -----------------------
API_ID = int(os.environ.get('API_ID', ''))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = int(os.environ.get('OWNER_ID', ''))
SESSION_NAME = os.environ.get('SESSION_NAME', 'session')
PORT = int(os.environ.get('PORT', 10000))
MAX_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB

# Validate environment
if not all([API_ID, API_HASH, BOT_TOKEN, OWNER_ID]):
    raise ValueError("❌ Missing environment variables")

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("telebridge")

# Global State
STATE = {"phone": None, "sent_code": None, "awaiting": None, "logged_in": False}

# -----------------------
# TeleHelper
# -----------------------
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
                me = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
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
            # Detect file type
            media_type = "photo" if "Photo" in str(msg.media) else "video" if "Video" in str(msg.media) else "document"
            ext = ".jpg" if media_type=="photo" else ".mp4" if media_type=="video" else ".bin"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            path = await client.download_media(msg, file=tmp.name)
            size = os.path.getsize(path)
            if size > MAX_FILE_SIZE:
                os.unlink(path)
                return {"ok": False, "error": "File too large (>1GB)"}
            return {"ok": True, "file_path": path, "media_type": media_type, "file_size": size, "text": msg.text or ""}
        return self.run_coro(_fetch())

    def upload_to_bot(self, file_path, caption, media_type):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/"
        data = {"chat_id": OWNER_ID, "caption": caption[:1024]}
        files = {}
        if media_type=="photo":
            files['photo'] = open(file_path,'rb')
            method = "sendPhoto"
        elif media_type=="video":
            files['video'] = open(file_path,'rb')
            data['supports_streaming'] = True
            method = "sendVideo"
        else:
            files['document'] = open(file_path,'rb')
            method = "sendDocument"
        res = requests.post(url+method, data=data, files=files)
        for f in files.values(): f.close()
        return res.json()

tele = TeleHelper(API_ID, API_HASH, SESSION_NAME)

# -----------------------
# Flask
# -----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status":"active"})

@app.route('/health')
def health():
    return jsonify({"status":"healthy"})

# -----------------------
# Telegram Bot
# -----------------------
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

def owner_only(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("❌ অনুমোদিত নয়")
            return
        return func(update, context, *args, **kwargs)
    return wrapper

@owner_only
def start_cmd(update, context):
    update.message.reply_text("🤖 MAX SPEED BOT READY\n\nSend t.me link to fetch media.")

@owner_only
def text_handler(update, context):
    txt = update.message.text.strip()
    if STATE.get('awaiting')=='phone':
        phone = txt
        phone_code = tele.send_code_request(phone)
        STATE.update({"phone": phone, "sent_code": phone_code, "awaiting": "code"})
        update.message.reply_text("📨 Code sent. Send the code here.")
        return
    if STATE.get('awaiting')=='code':
        code = txt
        phone = STATE['phone']
        code_hash = STATE['sent_code']
        res, _ = tele.sign_in_with_code(phone, code, code_hash)
        if res=="ok":
            STATE.update({"awaiting": None, "logged_in": True})
            update.message.reply_text("🎉 Logged in successfully! Send t.me link.")
        elif res=="password_needed":
            STATE['awaiting']="password"
            update.message.reply_text("🔒 Send 2FA password.")
        return
    if STATE.get('awaiting')=='password':
        password = txt
        tele.sign_in_with_password(password)
        STATE.update({"awaiting": None, "logged_in": True})
        update.message.reply_text("🎉 Logged in successfully! Send t.me link.")
        return

    # t.me link parse
    m = re.search(r"https?://t\.me/((?:c/)?(\d+|[A-Za-z0-9_]+)/(\d+))", txt)
    if not m:
        update.message.reply_text("❌ Send valid t.me link")
        return
    full_path, chat_part, msg_id = m.group(1), m.group(2), int(m.group(3))
    chat = int("-100"+chat_part) if full_path.startswith("c/") else f"@{chat_part}"

    update.message.reply_text("⚡ Downloading...")
    res = tele.fetch_message_and_download(chat, msg_id)
    if not res.get("ok"):
        update.message.reply_text(f"❌ {res.get('error')}")
        return
    file_path, media_type, caption = res['file_path'], res['media_type'], res['text']
    update.message.reply_text(f"✅ Download complete ({tele.format_size(res['file_size'])})\nUploading...")

    up_res = tele.upload_to_bot(file_path, caption, media_type)
    if up_res.get("ok"):
        update.message.reply_text("🎉 Upload successful!")
    else:
        update.message.reply_text(f"❌ Upload failed: {up_res}")

    if os.path.exists(file_path):
        os.unlink(file_path)

dp.add_handler(CommandHandler("start", start_cmd))
dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_handler))

def start_bot():
    updater.start_polling()
    logger.info("🚀 Bot running")

if __name__=="__main__":
    start_bot()
    app.run(host='0.0.0.0', port=PORT)
