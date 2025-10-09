#!/usr/bin/env python3
"""
High Speed Telegram Media Bot with Phone Login
Python 3.11 compatible for Render
"""

import os
import re
import asyncio
import logging
import tempfile
import threading
from functools import wraps
from flask import Flask, jsonify
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaVideo
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, ConversationHandler

# -----------------------
# CONFIGURATION
# -----------------------
API_ID = int(os.environ.get('API_ID', ''))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = int(os.environ.get('OWNER_ID', ''))

if not all([API_ID, API_HASH, BOT_TOKEN, OWNER_ID]):
    raise ValueError("Missing required environment variables!")

# Configuration
MAX_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1GB
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks for large files

# Conversation states
PHONE, CODE, PASSWORD = range(3)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("TelegramMediaBot")

# -----------------------
# FLASK APP (for Render & Uptime Robot)
# -----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "Bot is running", "service": "telegram-media-bot"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "bot": "active"})

@app.route('/ping')
def ping():
    return jsonify({"status": "pong"})

# -----------------------
# TELETHON CLIENT MANAGER
# -----------------------
class TelethonManager:
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TelethonManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.client = None
        self.is_connected = False
        self.phone_code_hash = None
        self.phone_number = None
        self._initialized = True
    
    def initialize_client(self):
        """Initialize Telethon client"""
        try:
            self.client = TelegramClient(
                'media_bot_session',
                API_ID,
                API_HASH,
                device_model="High Speed Media Bot",
                system_version="4.0",
                app_version="2.0"
            )
            logger.info("Telethon client initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize client: {e}")
            return False
    
    async def connect_with_phone(self, phone_number):
        """Start phone number authentication"""
        try:
            if not self.client:
                self.initialize_client()
            
            await self.client.connect()
            self.phone_number = phone_number
            result = await self.client.send_code_request(phone_number)
            self.phone_code_hash = result.phone_code_hash
            return True, "Code sent successfully"
        except Exception as e:
            logger.error(f"Phone auth error: {e}")
            return False, str(e)
    
    async def verify_code(self, code):
        """Verify authentication code"""
        try:
            if not self.client or not self.phone_code_hash:
                return False, "No active authentication session"
            
            await self.client.sign_in(
                phone=self.phone_number,
                code=code,
                phone_code_hash=self.phone_code_hash
            )
            self.is_connected = True
            return True, "Successfully authenticated"
        except Exception as e:
            logger.error(f"Code verification error: {e}")
            return False, str(e)
    
    async def download_media(self, chat_identifier, message_id):
        """Download media with chunked download for large files"""
        try:
            message = await self.client.get_messages(chat_identifier, ids=message_id)
            
            if not message or not message.media:
                return {"success": False, "error": "Message or media not found"}
            
            # Get file size first
            if hasattr(message.media, 'document'):
                file_size = message.media.document.size
            elif hasattr(message.media, 'photo'):
                file_size = message.media.photo.sizes[-1].size if message.media.photo.sizes else 0
            else:
                file_size = 0
            
            if file_size > MAX_FILE_SIZE:
                return {"success": False, "error": f"File too large ({file_size/1024/1024/1024:.2f}GB > 1GB)"}
            
            # Create temporary file
            file_ext = self._get_file_extension(message.media)
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_path = temp_file.name
            
            # Download file
            downloaded_path = await self.client.download_media(
                message, 
                file=temp_path
            )
            
            actual_size = os.path.getsize(downloaded_path)
            
            return {
                "success": True,
                "file_path": downloaded_path,
                "file_size": actual_size,
                "file_type": self._get_media_type(message.media),
                "caption": message.text or "",
                "file_extension": file_ext
            }
            
        except Exception as e:
            logger.error(f"Download error: {e}")
            return {"success": False, "error": str(e)}
    
    def _get_file_extension(self, media):
        """Determine file extension based on media type"""
        if isinstance(media, MessageMediaPhoto):
            return ".jpg"
        elif isinstance(media, MessageMediaVideo):
            return ".mp4"
        elif isinstance(media, MessageMediaDocument):
            if media.document.mime_type:
                if 'image' in media.document.mime_type:
                    return ".jpg"
                elif 'video' in media.document.mime_type:
                    return ".mp4"
                elif 'pdf' in media.document.mime_type:
                    return ".pdf"
            return ".bin"
        return ".bin"
    
    def _get_media_type(self, media):
        """Get media type for Telegram Bot API"""
        if isinstance(media, MessageMediaPhoto):
            return "photo"
        elif isinstance(media, MessageMediaVideo):
            return "video"
        elif isinstance(media, MessageMediaDocument):
            if media.document.mime_type and 'video' in media.document.mime_type:
                return "video"
            return "document"
        return "document"

