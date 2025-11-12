import os
import re
import logging
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from collections import deque

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------- Environment Variables -----------------
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # ✅ Correct variable name here!

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("⚠️ Missing API credentials! Set API_ID, API_HASH, and BOT_TOKEN.")

# ----------------- Initialize Bot -----------------
app = Client("caption_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ----------------- Global Variables -----------------
user_caption_template = ""
video_queue = deque()
processing = False
processor_started = False

# ----------------- Episode & Quality Extractor -----------------
def extract_episode_and_quality(caption_text, filename):
    """Extract episode and quality from caption and filename"""
    episode = None
    quality = None

    text_to_scan = ""
    if caption_text:
        text_to_scan += caption_text + " "
    if filename:
        text_to_scan += filename + " "
    logger.info(f"Scanning text: {text_to_scan}")

    # Improved episode patterns (exactly like your GitHub version)
    episode_patterns = [
        r'Episode\s*[:=-]+\s*(\d+)',
        r'Episode\s*[:=-]\s*(\d+)',
        r'Episode\s*[∶＝—–-]\s*(\d+)',
        r'Episode\s*:\s*(\d+)',
        r'Episode\s*-\s*(\d+)',
        r'Epi\s*[:=-]+\s*(\d+)',
        r'EP\s*[:=-]+\s*(\d+)',
        r'E\s*(\d+)',
        r'E(\d+)',
        r'Episode\s*(\d+)',
        r'⊙ Episode\s*:\s*(\d+)',
        r'›› 𝖤𝗉𝗂𝗌𝗈𝖽𝖾\s*:\s*(\d+)',
        r'✅\s*Episode\s*[:=-]+\s*(\d+)',
        r'▶\s*Episode\s*[:=-]+\s*(\d+)',
        r'[Ee]pisode.*?(\d+)',
        r'[Ee]pi.*?(\d+)',
    ]

    for pattern in episode_patterns:
        match = re.search(pattern, text_to_scan, re.IGNORECASE)
        if match:
            try:
                episode = int(match.group(1))
                logger.info(f"Episode found: {episode}")
                break
            except ValueError:
                continue

    if episode is None:
        number_matches = re.findall(r'\b(\d{1,3})\b', text_to_scan)
        for num in number_matches:
            num_int = int(num)
            if 1 <= num_int <= 999:
                episode_context = re.search(r'[Ee]pisode[^\\d]*' + num, text_to_scan, re.IGNORECASE)
                if episode_context:
                    episode = num_int
                    logger.info(f"Episode found (context): {episode}")
                    break

    # More flexible quality patterns (exactly like yours)
    quality_patterns = [
        r'Quality\s*[:=-]+\s*([^\n\r]+)',
        r'🟡 Quality\s*[:=-]+\s*([^\n\r]+)',
        r'Quality\s*[:=-]\s*([^\n\r]+)',
        r'⌬ Quality:\s*([^\n\r]+)',
        r'›› 𝖰𝗎𝖺𝗅𝗂𝗍𝗒\s*:\s*([^\n\r]+)',
        r'(\d+p)\s*\[?[^\]\n]*\]?',
    ]

    for pattern in quality_patterns:
        match = re.search(pattern, text_to_scan, re.IGNORECASE)
        if match:
            quality_text = match.group(1).strip()
            quality_match = re.search(r'(480p|720p|1080p)', quality_text, re.IGNORECASE)
            if quality_match:
                quality = quality_match.group(1).lower()
                logger.info(f"Quality found: {quality}")
                break
            else:
                quality = re.sub(r'[^\w\s]', '', quality_text).strip()
                logger.info(f"Quality found (raw): {quality}")
                break

    if not quality:
        quality_match = re.search(r'(\d+p)', text_to_scan, re.IGNORECASE)
        if quality_match:
            quality = quality_match.group(1).lower()
            logger.info(f"Quality found (generic): {quality}")

    logger.info(f"✅ Final extraction - Episode: {episode}, Quality: {quality}")
    return episode, quality


# ----------------- Template Formatting -----------------
def fix_template_formatting(template):
    template = template.replace("━━━━━━━━━━━━━━━━━━━━━━━ ››", "━━━━━━━━━━━━━━━━━━━━━━━\n››")
    template = template.replace("𝖧𝗂𝗇𝖽𝗂 ━━━━━━━━━━━━━━━━━━━━━━━", "𝖧𝗂𝗇𝖽𝗂\n━━━━━━━━━━━━━━━━━━━━━━━")
    template = template.replace("➥ ᴊᴏɪɴ", "\n➥ ᴊᴏɪɴ")
    lines = template.split('\n')
    formatted_lines = [line.rstrip() for line in lines]
    return '\n'.join(formatted_lines)


def generate_final_caption(episode, quality):
    if not user_caption_template:
        return None
    fixed_template = fix_template_formatting(user_caption_template)
    return fixed_template.replace("{episode}", str(episode)).replace("{quality}", quality)


# ----------------- Async Video Processor -----------------
async def process_videos_serial():
    global processing, processor_started
    processor_started = True
    logger.info("Processor started")

    while True:
        if not video_queue:
            processing = False
            await asyncio.sleep(0.1)
            continue

        processing = True
        message = video_queue.popleft()

        try:
            if not user_caption_template:
                await message.reply_text("❌ Please set caption template first using /set_caption")
                continue

            filename = message.video.file_name if message.video and message.video.file_name else ""
            extracted_episode, extracted_quality = extract_episode_and_quality(message.caption, filename)

            if extracted_episode is None or extracted_quality is None:
                await message.reply_text(
                    f"❌ Could not extract episode/quality.\n\n"
                    f"**Caption:** {message.caption or 'No caption'}\n"
                    f"**Filename:** {filename or 'No filename'}"
                )
                continue

            final_caption = generate_final_caption(extracted_episode, extracted_quality)
            if final_caption:
                try:
                    await message.copy(chat_id=message.chat.id, caption=final_caption, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    await message.copy(chat_id=message.chat.id, caption=final_caption)
        except Exception as e:
            logger.exception(f"Error processing video: {e}")
            await message.reply_text(f"❌ Error: {e}")

        await asyncio.sleep(0.5)


# ----------------- Commands -----------------
@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    global processor_started
    if not processor_started:
        asyncio.create_task(process_videos_serial())
    await message.reply_text(
        "🤖 **Advanced Caption Bot Started!**\n\n"
        "**Usage:**\n"
        "1️⃣ /set_caption - Set your caption template\n"
        "2️⃣ Send videos\n"
        "3️⃣ Bot will apply captions automatically\n\n"
        "**Template must include:** {episode} and {quality}"
    )


@app.on_message(filters.command("set_caption"))
async def set_caption_command(client, message: Message):
    global user_caption_template
    if len(message.command) > 1:
        text = " ".join(message.command[1:])
        if "{episode}" not in text or "{quality}" not in text:
            await message.reply_text("❌ Template must include {episode} and {quality}")
            return
        user_caption_template = fix_template_formatting(text)
        preview = generate_final_caption(1, "720p")
        await message.reply_text(f"✅ Template Set!\nPreview:\n```\n{preview}\n```")
    else:
        await message.reply_text("Send template after /set_caption command.")


@app.on_message(filters.video)
async def handle_video(client, message: Message):
    if not user_caption_template:
        await message.reply_text("❌ Please set caption template first using /set_caption")
        return
    video_queue.append(message)
    if not processor_started:
        asyncio.create_task(process_videos_serial())


@app.on_message(filters.command("status"))
async def status_command(client, message: Message):
    queue_size = len(video_queue)
    preview = generate_final_caption(1, "480p") if user_caption_template else "❌ No template set"
    await message.reply_text(
        f"📊 **Bot Status:**\n• Queue: {queue_size}\n• Template: {'✅' if user_caption_template else '❌'}\n"
        f"• Processor: {'✅ Running' if processor_started else '❌ Stopped'}\n\n"
        f"**Template Preview:**\n```\n{preview}\n```"
    )


@app.on_message(filters.command("clear_queue"))
async def clear_queue_command(client, message: Message):
    video_queue.clear()
    await message.reply_text("✅ Queue cleared!")


@app.on_message(filters.command("template"))
async def template_command(client, message: Message):
    if user_caption_template:
        await message.reply_text(f"**Current Template:**\n```\n{user_caption_template}\n```")
    else:
        await message.reply_text("❌ No template set.")


# ----------------- Main -----------------
if __name__ == "__main__":
    print("🎯 Advanced Caption Bot Started...")
    loop = asyncio.get_event_loop()
    loop.create_task(process_videos_serial())
    app.run()
