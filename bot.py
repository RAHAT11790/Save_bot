#!/usr/bin/env python3
"""
Telegram Media Bridge - Fixed Version
Fast Download & Upload, Proper Media Types
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
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import (
    Document,
    Photo,
    DocumentAttributeVideo,
    DocumentAttributeFilename
)
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

app = Flask(__name__)

# Configuration from Environment Variables
API_ID = int(os.environ.get('API_ID', '25976192'))
API_HASH = os.environ.get('API_HASH', '8ba23141980539b4896e5adbc4ffd2e2')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8061585389:AAFT-3cubiYTU9VjX9VVYDE8Q6hh6mJJc-s')
OWNER_ID = int(os.environ.get('OWNER_ID', '6621572366'))
SESSION_NAME = os.environ.get('SESSION_NAME', 'user')
PORT = int(os.environ.get('PORT', 5000))
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger("telebridge")

# State
STATE = {
    "phone": None,
    "sent_code": None,
    "awaiting": None,
    "logged_in": False,
    "last_progress_update": 0
}

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
            self.client = TelegramClient(
                self.session_name, 
                self.api_id, 
                self.api_hash, 
                loop=self.loop,
                connection_retries=5
            )
            await self.client.connect()
        return self.client

    def send_code_request(self, phone):
        async def _send():
            client = await self._init_client()
            try:
                res = await client.send_code_request(phone)
                return res.phone_code_hash
            except Exception as e:
                logger.error(f"Code request failed: {e}")
                raise
        return self.run_coro(_send())

    def sign_in_with_code(self, phone, code, phone_code_hash):
        async def _sign():
            client = await self._init_client()
            try:
                me = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                return ("ok", me)
            except SessionPasswordNeededError:
                return ("password_needed", None)
            except Exception as e:
                logger.error(f"Sign-in failed: {e}")
                raise
        return self.run_coro(_sign())

    def sign_in_with_password(self, password):
        async def _signpwd():
            client = await self._init_client()
            try:
                me = await client.sign_in(password=password)
                return me
            except Exception as e:
                logger.error(f"Password sign-in failed: {e}")
                raise
        return self.run_coro(_signpwd())

    def is_user_authorized(self):
        async def _check():
            client = await self._init_client()
            return await client.is_user_authorized()
        return self.run_coro(_check())

    def detect_media_type_and_name(self, msg_media):
        """Detect media type and proper file name"""
        if not msg_media:
            return "document", "file.bin"
        
        media_type = "document"
        file_name = "file.bin"
        file_extension = ""
        
        try:
            # Check if it's a photo
            if hasattr(msg_media, 'photo') and msg_media.photo:
                media_type = "photo"
                file_extension = ".jpg"
            
            # Check if it's a document with attributes
            elif hasattr(msg_media, 'document'):
                document = msg_media.document
                
                # Check mime type
                if hasattr(document, 'mime_type') and document.mime_type:
                    mime_type = document.mime_type
                    if mime_type.startswith('image/'):
                        media_type = "photo"
                        file_extension = ".jpg"
                    elif mime_type.startswith('video/'):
                        media_type = "video"
                        file_extension = ".mp4"
                    elif mime_type.startswith('audio/'):
                        media_type = "audio"
                        file_extension = ".mp3"
                
                # Check document attributes for better detection
                if hasattr(document, 'attributes'):
                    for attr in document.attributes:
                        if isinstance(attr, DocumentAttributeVideo):
                            media_type = "video"
                            file_extension = ".mp4"
                        elif isinstance(attr, DocumentAttributeFilename):
                            file_name = attr.file_name
                            # Determine type from file extension
                            if file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                                media_type = "photo"
                            elif file_name.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
                                media_type = "video"
                            elif file_name.lower().endswith(('.mp3', '.wav', '.ogg')):
                                media_type = "audio"
            
            # If no proper name found, create one based on type
            if file_name == "file.bin" and file_extension:
                file_name = f"file{file_extension}"
                
        except Exception as e:
            logger.error(f"Error detecting media type: {e}")
        
        return media_type, file_name

    def fetch_message_and_download(self, from_chat, msg_id):
        async def _fetch():
            client = await self._init_client()
            
            try:
                logger.info(f"📨 Fetching message from {from_chat} with ID {msg_id}")
                
                # Get the message
                msg = await client.get_messages(from_chat, ids=msg_id)
                if not msg:
                    return {"ok": False, "error": "Message not found"}
                
                logger.info(f"✅ Message found: {msg.id}, Media: {msg.media}")
                
                if not msg.media:
                    return {"ok": True, "has_media": False, "text": msg.text or ""}
                
                # Detect media type and file name
                media_type, file_name = self.detect_media_type_and_name(msg.media)
                
                # Create proper temp file with correct extension
                file_extension = os.path.splitext(file_name)[1] or ".tmp"
                with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
                    dest_path = temp_file.name
                
                logger.info(f"📥 Downloading {media_type}: {file_name}")
                
                # Download with progress
                start_time = time.time()
                last_update = start_time
                
                def progress_callback(downloaded, total):
                    nonlocal last_update
                    current_time = time.time()
                    if current_time - last_update > 3:  # Update every 3 seconds
                        if total > 0:
                            percent = (downloaded / total) * 100
                            elapsed = current_time - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            speed_mb = speed / (1024 * 1024)
                            logger.info(f"⏳ Download: {percent:.1f}% | Speed: {speed_mb:.1f} MB/s")
                        last_update = current_time
                
                # Download the media
                path = await client.download_media(
                    msg, 
                    file=dest_path,
                    progress_callback=progress_callback
                )
                
                if path and os.path.exists(path):
                    file_size = os.path.getsize(path)
                    download_time = time.time() - start_time
                    speed = file_size / download_time if download_time > 0 else 0
                    speed_mb = speed / (1024 * 1024)
                    
                    logger.info(f"✅ Download complete: {file_size} bytes in {download_time:.1f}s ({speed_mb:.1f} MB/s)")
                    
                    if file_size > MAX_FILE_SIZE:
                        os.unlink(path)
                        return {"ok": False, "error": f"File too large ({file_size / (1024**2):.2f} MB > 2 GB)"}
                    
                    return {
                        "ok": True,
                        "has_media": True,
                        "file_path": path,
                        "text": msg.text or "",
                        "file_size": file_size / (1024**2),
                        "file_name": file_name,
                        "media_type": media_type,
                        "download_speed": speed_mb
                    }
                else:
                    return {"ok": False, "error": "Download failed - file not found"}
                
            except Exception as e:
                logger.error(f"❌ Download failed: {e}")
                return {"ok": False, "error": str(e)}
        
        return self.run_coro(_fetch())

    def upload_to_telegram_bot(self, file_path, caption, media_type, file_name):
        """Upload to bot using Telegram Bot API (fast)"""
        try:
            import requests
            from urllib.parse import quote
            
            # Prepare files and data
            files = {}
            data = {'chat_id': OWNER_ID}
            
            if caption:
                data['caption'] = caption[:1024]  # Telegram caption limit
            
            # Set appropriate parameter based on media type
            if media_type == "photo":
                files['photo'] = open(file_path, 'rb')
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            elif media_type == "video":
                files['video'] = open(file_path, 'rb')
                data['supports_streaming'] = True
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
            elif media_type == "audio":
                files['audio'] = open(file_path, 'rb')
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
            else:
                files['document'] = open(file_path, 'rb')
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
            
            # Upload with progress tracking
            file_size = os.path.getsize(file_path)
            start_time = time.time()
            
            def read_in_chunks(file_object, chunk_size=1024*1024):  # 1MB chunks
                while True:
                    data = file_object.read(chunk_size)
                    if not data:
                        break
                    yield data
            
            # For large files, we need to use different approach
            if file_size > 50 * 1024 * 1024:  # 50MB
                logger.info("📤 Using chunked upload for large file")
                response = requests.post(url, data=data, files=files, timeout=300)
            else:
                logger.info("📤 Uploading file directly")
                response = requests.post(url, data=data, files=files, timeout=300)
            
            # Close file handles
            for file_handle in files.values():
                file_handle.close()
            
            upload_time = time.time() - start_time
            speed = file_size / upload_time if upload_time > 0 else 0
            speed_mb = speed / (1024 * 1024)
            
            if response.status_code == 200:
                logger.info(f"✅ Upload complete in {upload_time:.1f}s ({speed_mb:.1f} MB/s)")
                return {"ok": True, "message": "File uploaded successfully"}
            else:
                error_msg = response.json().get('description', 'Unknown error')
                logger.error(f"❌ Upload failed: {error_msg}")
                return {"ok": False, "error": error_msg}
                
        except Exception as e:
            logger.error(f"❌ Upload failed: {e}")
            # Close file handles in case of error
            for file_handle in files.values():
                try:
                    file_handle.close()
                except:
                    pass
            return {"ok": False, "error": str(e)}

# Initialize TeleHelper
tele = TeleHelper(API_ID, API_HASH, SESSION_NAME)

# Bot setup
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

# Bot utilities
def owner_only(handler):
    @wraps(handler)
    def inner(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("❌ আপনি অনুমোদিত নন।")
            return
        return handler(update, context, *args, **kwargs)
    return inner

# Bot command handlers
@owner_only
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🤖 **Telegram Media Bridge Bot**\n\n"
        "📥 **Features:**\n"
        "• Fast download & upload\n"
        "• Photos as photos, Videos as videos\n" 
        "• Preserves original captions\n"
        "• 2GB file size limit\n\n"
        "🔐 **Commands:**\n"
        "/login - Start login process\n"
        "/status - Check bot status\n\n"
        "📝 **Usage:**\n"
        "Send any t.me link to download media"
    )

@owner_only
def login_cmd(update: Update, context: CallbackContext):
    STATE['awaiting'] = 'phone'
    update.message.reply_text("📱 **লগইন শুরু করুন**\n\nআপনার ফোন নম্বর পাঠান:\nউদাহরণ: `+8801XXXXXXXXX`")

@owner_only
def status_cmd(update: Update, context: CallbackContext):
    try:
        is_auth = tele.is_user_authorized()
        status = "✅ লগড ইন" if is_auth else "❌ লগড আউট"
        
        update.message.reply_text(
            f"🤖 **বট স্ট্যাটাস**\n\n"
            f"• লগইন: {status}\n"
            f"• Server: Render.com\n" 
            f"• Speed: Fast ⚡\n"
            f"• Media Types: Photo, Video, File\n"
        )
    except Exception as e:
        update.message.reply_text(f"❌ স্ট্যাটাস চেক করতে সমস্যা: {e}")

def text_message_handler(update: Update, context: CallbackContext):
    if update.effective_user.id != OWNER_ID:
        update.message.reply_text("❌ আপনি অনুমোদিত নন।")
        return
        
    txt = update.message.text.strip()
    logger.info(f"📩 Received: {txt}")

    # Handle login states
    if STATE.get('awaiting') == 'phone':
        phone = txt
        try:
            update.message.reply_text("📨 **কোড পাঠানো হচ্ছে...**")
            phone_code_hash = tele.send_code_request(phone)
            STATE.update({"phone": phone, "sent_code": phone_code_hash, "awaiting": 'code'})
            update.message.reply_text("✅ **কোড পাঠানো হয়েছে!**\nকোডটি পাঠান।")
        except Exception as e:
            update.message.reply_text(f"❌ কোড পাঠাতে ব্যর্থ: {e}")
        return

    if STATE.get('awaiting') == 'code':
        code = txt
        phone = STATE.get('phone')
        phone_code_hash = STATE.get('sent_code')
        if not phone or not phone_code_hash:
            update.message.reply_text("❌ /login দিয়ে আবার চেষ্টা করুন।")
            STATE['awaiting'] = None
            return
        try:
            res, info = tele.sign_in_with_code(phone, code, phone_code_hash)
            if res == "ok":
                STATE['logged_in'] = True
                STATE['awaiting'] = None
                update.message.reply_text("🎉 **লগইন সফল!**\nএবার t.me লিংক পাঠান।")
            elif res == "password_needed":
                STATE['awaiting'] = 'password'
                update.message.reply_text("🔒 **পাসওয়ার্ড পাঠান।**")
            else:
                update.message.reply_text("❌ লগইন ব্যর্থ।")
        except Exception as e:
            update.message.reply_text(f"❌ লগইন ব্যর্থ: {e}")
        return

    if STATE.get('awaiting') == 'password':
        password = txt
        try:
            tele.sign_in_with_password(password)
            STATE.update({"logged_in": True, "awaiting": None})
            update.message.reply_text("🎉 **লগইন সফল!**\nএবার t.me লিংক পাঠান।")
        except Exception as e:
            update.message.reply_text(f"❌ পাসওয়ার্ড ভেরিফিকেশন ব্যর্থ: {e}")
        return

    # Check login
    try:
        is_auth = tele.is_user_authorized()
        STATE['logged_in'] = is_auth
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        is_auth = False

    if not is_auth:
        update.message.reply_text("❌ **লগইন করুন!**\n/login দিয়ে লগইন করুন।")
        return

    # Parse t.me link
    m = re.search(r"https?://t\.me/((?:c/)?(\d+|[A-Za-z0-9_]+)/(\d+))", txt)
    if not m:
        update.message.reply_text("❌ **সঠিক t.me লিংক পাঠান**")
        return

    full_path = m.group(1)
    chat_part = m.group(2)
    msg_id = int(m.group(3))
    
    # Determine chat type
    if full_path.startswith("c/"):
        from_chat = int("-100" + chat_part)
    else:
        from_chat = chat_part if chat_part.startswith("@") else f"@{chat_part}"

    update.message.reply_text("⏳ **ডাউনলোড শুরু...**\nদ্রুত ডাউনলোড হবে ⚡")

    # Download media
    res = tele.fetch_message_and_download(from_chat, msg_id)
    
    if not res.get("ok"):
        err = res.get("error", "unknown")
        update.message.reply_text(f"❌ ডাউনলোড ব্যর্থ: {err}")
        return

    if not res.get("has_media"):
        text_content = res.get("text", "")
        update.message.reply_text(f"📝 **মিডিয়া নেই:**\n{text_content or '(খালি)'}")
        return

    # Upload media
    file_path = res.get("file_path")
    caption = res.get("text", "") or ""
    file_size = res.get("file_size", 0)
    file_name = res.get("file_name", "file")
    media_type = res.get("media_type", "document")
    download_speed = res.get("download_speed", 0)
    
    # Media type emoji
    emoji = {"photo": "🖼️", "video": "🎥", "audio": "🎵"}.get(media_type, "📄")
    
    update.message.reply_text(
        f"✅ **ডাউনলোড সম্পন্ন!**\n\n"
        f"• {emoji} টাইপ: {media_type}\n"
        f"• ফাইল: {file_name}\n"
        f"• সাইজ: {file_size:.1f} MB\n"
        f"• স্পিড: {download_speed:.1f} MB/s\n"
        f"• 📤 আপলোড শুরু..."
    )

    # Upload using Bot API (fast)
    upload_res = tele.upload_to_telegram_bot(file_path, caption, media_type, file_name)
    
    if upload_res.get("ok"):
        update.message.reply_text(f"🎉 **{emoji} {media_type} আপলোড সম্পন্ন!** ✅")
    else:
        error_msg = upload_res.get('error', 'Unknown error')
        update.message.reply_text(f"❌ **আপলোড ব্যর্থ:** {error_msg}")

    # Clean up
    if os.path.exists(file_path):
        os.unlink(file_path)

# Add handlers
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("login", login_cmd))
dp.add_handler(CommandHandler("status", status_cmd))
dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_message_handler))

# Flask Routes
@app.route('/')
def home():
    return jsonify({"status": "active", "service": "Telegram Media Bridge"})

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy"})

def start_bot():
    logger.info("🚀 Starting Telegram Bot...")
    updater.start_polling()
    logger.info("✅ Bot started successfully!")

if __name__ == '__main__':
    start_bot()
    app.run(host='0.0.0.0', port=PORT, debug=False)
