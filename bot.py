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

# Global storage
user_caption_template = ""
video_queue = deque()
processing = False
processor_started = False

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
    
    # Improved episode patterns with better Unicode and symbol handling
    episode_patterns = [
        r'Episode\s*[:=-]+\s*(\d+)',           # "Episode :- 11" (with colon and dash)
        r'Episode\s*[:=-]\s*(\d+)',            # "Episode :- 11" 
        r'Episode\s*[∶＝—–-]\s*(\d+)',         # Various dash types
        r'Episode\s*:\s*(\d+)',                # "Episode : 11"
        r'Episode\s*-\s*(\d+)',                # "Episode - 11"
        r'Epi\s*[:=-]+\s*(\d+)',               # "Epi :- 11"
        r'EP\s*[:=-]+\s*(\d+)',                # "EP :- 11"
        r'E\s*(\d+)',                          # "E 11"
        r'E(\d+)',                             # "E11"
        r'Episode\s*(\d+)',                    # "Episode 11"
        r'⊙ Episode\s*:\s*(\d+)',              # Other formats
        r'›› 𝖤𝗉𝗂𝗌𝗈𝖽𝖾\s*:\s*(\d+)',
        r'✅\s*Episode\s*[:=-]+\s*(\d+)',      # "✅ Episode :- 11"
        r'▶\s*Episode\s*[:=-]+\s*(\d+)',      # "▶ Episode :- 11"
        # Fallback patterns
        r'[Ee]pisode.*?(\d+)',                 # Any text with "episode" followed by number
        r'[Ee]pi.*?(\d+)',                     # Any text with "epi" followed by number
    ]
    
    for pattern in episode_patterns:
        match = re.search(pattern, text_to_scan, re.IGNORECASE)
        if match:
            try:
                episode = int(match.group(1))
                logger.info(f"Episode found with pattern '{pattern}': {episode}")
                break
            except ValueError:
                continue
    
    # If still no episode found, try more aggressive search
    if episode is None:
        # Look for any number that might be episode number (usually 1-3 digits)
        number_matches = re.findall(r'\b(\d{1,3})\b', text_to_scan)
        for num in number_matches:
            num_int = int(num)
            # Assume episode numbers are usually between 1 and 999
            if 1 <= num_int <= 999:
                # Check if this number appears near "episode" text
                episode_context = re.search(r'[Ee]pisode[^\\d]*' + num, text_to_scan, re.IGNORECASE)
                if episode_context:
                    episode = num_int
                    logger.info(f"Episode found with context search: {episode}")
                    break
    
    # More flexible quality patterns
    quality_patterns = [
        r'Quality\s*[:=-]+\s*([^\n\r]+)',      # "Quality :- 720p"
        r'🟡 Quality\s*[:=-]+\s*([^\n\r]+)',   # "🟡 Quality :- 720p"
        r'Quality\s*[:=-]\s*([^\n\r]+)',       # "Quality :- 720p"
        r'⌬ Quality:\s*([^\n\r]+)',            # Other format
        r'›› 𝖰𝗎𝖺𝗅𝗂𝗍𝗒\s*:\s*([^\n\r]+)',       # Other format
        r'(\d+p)\s*\[?[^\]\n]*\]?'             # Generic "480p", "720p", "1080p"
    ]
    
    for pattern in quality_patterns:
        match = re.search(pattern, text_to_scan, re.IGNORECASE)
        if match:
            quality_text = match.group(1).strip()
            # Extract just the resolution (480p, 720p, 1080p)
            quality_match = re.search(r'(480p|720p|1080p)', quality_text, re.IGNORECASE)
            if quality_match:
                quality = quality_match.group(1).lower()
                logger.info(f"Quality found with pattern '{pattern}': {quality}")
                break
            else:
                # If no resolution found, use the full text but clean it
                quality = re.sub(r'[^\w\s]', '', quality_text).strip()
                logger.info(f"Quality found (raw) with pattern '{pattern}': {quality}")
                break
    
    # If still no quality found, try more generic patterns
    if not quality:
        quality_match = re.search(r'(\d+p)', text_to_scan, re.IGNORECASE)
        if quality_match:
            quality = quality_match.group(1).lower()
            logger.info(f"Quality found with generic pattern: {quality}")
    
    # Debug: Check what's being extracted
    logger.info(f"Final extraction - Episode: {episode}, Quality: {quality}")
    
    return episode, quality

def fix_template_formatting(template):
    """Fix template formatting to preserve line breaks and emojis"""
    template = template.replace("━━━━━━━━━━━━━━━━━━━━━━━ ››", "━━━━━━━━━━━━━━━━━━━━━━━\n››")
    template = template.replace("𝖧𝗂𝗇𝖽𝗂 ━━━━━━━━━━━━━━━━━━━━━━━", "𝖧𝗂𝗇𝖽𝗂\n━━━━━━━━━━━━━━━━━━━━━━━")
    template = template.replace("➥ ᴊᴏɪɴ", "\n➥ ᴊᴏɪɴ")
    lines = template.split('\n')
    formatted_lines = [line.rstrip() for line in lines]  # preserve leading spaces
    return '\n'.join(formatted_lines)

