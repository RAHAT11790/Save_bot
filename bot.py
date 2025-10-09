#!/usr/bin/env python3
import os, re, tempfile, threading, asyncio, logging, requests
from functools import wraps
from flask import Flask
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SESSION_NAME = os.getenv("SESSION_NAME", "user")
MAX_FILE_SIZE = 1024*1024*1024  # 1 GB

logging.basicConfig(level=logging.INFO)
STATE = {"awaiting": None, "phone": None, "sent_code": None, "logged_in": False}

# Flask
app = Flask(__name__)
@app.route("/")
def index(): return "Bot is running"

# Telethon helper
class TeleHelper:
    def __init__(self): self.loop = asyncio.new_event_loop(); self.client=None; threading.Thread(target=self.start_loop, daemon=True).start()
    def start_loop(self): asyncio.set_event_loop(self.loop); self.loop.run_forever()
    def run(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
    async def init_client(self):
        if not self.client: self.client=TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=self.loop); await self.client.connect(); await self.client.start()
        return self.client
    def send_code(self, phone): return self.run(self._send_code(phone))
    async def _send_code(self, phone): c=await self.init_client(); return await c.send_code_request(phone)
    def sign_in_code(self, phone, code, hash_): return self.run(self._sign_in_code(phone, code, hash_))
    async def _sign_in_code(self, phone, code, hash_): c=await self.init_client(); return await c.sign_in(phone, code=code, phone_code_hash=hash_)
    def is_auth(self): return self.run(self._is_auth())
    async def _is_auth(self): c=await self.init_client(); return await c.is_user_authorized()
    def download_msg(self, chat, msg_id):
        async def _dl(): c=await self.init_client(); m=await c.get_messages(chat, ids=msg_id); 
            if not m or not m.media: return {"ok":False,"error":"No media"}; ext=".jpg" if "Photo" in str(m.media) else ".mp4" if "Video" in str(m.media) else ".bin"
            f=tempfile.NamedTemporaryFile(delete=False, suffix=ext).name; p=await c.download_media(m, file=f)
            if os.path.getsize(p)>MAX_FILE_SIZE: os.unlink(p); return {"ok":False,"error":"File too large"}; return {"ok":True,"file":p,"media":"photo" if ext=='.jpg' else "video" if ext=='.mp4' else "doc"}
        return self.run(_dl())
    def upload(self, path, media, caption):
        data={"chat_id":OWNER_ID,"caption":caption[:1024] if caption else ""}
        files={"photo":open(path,'rb')} if media=="photo" else {"video":open(path,'rb')} if media=="video" else {"document":open(path,'rb')}
        url=f"https://api.telegram.org/bot{BOT_TOKEN}/send{'Photo' if media=='photo' else 'Video' if media=='video' else 'Document'}"
        r=requests.post(url,data=data,files=files)
        for f in files.values(): f.close()
        return r.status_code==200

tele = TeleHelper()

# Telegram Bot
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

def owner_only(f):
    @wraps(f)
    def inner(update, context): 
        if update.effective_user.id!=OWNER_ID: update.message.reply_text("❌ অনুমোদিত নন"); return
        return f(update, context)
    return inner

@owner_only
def start(update, context): update.message.reply_text("🤖 MAX SPEED BOT\nSend t.me link → Fast Download → Fast Upload")

@owner_only
def login(update, context):
    STATE["awaiting"]="phone"; update.message.reply_text("📱 ফোন নম্বর পাঠান (e.g +8801XXXX)")

def handle_text(update, context):
    txt=update.message.text.strip()
    if STATE.get("awaiting")=="phone":
        phone=txt; STATE["phone"]=phone
        try: hash_=tele.send_code(phone); STATE["sent_code"]=hash_; STATE["awaiting"]="code"; update.message.reply_text("✅ কোড পাঠানো হয়েছে!")
        except Exception as e: update.message.reply_text(f"❌ Failed: {e}"); return
        return
    if STATE.get("awaiting")=="code":
        code=txt; phone=STATE.get("phone"); hash_=STATE.get("sent_code")
        try: tele.sign_in_code(phone, code, hash_); STATE["logged_in"]=True; STATE["awaiting"]=None; update.message.reply_text("🎉 লগইন সফল!"); return
        except Exception as e: update.message.reply_text(f"❌ Failed: {e}"); return
    # Process t.me link
    m=re.search(r"https?://t\.me/(c/\d+|[\w_]+)/(\d+)", txt)
    if not m: update.message.reply_text("❌ সঠিক t.me লিংক পাঠান"); return
    chat=m.group(1); msg_id=int(m.group(2))
    update.message.reply_text("⚡ ডাউনলোড শুরু হচ্ছে...")
    res=tele.download_msg(chat,msg_id)
    if not res.get("ok"): update.message.reply_text(f"❌ Download failed: {res.get('error')}"); return
    path, media=res["file"], res["media"]
    update.message.reply_text("✅ DOWNLOAD COMPLETE")
    if tele.upload(path, media, "Uploaded via bot"): update.message.reply_text("🎉 UPLOAD COMPLETE")
    else: update.message.reply_text("❌ Upload failed")
    os.unlink(path)

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("login", login))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

# RUN
if __name__=="__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", PORT))), daemon=True).start()
    updater.start_polling(); updater.idle()