# Initialize Telethon Manager
telethon_mgr = TelethonManager()

# -----------------------
# TELEGRAM BOT HANDLERS
# -----------------------
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

def owner_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("❌ Unauthorized access!")
            return
        return func(update, context, *args, **kwargs)
    return wrapper

@owner_only
def start_command(update: Update, context: CallbackContext):
    """Handle /start command"""
    welcome_text = """
🚀 **High Speed Media Download Bot**

**Features:**
• 📥 High speed downloads
• 📤 Fast upload to Telegram  
• 🗑️ Auto cleanup after upload
• 💾 Support files up to 1GB
• 📱 Phone login in bot

**Commands:**
/login - Login with phone number
/status - Check bot status
/help - Show this message

**Ready to download media!**
    """
    update.message.reply_text(welcome_text, parse_mode='Markdown')

@owner_only
def status_command(update: Update, context: CallbackContext):
    """Handle /status command"""
    status_text = f"""
🤖 **Bot Status**

• 🔗 Telethon Connected: `{telethon_mgr.is_connected}`
• 💾 Max File Size: `1 GB`
• 🗑️ Auto Cleanup: `Enabled`
• 🚀 Uptime: `Active`

**Ready: {'✅' if telethon_mgr.is_connected else '❌'}**
    """
    update.message.reply_text(status_text, parse_mode='Markdown')

@owner_only
def login_command(update: Update, context: CallbackContext):
    """Start phone login process"""
    if telethon_mgr.is_connected:
        update.message.reply_text("✅ Already logged in!")
        return ConversationHandler.END
        
    update.message.reply_text(
        "📱 Please send your phone number in international format:\n"
        "Example: +1234567890"
    )
    return PHONE

def phone_handler(update: Update, context: CallbackContext):
    """Handle phone number input"""
    phone_number = update.message.text.strip()
    
    async def send_code():
        success, message = await telethon_mgr.connect_with_phone(phone_number)
        if success:
            update.message.reply_text(f"✅ Code sent! Please check Telegram and send the code:")
            return CODE
        else:
            update.message.reply_text(f"❌ Failed: {message}")
            return ConversationHandler.END
    
    # Run async function synchronously in thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        next_state = loop.run_until_complete(send_code())
    finally:
        loop.close()
    
    return next_state

def code_handler(update: Update, context: CallbackContext):
    """Handle authentication code"""
    code = update.message.text.strip()
    
    async def verify_code():
        success, message = await telethon_mgr.verify_code(code)
        if success:
            update.message.reply_text("✅ Successfully logged in! You can now download media.")
        else:
            update.message.reply_text(f"❌ Login failed: {message}")
        return ConversationHandler.END
    
    # Run async function synchronously in thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(verify_code())
    finally:
        loop.close()
    
    return ConversationHandler.END

def cancel_command(update: Update, context: CallbackContext):
    """Cancel conversation"""
    update.message.reply_text("❌ Login cancelled.")
    return ConversationHandler.END

async def upload_large_file(file_path, chat_id, caption, file_type, bot):
    """Upload large files in chunks to avoid Render timeout"""
    try:
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        # For files larger than 50MB, use chunked upload
        if file_size > 50 * 1024 * 1024:
            await bot.send_message(chat_id, f"📦 Uploading large file: {file_size_mb:.2f} MB (this may take a while...)")
        
        with open(file_path, 'rb') as file:
            if file_type == "photo":
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=file,
                    caption=caption,
                    timeout=300
                )
            elif file_type == "video":
                await bot.send_video(
                    chat_id=chat_id,
                    video=file,
                    caption=caption,
                    supports_streaming=True,
                    timeout=300
                )
            else:
                await bot.send_document(
                    chat_id=chat_id,
                    document=file,
                    caption=caption,
                    timeout=300
                )
        
        return True, "Upload successful"
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return False, str(e)