def generate_final_caption(episode, quality):
    """Generate final caption with perfect formatting"""
    if not user_caption_template:
        return None
    fixed_template = fix_template_formatting(user_caption_template)
    final_caption = fixed_template.replace("{episode}", str(episode)).replace("{quality}", quality)
    return final_caption

async def process_videos_serial():
    """Process videos in exact serial order"""
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
                await message.reply_text("❌ **Please set caption template first using /set_caption**")
                continue
            
            filename = message.video.file_name if message.video and message.video.file_name else ""
            extracted_episode, extracted_quality = extract_episode_and_quality(message.caption, filename)
            
            if extracted_episode is None or extracted_quality is None:
                await message.reply_text(
                    f"❌ Could not extract episode/quality from:\n\n"
                    f"**Caption:** {message.caption or 'No caption'}\n\n"
                    f"**Filename:** {filename or 'No filename'}\n\n"
                    f"**Extracted:** Episode: {extracted_episode}, Quality: {extracted_quality}"
                )
                continue
            
            final_caption = generate_final_caption(extracted_episode, extracted_quality)
            if final_caption:
                try:
                    await message.copy(chat_id=message.chat.id, caption=final_caption, parse_mode=ParseMode.MARKDOWN)
                    logger.info(f"✅ Posted - Episode: {extracted_episode}, Quality: {extracted_quality}")
                except Exception:
                    await message.copy(chat_id=message.chat.id, caption=final_caption)
                    logger.info(f"✅ Posted (no markdown) - Episode: {extracted_episode}, Quality: {extracted_quality}")
        except Exception as e:
            logger.error(f"Error processing video: {e}")
            await message.reply_text(f"❌ Error processing video: {str(e)}")
        await asyncio.sleep(0.5)

# ----------------- Bot Commands -----------------

@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    global processor_started
    if not processor_started:
        asyncio.create_task(process_videos_serial())
    
    await message.reply_text(
        "🤖 **Advanced Caption Bot Started!**\n\n"
        "**Usage:**\n"
        "1. /set_caption - Set full template\n"
        "2. Send videos\n"
        "3. Get perfectly formatted captions\n\n"
        "**Template must include:** {episode} and {quality}"
    )

@app.on_message(filters.command("set_caption"))
async def set_caption_command(client, message: Message):
    global user_caption_template
    if len(message.command) > 1:
        template_text = " ".join(message.command[1:])
        if "{episode}" not in template_text or "{quality}" not in template_text:
            await message.reply_text("❌ Template must include {episode} and {quality}")
            return
        user_caption_template = fix_template_formatting(template_text)
        test_output = generate_final_caption(1, "480p")
        await message.reply_text(f"✅ Template Set!\nPreview:\n```\n{test_output}\n```")
    else:
        await message.reply_text("Send full template after /set_caption command.")

@app.on_message(filters.text & filters.private & ~filters.command(["start", "set_caption", "status", "clear_queue", "test", "template"]))
async def handle_caption_template(client, message: Message):
    global user_caption_template
    if not message.text.startswith('/'):
        template_text = message.text
        if "{episode}" not in template_text or "{quality}" not in template_text:
            await message.reply_text("❌ Template must include {episode} and {quality}")
            return
        user_caption_template = fix_template_formatting(template_text)
        test_output = generate_final_caption(1, "480p")
        await message.reply_text(f"✅ Template Set!\nPreview:\n```\n{test_output}\n```")

@app.on_message(filters.video)
async def handle_video_message(client, message: Message):
    if not user_caption_template:
        await message.reply_text("❌ Set caption template first using /set_caption")
        return
    video_queue.append(message)
    if not processor_started:
        asyncio.create_task(process_videos_serial())

@app.on_message(filters.command("status"))
async def status_command(client, message: Message):
    queue_size = len(video_queue)
    status_text = f"📊 **Bot Status:**\n• eue size: {queue_size}\n• Template set: {'✅' if user_caption_template else '❌'}\n• Processor: {'✅ Running' if processor_started else '❌ Stopped'}"
    if user_caption_template:
        test_output = generate_final_caption(1, "480p")
        status_text += f"\n\n**Template Preview:**\n```\n{test_output}\n```"
    await message.reply_text(status_text)

@app.on_message(filters.command("clear_queue"))
async def clear_queue_command(client, message: Message):
    video_queue.clear()
    await message.reply_text("✅ Queue cleared!")

@app.on_message(filters.command("test"))
async def test_command(client, message: Message):
    if user_caption_template:
        test_output = generate_final_caption(1, "480p")
        await message.reply_text(f"**Template Test:**\n```\n{test_output}\n```")
    else:
        await message.reply_text("❌ No template set.")

@app.on_message(filters.command("template"))
async def template_command(client, message: Message):
    if user_caption_template:
        await message.reply_text(f"**Current Template:**\n```\n{user_caption_template}\n```")
    else:
        await message.reply_text("❌ No template set.")

# ----------------- Main -----------------
if __name__ == "__main__":
    print("🎯 Advanced Caption Bot Started...")
    asyncio.get_event_loop().create_task(process_videos_serial())
    app.run()
