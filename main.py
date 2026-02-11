import os
import asyncio
import glob
import shutil
import time
import re
import logging
from pyrogram import Client, filters, idle
from yt_dlp import YoutubeDL

# --- LOGGING SETUP (From l5-master) ---
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

class MyLogger:
    """
    Adapted from l5-master to track filename changes during download/merge.
    """
    def __init__(self, obj=None):
        self.obj = obj

    def debug(self, msg):
        # Detect filename changes (e.g. merging video+audio)
        if self.obj and hasattr(self.obj, 'name'):
            if match := re.search(r'.Merger..Merging formats into..(.*?).$', msg) or \
                    re.search(r'.ExtractAudio..Destination..(.*?)$', msg):
                LOGGER.info(f"Filename changed: {msg}")
                newname = match.group(1)
                newname = newname.rsplit("/", 1)[-1]
                self.obj.name = newname

    def warning(self, msg):
        LOGGER.warning(msg)

    def error(self, msg):
        if msg != "ERROR: Cancelling...":
            LOGGER.error(msg)

class DownloadStatus:
    def __init__(self):
        self.name = "Unknown"

# --- CONFIGURATION (Adapted for Koyeb/Env Vars) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Load Channels (Optional)
CHANNEL_1 = int(os.environ.get("CHANNEL_1", "0")) if os.environ.get("CHANNEL_1") else None
CHANNEL_2 = int(os.environ.get("CHANNEL_2", "0")) if os.environ.get("CHANNEL_2") else None
CHANNEL_3 = int(os.environ.get("CHANNEL_3", "0")) if os.environ.get("CHANNEL_3") else None

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ACTIVE_TASKS = {}

app = Client(
    "anime_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    ipv6=False,
    workers=16
)

# --- THE l5-master DOWNLOAD LOGIC ---
def l5_download_engine(link, path, listener_obj):
    """
    The core download logic ported from l5-master.
    Uses yt-dlp with Aria2c injection for max speed.
    """
    ydl_opts = {
        'logger': MyLogger(listener_obj),
        'progress_hooks': [], # Add hooks if you want real-time progress
        'outtmpl': f"{path}/%(title)s.%(ext)s",
        'writethumbnail': True,
        'allow_multiple_video_streams': True,
        'allow_multiple_audio_streams': True,
        'noprogress': False,
        'overwrites': True,
        # --- SPEED BOOST CONFIG (The "Secret Sauce") ---
        'external_downloader': 'aria2c',
        'external_downloader_args': ['-x', '16', '-k', '1M', '-s', '16'],
        'buffer_size': '16M',
        'no_mtime': True,
        # -----------------------------------------------
    }

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(link, download=True)
            # Update name if it changed
            if 'requested_downloads' in info:
                filename = info['requested_downloads'][0]['filepath']
            else:
                filename = ydl.prepare_filename(info)
            return filename
        except Exception as e:
            LOGGER.error(f"Download failed: {e}")
            return None

# --- BOT COMMANDS ---
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply("🚀 **Koyeb Bot Ready!**\nSend a link to download using l5-master logic.")

@app.on_message(filters.command("dl"))
async def download_handler(client, message):
    chat_id = message.chat.id
    if chat_id in ACTIVE_TASKS:
        return await message.reply("⚠️ Task running.")

    # 1. Parse Link
    try:
        link = message.text.split(" ", 1)[1]
    except IndexError:
        return await message.reply("❌ Usage: `/dl <link>`")

    status_msg = await message.reply("⏳ **Initializing High-Speed Engine...**")
    ACTIVE_TASKS[chat_id] = True

    # 2. Start Download
    try:
        status_obj = DownloadStatus()
        
        # Run synchronous download in a separate thread to not block bot
        loop = asyncio.get_event_loop()
        filepath = await loop.run_in_executor(
            None, 
            l5_download_engine, 
            link, 
            DOWNLOAD_DIR, 
            status_obj
        )

        if filepath and os.path.exists(filepath):
            await status_msg.edit_text("🚀 **Uploading...**")
            
            # 3. Upload
            start_time = time.time()
            sent = await client.send_document(
                chat_id, 
                document=filepath, 
                caption=f"✅ **Downloaded via l5-Engine**\n📂 `{os.path.basename(filepath)}`"
            )
            
            # Forward to channels if configured
            if CHANNEL_1: await sent.copy(CHANNEL_1)
            
            # Cleanup
            os.remove(filepath)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Download Failed. Check logs.")

    except Exception as e:
        LOGGER.error(e)
        await message.reply(f"❌ Error: {e}")
    
    finally:
        if chat_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[chat_id]

# --- MAIN LOOP ---
if __name__ == "__main__":
    print("🤖 Bot Started on Koyeb...")
    app.run()
