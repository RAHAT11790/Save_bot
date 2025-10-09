#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fast Telegram Media Bridge
- Telethon user login by phone
- 1GB max file limit (no download/upload above)
- Fast chunked download (10MB chunks)
- Async upload via aiohttp, preserves media type & caption & thumbnail
- Flask endpoints for uptime/health
"""

import os
import time
import asyncio
import logging
import tempfile
import threading
from functools import wraps

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeFilename, MessageMediaPhoto

import aiohttp

# ---------- Configuration from env ----------
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "") or ""
BOT_TOKEN = os.environ.get("BOT_TOKEN", "") or ""
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
SESSION_NAME = os.environ.get("SESSION_NAME", "user")
PORT = int(os.environ.get("PORT", "5000") or 5000)

# Limits and tuning
MAX_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB
CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per chunk for download
PROGRESS_LOG_INTERVAL = 2.0  # seconds

# Basic validation
if not all([API_ID, API_HASH, BOT_TOKEN, OWNER_ID]):
    raise RuntimeError("Missing required env vars: API_ID, API_HASH, BOT_TOKEN, OWNER_ID")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fastbridge")

# Flask app for uptime
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"status": "active", "note": "Fast Telegram Media Bridge"})

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

# ---------- Telethon helper (async) ----------
class TeleHelper:
    def __init__(self, api_id, api_hash, session_name):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name

        # Create a dedicated loop / client running in a background thread
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        self.thread.start()
        # wait a bit for loop to start
        while not self.loop.is_running():
            time.sleep(0.01)

        # Telethon client will be created inside this loop
        self.client = None
        self._client_lock = threading.Lock()

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coro(self, coro):
        """Run coroutine in helper loop and return result (blocking)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result()

    async def _ensure_client(self):
        if self.client is None:
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash, loop=self.loop)
            await self.client.connect()
        return self.client

    def is_user_authorized(self):
        return self.run_coro(self._is_user_authorized())

    async def _is_user_authorized(self):
        client = await self._ensure_client()
        return await client.is_user_authorized()

    def send_code_request(self, phone):
        return self.run_coro(self._send_code_request(phone))

    async def _send_code_request(self, phone):
        client = await self._ensure_client()
        return await client.send_code_request(phone)

    def sign_in_with_code(self, phone, code, phone_code_hash=None):
        return self.run_coro(self._sign_in_with_code(phone, code, phone_code_hash))

    async def _sign_in_with_code(self, phone, code, phone_code_hash):
        client = await self._ensure_client()
        try:
            if phone_code_hash:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            else:
                await client.sign_in(phone=phone, code=code)
            return True, None
        except SessionPasswordNeededError:
            return False, "password_needed"
        except Exception as e:
            return False, str(e)

    def sign_in_with_password(self, password):
        return self.run_coro(self._sign_in_with_password(password))

    async def _sign_in_with_password(self, password):
        client = await self._ensure_client()
        return await client.sign_in(password=password)

    # ------------ Fast download ------------
    def fetch_message_and_download(self, from_chat, msg_id):
        """Fetch a message and download its media (if any).
           Returns dict with ok, error, file_path, media_type, caption, file_size, avg_speed, download_time, thumb_path (optional)
        """
        return self.run_coro(self._fetch_message_and_download(from_chat, msg_id))

    async def _fetch_message_and_download(self, from_chat, msg_id):
        client = await self._ensure_client()
        try:
            logger.info("Fetching message %s from %s", msg_id, from_chat)
            msg = await client.get_messages(from_chat, ids=msg_id)
            if not msg:
                return {"ok": False, "error": "Message not found"}

            if not msg.media:
                return {"ok": True, "has_media": False, "text": msg.text or ""}

            # Determine media type & file name best-effort
            media_type = "document"
            file_name = "file.bin"

            # try to infer from msg
            if msg.photo:
                media_type = "photo"
                file_name = "photo.jpg"
            elif getattr(msg, "video", False) or (getattr(msg.media, "document", None) and getattr(msg.media.document, "mime_type", "").startswith("video")):
                media_type = "video"
                file_name = "video.mp4"
            elif getattr(msg.media, "document", None):
                doc = msg.media.document
                if hasattr(doc, "attributes"):
                    for a in doc.attributes:
                        if isinstance(a, DocumentAttributeFilename):
                            file_name = a.file_name or file_name

            # temp dest
            ext = ".jpg" if media_type == "photo" else ".mp4" if media_type == "video" else os.path.splitext(file_name)[1] or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
                dest_path = tf.name

            # progress variables
            start_time = time.time()
            last_log = start_time

            # custom progress callback
            def progress_callback(downloaded, total):
                nonlocal last_log, start_time
                now = time.time()
                if total:
                    pct = downloaded / total * 100
                else:
                    pct = 0
                elapsed = now - start_time
                speed = downloaded / elapsed if elapsed > 0 else 0
                if now - last_log >= PROGRESS_LOG_INTERVAL:
                    logger.info("Downloading: %.1f%% | %s/s | %s elapsed", pct, self._format_speed(speed), self._format_time(elapsed))
                    last_log = now

            # Use telethon download_media with bigger chunk_size for speed
            path = await client.download_media(msg, file=dest_path, progress_callback=progress_callback, chunk_size=CHUNK_SIZE)

            if not path or not os.path.exists(path):
                return {"ok": False, "error": "Download failed"}

            file_size = os.path.getsize(path)
            download_time = time.time() - start_time
            avg_speed = file_size / download_time if download_time > 0 else 0

            logger.info("Download complete: %s (%s) in %s | avg %s/s",
                        path, self._format_size(file_size), self._format_time(download_time), self._format_speed(avg_speed))

            # enforce 1GB limit
            if file_size > MAX_FILE_SIZE:
                try:
                    os.unlink(path)
                except Exception:
                    pass
                return {"ok": False, "error": f"File too large ({self._format_size(file_size)} > 1 GB limit)"}

            # Try to find & download thumbnail if exists (for videos/documents)
            thumb_path = None
            try:
                # if message has document and document.thumbs exists
                doc = getattr(msg.media, "document", None)
                if doc and getattr(doc, "thumb", None):
                    # Telethon may expose thumbs via msg.media.document.thumbs
                    # We'll attempt to download a thumb object if present
                    # Try the last thumb if list-like
                    thumbs = getattr(doc, "thumb", None)
                    # Sometimes thumbs is a list, sometimes single object handled differently; try generic approach
                # alternative: try client.download_media on msg.media.thumbnail if present
                if getattr(msg, "media", None):
                    # Try common thumbnail sources
                    possible_thumb = None
                    if hasattr(msg.media, "photo"):
                        possible_thumb = msg.media.photo
                    elif hasattr(msg.media, "document") and getattr(msg.media.document, "thumb", None):
                        possible_thumb = msg.media.document.thumb
                    # if we got something to download
                    if possible_thumb:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf2:
                            thumb_path = tf2.name
                        try:
                            await client.download_media(possible_thumb, file=thumb_path)
                            if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
                                try:
                                    os.unlink(thumb_path)
                                except:
                                    pass
                                thumb_path = None
                        except Exception:
                            # ignore thumbnail failures
                            try:
                                os.unlink(thumb_path)
                            except:
                                pass
                            thumb_path = None
            except Exception:
                thumb_path = None

            return {
                "ok": True,
                "has_media": True,
                "file_path": path,
                "file_size": file_size,
                "file_name": file_name,
                "media_type": media_type,
                "download_time": download_time,
                "avg_speed": avg_speed,
                "caption": msg.text or "",
                "thumb_path": thumb_path
            }

        except Exception as e:
            logger.exception("Download failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ------------ Async upload via Bot API (aiohttp) ------------
    def upload_to_bot(self, file_path, caption, media_type, file_name, thumb_path=None):
        """Blocking wrapper that runs async upload in helper loop"""
        return self.run_coro(self._async_upload_to_bot(file_path, caption, media_type, file_name, thumb_path))

    async def _async_upload_to_bot(self, file_path, caption, media_type, file_name, thumb_path=None):
        url_photo = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        url_video = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
        url_doc = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

        file_size = os.path.getsize(file_path)
        # Again enforce 1GB at upload time
        if file_size > MAX_FILE_SIZE:
            try:
                os.unlink(file_path)
            except:
                pass
            return {"ok": False, "error": "File too large (over 1 GB limit)"}

        data = {"chat_id": OWNER_ID}
        if caption:
            data["caption"] = caption[:1024]

        # Prepare multipart form
        form = aiohttp.FormData()
        for k, v in data.items():
            form.add_field(k, str(v))

        # Which endpoint & key?
        if media_type == "photo":
            endpoint = url_photo
            file_field = "photo"
        elif media_type == "video":
            endpoint = url_video
            file_field = "video"
            # set supports_streaming optional
            form.add_field("supports_streaming", "true")
        else:
            endpoint = url_doc
            file_field = "document"

        # Attach main file
        form.add_field(file_field,
                       open(file_path, "rb"),
                       filename=file_name,
                       content_type="application/octet-stream")

        # Attach thumb if provided and API supports it (for video/document)
        if thumb_path and os.path.exists(thumb_path):
            try:
                # Bot API expects thumbnail field named 'thumb'
                form.add_field("thumb",
                               open(thumb_path, "rb"),
                               filename=os.path.basename(thumb_path),
                               content_type="image/jpeg")
            except Exception:
                logger.warning("Failed to attach thumb; continuing without it")

        start = time.time()
        logger.info("Starting async upload to bot: %s -> %s", file_path, endpoint)

        # aiohttp session
        timeout = aiohttp.ClientTimeout(total=None)  # no total timeout
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(endpoint, data=form) as resp:
                    text = await resp.text()
                    status = resp.status
                    total_time = time.time() - start
                    avg_speed = file_size / total_time if total_time > 0 else 0
                    logger.info("Upload done: status=%s time=%s avg=%s", status, self._format_time(total_time), self._format_speed(avg_speed))
                    if status == 200:
                        return {"ok": True, "upload_time": total_time, "avg_speed": avg_speed}
                    else:
                        # try json
                        try:
                            j = await resp.json()
                            err = j.get("description", text)
                        except Exception:
                            err = text
                        return {"ok": False, "error": f"HTTP {status}: {err}"}
            except Exception as e:
                logger.exception("Upload exception: %s", e)
                return {"ok": False, "error": str(e)}
            finally:
                # close any opened file handles in form (best-effort)
                try:
                    for part in form._fields:
                        if hasattr(part[2], "close"):
                            try:
                                part[2].close()
                            except:
                                pass
                except Exception:
                    pass

    # ------------ Utilities ------------
    def _format_speed(self, bps):
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        if bps >= 1024:
            return f"{bps / 1024:.1f} KB/s"
        return f"{bps:.1f} B/s"

    def _format_size(self, s):
        if s >= 1024 * 1024 * 1024:
            return f"{s / (1024 * 1024 * 1024):.2f} GB"
        if s >= 1024 * 1024:
            return f"{s / (1024 * 1024):.1f} MB"
        if s >= 1024:
            return f"{s / 1024:.1f} KB"
        return f"{s} B"

    def _format_time(self, seconds):
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds//60)}m {int(seconds%60)}s"
        return f"{int(seconds//3600)}h {int((seconds%3600)//60)}m"

# Create TeleHelper instance
tele = TeleHelper(API_ID, API_HASH, SESSION_NAME)

# ---------- Simple admin text interface via python-telegram-bot style (optional) ----------
# For simplicity we'll use Telethon to receive commands via the same user session.
# We'll allow the OWNER_ID to send t.me links to the user account (direct message to the user account),
# and the bot will download & re-upload to OWNER_ID via the Bot token.
#
# Note: If you prefer to use a separate bot for commands, you can wire python-telegram-bot
# and keep tele helper for download/upload operations.

async def _start_listening():
    client = await tele._ensure_client()

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        # Only accept commands from OWNER_ID (safety)
        if sender_id != OWNER_ID:
            return

        text = (event.raw_text or "").strip()
        # Accept t.me links like https://t.me/username/123 or https://t.me/c/CHID/123
        import re
        m = re.search(r"https?://t\.me/((?:c/)?([\dA-Za-z_]+)/(\d+))", text)
        if not m:
            await event.reply("সঠিক t.me লিংক পাঠান (উদাহরণ: https://t.me/username/123)")
            return
        full_path = m.group(1)
        chat_part = m.group(2)
        msg_id = int(m.group(3))

        # Determine chat id
        if full_path.startswith("c/"):
            from_chat = int("-100" + chat_part)
        else:
            from_chat = chat_part if chat_part.startswith("@") else f"@{chat_part}"

        # Inform start
        await event.reply("⚡ ডাউনলোড শুরু করা হচ্ছে...")

        # fetch & download
        res = await asyncio.get_event_loop().run_in_executor(None, tele.fetch_message_and_download, from_chat, msg_id)
        if not res.get("ok"):
            await event.reply(f"❌ ডাউনলোড ব্যর্থ: {res.get('error')}")
            return
        if not res.get("has_media"):
            await event.reply(f"📝 No media: {res.get('text') or 'Empty'}")
            return

        file_path = res["file_path"]
        media_type = res["media_type"]
        caption = res.get("caption", "")
        file_size = res.get("file_size", 0)
        thumb_path = res.get("thumb_path", None)

        await event.reply(f"✅ ডাউনলোড সম্পন্ন: {tele._format_size(file_size)} — আপলোড শুরু করা হচ্ছে...")

        # upload
        upload_res = await asyncio.get_event_loop().run_in_executor(None, tele.upload_to_bot, file_path, caption, media_type, os.path.basename(file_path), thumb_path)

        if upload_res.get("ok"):
            await event.reply(f"🎉 আপলোড সম্পন্ন! Size: {tele._format_size(file_size)} Time: {tele._format_time(upload_res.get('upload_time',0))}")
        else:
            await event.reply(f"❌ আপলোড ব্যর্থ: {upload_res.get('error')}")

        # cleanup files
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
            if thumb_path and os.path.exists(thumb_path):
                os.unlink(thumb_path)
        except Exception:
            pass

    logger.info("Telethon message listener ready (listening for OWNER_ID commands).")

# start the telethon listener in telehelper loop
tele.run_coro(_start_listening())

# ---------- Run Flask + keep process alive ----------
def run_flask():
    # use threaded=True so it doesn't block other threads
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

if __name__ == "__main__":
    logger.info("Starting Fast Telegram Media Bridge...")
    # Flask in background thread (so Telethon loop keeps running)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