async def process_media_download(chat_identifier, message_id, update: Update):
    """Process media download and upload with progress updates"""
    try:
        if not telethon_mgr.is_connected:
            await update.message.reply_text("❌ Not logged in. Use /login first")
            return
        
        # Send initial status
        status_msg = await update.message.reply_text("⚡ Starting download...")
        
        # Download media
        download_result = await telethon_mgr.download_media(chat_identifier, message_id)
        
        if not download_result["success"]:
            await status_msg.edit_text(f"❌ Download failed: {download_result['error']}")
            return
        
        file_path = download_result["file_path"]
        file_size_mb = download_result["file_size"] / (1024 * 1024)
        media_type = download_result["file_type"]
        caption = download_result["caption"][:1024] if download_result["caption"] else ""
        
        # Update status
        await status_msg.edit_text(f"✅ Downloaded: {file_size_mb:.2f} MB\n⚡ Uploading now...")
        
        # Upload file
        success, upload_message = await upload_large_file(
            file_path, 
            update.effective_chat.id, 
            caption, 
            media_type, 
            update.bot
        )
        
        # Cleanup file
        if os.path.exists(file_path):
            os.unlink(file_path)
            logger.info(f"Cleaned up file: {file_path}")
        
        if success:
            await status_msg.edit_text(f"🎉 Success! Processed {file_size_mb:.2f} MB file")
        else:
            await status_msg.edit_text(f"❌ Upload failed: {upload_message}")
            
    except Exception as e:
        logger.error(f"Processing error: {e}")
        # Cleanup on error
        if 'file_path' in locals() and os.path.exists(file_path):
            os.unlink(file_path)
        await update.message.reply_text(f"❌ Error: {str(e)}")

@owner_only
def handle_message(update: Update, context: CallbackContext):
    """Handle incoming messages with Telegram links"""
    text = update.message.text.strip()
    
    # Extract link using regex
    link_pattern = r'(?:https?://)?t\.me/([^/]+)/(\d+)'
    matches = re.findall(link_pattern, text)
    
    if not matches:
        update.message.reply_text("❌ Please send a valid Telegram message link")
        return
    
    # Process first link found
    chat_identifier, message_id = matches[0]
    message_id = int(message_id)
    
    # Run async processing synchronously in thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_media_download(chat_identifier, message_id, update))
    finally:
        loop.close()

# Register handlers
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('login', login_command)],
    states={
        PHONE: [MessageHandler(Filters.text & ~Filters.command, phone_handler)],
        CODE: [MessageHandler(Filters.text & ~Filters.command, code_handler)],
    },
    fallbacks=[CommandHandler('cancel', cancel_command)]
)

dp.add_handler(conv_handler)
dp.add_handler(CommandHandler("start", start_command))
dp.add_handler(CommandHandler("status", status_command))
dp.add_handler(CommandHandler("help", start_command))
dp.add_handler(CommandHandler("cancel", cancel_command))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

# -----------------------
# STARTUP & BACKGROUND TASKS
# -----------------------
def start_services():
    """Start all services"""
    # Start Telegram Bot polling
    updater.start_polling()
    logger.info("✅ Telegram Bot started polling")
    
    # Initialize Telethon client
    if telethon_mgr.initialize_client():
        logger.info("✅ Telethon client initialized")
    else:
        logger.error("❌ Failed to initialize Telethon client")

def stop_services():
    """Stop all services gracefully"""
    logger.info("🛑 Stopping services...")
    updater.stop()
    if telethon_mgr.client:
        telethon_mgr.client.disconnect()
    logger.info("✅ All services stopped")

# -----------------------
# MAIN EXECUTION
# -----------------------
if __name__ == "__main__":
    try:
        # Start Flask app in background thread for Render
        def run_flask():
            app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # Start bot services
        start_services()
        
        logger.info("🚀 Bot is now fully operational!")
        logger.info("🌐 Flask server running on port 5000 for Uptime Robot")
        
        # Keep the bot running
        updater.idle()
            
    except KeyboardInterrupt:
        logger.info("Received interrupt signal...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        stop_services()
