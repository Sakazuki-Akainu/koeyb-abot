import os
import asyncio
import glob
import shutil
import time
import logging
from pyrogram import Client, filters, idle

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Channel IDs (Optional)
CHANNEL_1 = int(os.environ.get("CHANNEL_1", "0")) if os.environ.get("CHANNEL_1") else None
CHANNEL_2 = int(os.environ.get("CHANNEL_2", "0")) if os.environ.get("CHANNEL_2") else None
CHANNEL_3 = int(os.environ.get("CHANNEL_3", "0")) if os.environ.get("CHANNEL_3") else None

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ACTIVE_TASKS = {}

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

@app.on_message(filters.command("dl"))
async def run_scraper(client, message):
    chat_id = message.chat.id
    if chat_id in ACTIVE_TASKS:
        return await message.reply("⚠️ **Task already running!** Wait for it to finish.")

    # 1. Parse Command
    #    User sends: /dl -a "Naruto" -e 1
    #    We strip '/dl ' and pass the rest to the bash script.
    args = message.text[3:].strip()
    if not args:
        return await message.reply("❌ Usage: `/dl -a \"Name\" -e 1`")

    status_msg = await message.reply(f"⏳ **Starting Scraper...**\n`{args}`")
    ACTIVE_TASKS[chat_id] = True

    try:
        # 2. Run the Bash Script
        #    We use stdbuf to prevent buffering so we get real-time logs
        cmd = f"stdbuf -oL ./animepahe-dl.sh {args}"
        
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # 3. Monitor Output (Real-time logs)
        lines_buffer = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            
            decoded_line = line.decode().strip()
            print(f"SCRAPER: {decoded_line}") # Log to server console
            
            # Detect "BATCH_PROGRESS" from your bash script to update Telegram
            if "BATCH_PROGRESS" in decoded_line:
                try:
                    progress = decoded_line.split("=")[1]
                    await status_msg.edit_text(f"📥 **Downloading...**\nProgress: `{progress}`")
                except:
                    pass

        await process.wait()

        # 4. Check for Downloads & Upload
        mp4_files = glob.glob("**/*.mp4", recursive=True)
        if not mp4_files:
            await status_msg.edit_text("❌ **Failed.** No files found.\nCheck if the anime name is correct.")
        else:
            await status_msg.edit_text(f"🚀 **Uploading {len(mp4_files)} files...**")
            
            for file_path in mp4_files:
                # Upload to user
                sent = await client.send_document(
                    chat_id, 
                    document=file_path, 
                    caption=f"✅ `{os.path.basename(file_path)}`"
                )
                
                # Forward to channels
                if CHANNEL_1: await sent.copy(CHANNEL_1)
                
                # Cleanup immediate file to save space
                os.remove(file_path)

            await status_msg.edit_text("✅ **All Done!**")

    except Exception as e:
        await message.reply(f"❌ **Error:** {e}")
    
    finally:
        # Cleanup folder
        if os.path.exists(os.path.join(os.getcwd(), "anime.list")):
            os.remove("anime.list")
        if chat_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[chat_id]

if __name__ == "__main__":
    print("🤖 Scraper Bot Started on Koyeb!")
    app.run()
