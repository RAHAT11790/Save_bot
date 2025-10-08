#!/usr/bin/env python3
"""
Telegram Media Bridge - Flask Version for Render.com
Media Type Detection Fixed - Compatible with latest Telethon
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
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

app = Flask(__name__)

# Configuration from Environment Variables
API_ID = int(os.environ.get('API_ID', '25976192'))
API_HASH = os.environ.get('API_HASH', '8ba23141980539b4896e5adbc4ffd2e2'))
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8061585389:AAFT-3cubiYTU9VjX9VVYDE8Q6hh6mJJc-s')
OWNER_ID = int(os.environ.get('OWNER_ID', '6621572366'))
SESSION_NAME = os.environ.get('SESSION_NAME', 'user')
RENDER_URL = os.environ.get('RENDER_URL', '')
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash, loop=self.loop)
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

    def detect_media_type(self, msg_media):
        """Detect media type from message media"""
        if not msg_media:
            return "document"
        
        media_class_name = msg_media.__class__.__name__
        logger.info(f"Media class: {media_class_name}")
        
        if 'Photo' in media_class_name:
            return "photo"
        elif 'Video' in media_class_name:
            return "video"
        elif 'Document' in media_class_name:
            # Check mime type for documents
            if hasattr(msg_media, 'document'):
                document = msg_media.document
                if hasattr(document, 'mime_type') and document.mime_type:
                    mime_type = document.mime_type
                    if mime_type.startswith('image/'):
                        return "photo"
                    elif mime_type.startswith('video/'):
                        return "video"
            return "document"
        else:
            return "document"

    def fetch_message_and_download(self, from_chat, msg_id, dest_path):
        async def _fetch():
            client = await self._init_client()
            start_time = time.time()

            async def progress_callback(downloaded, total):
                if total == 0:
                    return
                percent = (downloaded / total) * 100
                elapsed = time.time() - start_time
                if elapsed > 0:
                    speed = downloaded / elapsed
                    remaining = total - downloaded
                    eta = remaining / speed if speed > 0 else 0
                    eta_str = f"{int(eta // 60)}m {int(eta % 60)}s"
                else:
                    eta_str = "Unknown"
                if time.time() - STATE["last_progress_update"] > 5:
                    logger.info(f"⏳ Download: {percent:.1f}% (ETA: {eta_str})")
                    STATE["last_progress_update"] = time.time()

            try:
                logger.info(f"Fetching message from {from_chat} with ID {msg_id}")
                
                msg = await client.get_messages(from_chat, ids=msg_id)
                if not msg:
                    return {"ok": False, "error": "Message not found"}
                
                logger.info(f"Message found: {msg.id}, Media: {msg.media}")
                
                if msg.media:
                    logger.info("Downloading media...")
                    path = await client.download_media(
                        msg, 
                        file=dest_path,
                        progress_callback=progress_callback
                    )
                    
                    if path and os.path.exists(path):
                        file_size = os.path.getsize(path)
                        logger.info(f"Download completed: {path}, Size: {file_size} bytes")
                        
                        if file_size > MAX_FILE_SIZE:
                            os.unlink(path)
                            return {"ok": False, "error": f"File too large ({file_size / (1024**2):.2f} MB > 2 GB)"}
                        
                        # Detect media type
                        media_type = self.detect_media_type(msg.media)
                        
                        return {
                            "ok": True,
                            "has_media": True,
                            "file_path": path,
                            "text": msg.text or "",
                            "file_size": file_size / (1024**2),
                            "file_name": os.path.basename(path),
                            "media_type": media_type
                        }
                    else:
                        return {"ok": False, "error": "Download failed - file not found"}
                
                return {"ok": True, "has_media": False, "text": msg.text or ""}
                
            except Exception as e:
                logger.error(f"Download failed: {e}")
                return {"ok": False, "error": str(e)}
        
        return self.run_coro(_fetch())

    def upload_to_telegram(self, file_path, caption, chat_id, media_type="document"):
        async def _upload():
            client = await self._init_client()
            start_time = time.time()

            async def progress_callback(uploaded, total):
                if total == 0:
                    return
                percent = (uploaded / total) * 100
                elapsed = time.time() - start_time
                if elapsed > 0:
                    speed = uploaded / elapsed
                    remaining = total - uploaded
                    eta = remaining / speed if speed > 0 else 0
                    eta_str = f"{int(eta // 60)}m {int(eta % 60)}s"
                else:
                    eta_str = "Unknown"
                if time.time() - STATE["last_progress_update"] > 5:
                    logger.info(f"⏳ Upload: {percent:.1f}% (ETA: {eta_str})")
                    STATE["last_progress_update"] = time.time()

            try:
                logger.info(f"Uploading {media_type}: {file_path} to chat {chat_id}")
                
                # Determine upload parameters based on media type
                if media_type == "photo":
                    # Send as photo
                    result = await client.send_file(
                        chat_id, 
                        file=file_path,
                        caption=caption[:1024] if caption else None,
                        progress_callback=progress_callback,
                        force_document=False
                    )
                elif media_type == "video":
                    # Send as video
                    result = await client.send_file(
                        chat_id, 
                        file=file_path,
                        caption=caption[:1024] if caption else None,
                        progress_callback=progress_callback,
                        force_document=False,
                        supports_streaming=True
                    )
                else:
                    # Send as document
                    result = await client.send_file(
                        chat_id, 
                        file=file_path,
                        caption=caption[:1024] if caption else None,
                        progress_callback=progress_callback,
                        force_document=True
                    )
                
                logger.info(f"Upload successful: {result}")
                return {"ok": True, "message": f"{media_type.capitalize()} uploaded successfully"}
                
            except Exception as e:
                logger.error(f"Upload failed: {e}")
                return {"ok": False, "error": str(e)}
        
        return self.run_coro(_upload())

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
        "• Download media from any t.me link\n"
        "• Upload to your chat with original format\n"
        "• Photos as photos, Videos as videos, Files as files\n"
        "• Preserves original captions\n"
        "• 2GB file size limit\n\n"
        "🔐 **Commands:**\n"
        "/login - Start login process\n"
        "/logout - Logout & clear session\n"
        "/status - Check bot status\n\n"
        "📝 **Usage:**\n"
        "1. First /login with your phone\n"
        "2. Send any t.me link\n"
        "3. Bot will download & upload media in original format"
    )

@owner_only
def login_cmd(update: Update, context: CallbackContext):
    STATE['awaiting'] = 'phone'
    update.message.reply_text("📱 **লগইন শুরু করুন**\n\nআপনার ফোন নম্বর পাঠান (আন্তর্জাতিক ফরম্যাটে):\nউদাহরণ: `+8801XXXXXXXXX`")

@owner_only
def logout_cmd(update: Update, context: CallbackContext):
    try:
        tele.run_coro(tele._init_client())
    except Exception:
        pass
    path = SESSION_NAME + ".session"
    if os.path.exists(path):
        os.remove(path)
    STATE.update({"logged_in": False, "awaiting": None, "phone": None, "sent_code": None})
    update.message.reply_text("✅ লগআউট সম্পন্ন এবং সেশন ফাইল ডিলিট করা হয়েছে।")

@owner_only
def status_cmd(update: Update, context: CallbackContext):
    try:
        is_auth = tele.is_user_authorized()
        status = "✅ লগড ইন" if is_auth else "❌ লগড আউট"
        state_info = f"বর্তমান স্টেট: {STATE.get('awaiting', 'None')}"
        
        update.message.reply_text(
            f"🤖 **বট স্ট্যাটাস**\n\n"
            f"• লগইন স্ট্যাটাস: {status}\n"
            f"• {state_info}\n"
            f"• Server: Render.com\n"
            f"• Media Types: Photo, Video, Document\n"
            f"• Uptime: Active"
        )
    except Exception as e:
        update.message.reply_text(f"❌ স্ট্যাটাস চেক করতে সমস্যা: {e}")

def text_message_handler(update: Update, context: CallbackContext):
    # Owner check
    if update.effective_user.id != OWNER_ID:
        update.message.reply_text("❌ আপনি অনুমোদিত নন।")
        return
        
    txt = update.message.text.strip()
    logger.info(f"Received message from {update.effective_user.id}: {txt}")

    # Handle login states
    if STATE.get('awaiting') == 'phone':
        phone = txt
        try:
            update.message.reply_text("📨 **কোড পাঠানো হচ্ছে...**\nদয়া করে অপেক্ষা করুন।")
            phone_code_hash = tele.send_code_request(phone)
            STATE.update({"phone": phone, "sent_code": phone_code_hash, "awaiting": 'code'})
            update.message.reply_text("✅ **কোড পাঠানো হয়েছে!**\n\nএবার Telegram/SMS থেকে প্রাপ্ত কোডটি পাঠান।")
        except Exception as e:
            logger.error(f"Code request failed: {e}")
            update.message.reply_text(f"❌ কোড পাঠাতে ব্যর্থ: {e}")
        return

    if STATE.get('awaiting') == 'code':
        code = txt
        phone = STATE.get('phone')
        phone_code_hash = STATE.get('sent_code')
        if not phone or not phone_code_hash:
            update.message.reply_text("❌ সেশন তথ্য পাওয়া যায়নি। /login দিয়ে আবার চেষ্টা করুন।")
            STATE['awaiting'] = None
            return
        try:
            res, info = tele.sign_in_with_code(phone, code, phone_code_hash)
            if res == "ok":
                STATE['logged_in'] = True
                STATE['awaiting'] = None
                update.message.reply_text("🎉 **লগইন সফল!**\n\nএবার কোনো t.me লিংক পাঠান ডাউনলোডের জন্য।")
            elif res == "password_needed":
                STATE['awaiting'] = 'password'
                update.message.reply_text("🔒 **Two-Step Verification**\n\nআপনার পাসওয়ার্ড পাঠান।")
            else:
                update.message.reply_text("❌ লগইন ব্যর্থ। আবার চেষ্টা করুন।")
        except Exception as e:
            logger.error(f"Sign-in failed: {e}")
            update.message.reply_text(f"❌ লগইন ব্যর্থ: {e}")
        return

    if STATE.get('awaiting') == 'password':
        password = txt
        try:
            tele.sign_in_with_password(password)
            STATE.update({"logged_in": True, "awaiting": None})
            update.message.reply_text("🎉 **লগইন সফল!**\n\nএবার কোনো t.me লিংক পাঠান ডাউনলোডের জন্য।")
        except Exception as e:
            logger.error(f"Password auth failed: {e}")
            update.message.reply_text(f"❌ পাসওয়ার্ড ভেরিফিকেশন ব্যর্থ: {e}")
        return

    # Check if user is logged in
    try:
        is_auth = tele.is_user_authorized()
        STATE['logged_in'] = is_auth
        logger.info(f"User authorized: {is_auth}")
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        is_auth = False

    if not is_auth:
        update.message.reply_text("❌ **আপনি লগইন করেননি!**\n\nলগইন করতে /login কমান্ড ব্যবহার করুন।")
        return

    # Parse t.me link
    logger.info("Parsing t.me link...")
    m = re.search(r"https?://t\.me/((?:c/)?(\d+|[A-Za-z0-9_]+)/(\d+))", txt)
    if not m:
        update.message.reply_text(
            "❌ **সঠিক t.me লিংক পাঠান**\n\n"
            "উদাহরণ:\n"
            "• `https://t.me/c/123456789/123`\n"
            "• `https://t.me/username/123`"
        )
        return

    full_path = m.group(1)
    chat_part = m.group(2)
    msg_id = int(m.group(3))
    
    logger.info(f"Parsed - Full: {full_path}, Chat: {chat_part}, Msg ID: {msg_id}")

    # Determine chat type
    if full_path.startswith("c/"):
        from_chat = int("-100" + chat_part)
    else:
        from_chat = chat_part if chat_part.startswith("@") else f"@{chat_part}"

    logger.info(f"Final chat: {from_chat}")

    STATE["last_progress_update"] = time.time()
    update.message.reply_text("⏳ **মেসেজ খোঁজা হচ্ছে...**\nমিডিয়া ডাউনলোড শুরু হবে shortly.")

    # Create temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as temp_file:
        dest_path = temp_file.name

    logger.info(f"Temp file created: {dest_path}")

    # Download media
    try:
        res = tele.fetch_message_and_download(from_chat, msg_id, dest_path)
    except Exception as e:
        logger.error(f"Download process failed: {e}")
        update.message.reply_text(f"❌ ডাউনলোড প্রসেস ব্যর্থ: {e}")
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        return
    
    if not res.get("ok"):
        err = res.get("error", "unknown")
        logger.error(f"Download failed: {err}")
        update.message.reply_text(f"❌ মেসেজ/মিডিয়া ফেচ করা যায়নি: {err}")
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        return

    if not res.get("has_media"):
        text_content = res.get("text", "")
        update.message.reply_text(f"📝 **মেসেজে মিডিয়া নেই:**\n\n{text_content or '(খালি মেসেজ)'}")
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        return

    # Upload media with correct type
    file_path = res.get("file_path")
    caption = res.get("text", "") or "Recovered media"
    file_size = res.get("file_size", 0)
    file_name = res.get("file_name", "file")
    media_type = res.get("media_type", "document")
    
    # Media type emoji mapping
    type_emojis = {
        "photo": "🖼️",
        "video": "🎥", 
        "document": "📄"
    }
    
    emoji = type_emojis.get(media_type, "📁")
    
    logger.info(f"Download successful: {file_path}, Type: {media_type}, Size: {file_size:.2f} MB")
    update.message.reply_text(
        f"✅ **ডাউনলোড সফল!**\n\n"
        f"• {emoji} টাইপ: `{media_type}`\n"
        f"• ফাইল: `{file_name}`\n"
        f"• সাইজ: `{file_size:.2f} MB`\n"
        f"• আপলোড শুরু হচ্ছে..."
    )

    STATE["last_progress_update"] = time.time()
    chat_id = update.effective_chat.id
    
    logger.info(f"Starting upload as {media_type} to chat {chat_id}")
    
    try:
        upload_res = tele.upload_to_telegram(file_path, caption, chat_id, media_type)
    except Exception as e:
        logger.error(f"Upload process failed: {e}")
        update.message.reply_text(f"❌ আপলোড প্রসেস ব্যর্থ: {e}")
        if os.path.exists(file_path):
            os.unlink(file_path)
        return
    
    if upload_res.get("ok"):
        update.message.reply_text(f"🎉 **{emoji} {media_type.capitalize()} আপলোড সম্পন্ন!** ✅")
        logger.info(f"{media_type} upload completed successfully")
    else:
        error_msg = upload_res.get('error', 'Unknown error')
        update.message.reply_text(f"❌ **আপলোড ব্যর্থ:** {error_msg}")
        logger.error(f"Upload failed: {error_msg}")

    # Clean up
    if os.path.exists(file_path):
        os.unlink(file_path)
        logger.info("Temp file cleaned up")

# Add handlers to dispatcher
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("login", login_cmd))
dp.add_handler(CommandHandler("logout", logout_cmd))
dp.add_handler(CommandHandler("status", status_cmd))
dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_message_handler))

# Flask Routes for Render.com
@app.route('/')
def home():
    return jsonify({
        "status": "active",
        "service": "Telegram Media Bridge",
        "version": "2.0",
        "media_types": "photo, video, document",
        "deployed_on": "Render.com"
    })

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "timestamp": time.time()})

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram bot"""
    update = Update.de_json(request.get_json(), updater.bot)
    dp.process_update(update)
    return jsonify({"status": "ok"})

def start_bot():
    """Start the bot in polling mode"""
    logger.info("Starting Telegram Bot...")
    
    # Set webhook if RENDER_URL is provided
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        updater.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")
    else:
        # Use polling
        updater.start_polling()
        logger.info("Bot started with polling")
    
    return "Bot started successfully"

# Start bot when app runs
if __name__ == '__main__':
    start_bot()
    app.run(host='0.0.0.0', port=5000, debug=False)
