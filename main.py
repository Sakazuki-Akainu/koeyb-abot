import os
import asyncio
import glob
import shutil
import time
import re
import logging
import sys
import random
import string
import urllib.parse
import subprocess  # 🟢 REQUIRED for running Node.js
from aiohttp import web
from pyrogram import Client, filters, idle
from curl_cffi import requests as cffi_requests

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
LOGGER = logging.getLogger("Bot")

# --- 1. CONFIGURATION ---
try:
    API_ID = int(os.environ.get('API_ID'))
    API_HASH = os.environ.get('API_HASH')
    BOT_TOKEN = os.environ.get('BOT_TOKEN')

    def load_channel(key):
        val = os.environ.get(key)
        if not val: return None
        try: return int(val)
        except ValueError: return val

    CHANNEL_1 = load_channel('CHANNEL_1')
    CHANNEL_2 = load_channel('CHANNEL_2')
    CHANNEL_3 = load_channel('CHANNEL_3')

except Exception as e:
    LOGGER.error(f"❌ Configuration Error: {e}")
    API_ID, API_HASH, BOT_TOKEN = 0, "", ""

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ACTIVE_TASKS = {}
SETTINGS = {"ch1": False, "ch2": False, "ch3": True}

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, ipv6=False, workers=16)

# --- 2. HEALTH CHECK SERVER ---
async def health_check(request):
    return web.Response(text="Bot is Running!", status=200)

async def start_web_server():
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    LOGGER.info("✅ Health Check Server Started on Port 8000")

# --- 3. NETWORK ENGINE ---
def get_browser_session():
    session = cffi_requests.Session(impersonate="chrome124")
    # Generate the Magic Cookie (Just like Bash script)
    ddg2_value = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    session.cookies.update({"__ddg2_": ddg2_value})
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://animepahe.si/",
        "Origin": "https://animepahe.si",
    }
    LOGGER.info(f"🍪 Generated Cookie: {ddg2_value}")
    return session

# 🟢 THE FIX: Replicating the Bash/Node Logic
def solve_kwik_with_node(html_content):
    """
    Extracts the obfuscated JS, transforms it, and runs it with Node.js
    just like the bash script:
    sed -E 's/document/process/g' | sed -E 's/querySelector/exit/g' | sed -E 's/eval\(/console.log\(/g'
    """
    try:
        # 1. Extract the script content: <script>eval(...)
        match = re.search(r"<script>(eval\(.+?\))</script>", html_content, re.DOTALL)
        if not match:
            LOGGER.error("❌ Could not find eval() script in Kwik page.")
            return None
        
        js_code = match.group(1)
        
        # 2. Apply the exact replacements from the bash script
        js_code = js_code.replace("document", "process")
        js_code = js_code.replace("querySelector", "exit")
        js_code = js_code.replace("eval(", "console.log(")
        
        # 3. Run with Node.js
        process = subprocess.run(
            ["node", "-e", js_code],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if process.returncode != 0:
            LOGGER.error(f"❌ Node Error: {process.stderr}")
            return None
            
        unpacked_js = process.stdout
        
        # 4. Extract the link from the unpacked output
        # Bash: grep 'source=' | sed ...
        source_match = re.search(r"source=['\"]([^'\"]+)['\"]", unpacked_js)
        if source_match:
            return source_match.group(1)
            
        # Fallback: Just look for any m3u8
        m3u8_match = re.search(r"(https?://.*?\.m3u8)", unpacked_js)
        if m3u8_match:
            return m3u8_match.group(1)
            
        LOGGER.error("❌ Link not found in Node output.")
        return None

    except Exception as e:
        LOGGER.error(f"❌ Node Execution Failed: {e}")
        return None

async def download_anime_episode(command_args, status_msg):
    name_match = re.search(r'-a\s+["\']([^"\']+)["\']', command_args)
    ep_match = re.search(r'-e\s+(\d+)', command_args)
    
    if not name_match or not ep_match:
        await status_msg.edit_text("❌ Usage: `/dl -a \"Name\" -e 1`")
        return None

    query = name_match.group(1)
    episode_num = int(ep_match.group(1))
    
    await status_msg.edit_text(f"🔍 **Searching:** `{query}`")

    s = get_browser_session()
    
    try:
        # Search
        s.get("https://animepahe.si/", timeout=10) # Warmup
        search_query = urllib.parse.quote(query)
        r = s.get(f"https://animepahe.si/api?m=search&q={search_query}", timeout=30)
        
        if r.status_code != 200:
            await status_msg.edit_text(f"❌ Search Error: {r.status_code}")
            return None
            
        data = r.json()
        if not data.get("data"):
            await status_msg.edit_text("❌ Anime not found.")
            return None
            
        anime = data["data"][0]
        session_id = anime["session"]
        title = anime["title"]
        
        await status_msg.edit_text(f"✅ **{title}**\n📥 Finding Ep {episode_num}...")
        
        # Episodes
        await asyncio.sleep(1)
        r = s.get(f"https://animepahe.si/api?m=release&id={session_id}&sort=episode_asc&page=1", timeout=30)
        eps_data = r.json()

        target_session = None
        for ep in eps_data["data"]:
            if int(ep["episode"]) == episode_num:
                target_session = ep["session"]
                break
        
        if not target_session:
            await status_msg.edit_text(f"❌ Ep {episode_num} not found on Pg 1.")
            return None

        # Get Player HTML
        play_url = f"https://animepahe.si/play/{session_id}/{target_session}"
        s.headers["Referer"] = "https://animepahe.si/"
        html_response = s.get(play_url, timeout=30)
        
        kwik_links = re.findall(r'(https?://kwik\.[a-z]+/e/\w+)', html_response.text)
        
        if not kwik_links:
            await status_msg.edit_text("❌ No stream links.")
            return None
            
        best_link = kwik_links[-1]
        LOGGER.info(f"🔗 Found Kwik Link: {best_link}")
        
        # Get Kwik HTML
        s.headers["Referer"] = play_url
        kwik_html = s.get(best_link, timeout=30).text
        
        # 🟢 CALL THE NODE DECRYPTOR
        m3u8 = solve_kwik_with_node(kwik_html)
        
        if not m3u8:
            await status_msg.edit_text("❌ **Failed to decrypt Kwik.**")
            return None
            
        # Download
        filename = f"{title} - Episode {episode_num}.mp4"
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        
        await status_msg.edit_text(f"⬇️ **Downloading...**\nFile: `{filename}`")
        
        cmd = [
            "yt-dlp",
            "--external-downloader", "aria2c",
            "--external-downloader-args", "-x 16 -k 1M",
            "--referer", "https://kwik.cx/",
            "-o", filepath,
            m3u8
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if os.path.exists(filepath):
            return filepath
        else:
            LOGGER.error(f"DL Error: {stderr.decode()}")
            await status_msg.edit_text("❌ Download Failed (Aria2).")
            return None

    except Exception as e:
        LOGGER.error(f"Engine Error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")
        return None

# --- 4. HANDLERS ---
@app.on_message(filters.command("dl"))
async def dl_handler(client, message):
    chat_id = message.chat.id
    if chat_id in ACTIVE_TASKS: return await message.reply("⚠️ Busy.")
    
    cmd_text = message.text[4:]
    if not cmd_text: return await message.reply("Usage: `/dl -a \"Name\" -e 1`")
    
    ACTIVE_TASKS[chat_id] = True
    status_msg = await message.reply("⏳ **Initializing...**")
    
    try:
        filepath = await download_anime_episode(cmd_text, status_msg)
        
        if filepath:
            await status_msg.edit_text("🚀 **Uploading...**")
            await client.send_document(chat_id, filepath, caption=f"✅ **Done**\n`{os.path.basename(filepath)}`")
            os.remove(filepath)
            await status_msg.delete()
            
    except Exception as e:
        LOGGER.error(e)
    
    finally:
        del ACTIVE_TASKS[chat_id]

async def main():
    print("🤖 Bot Starting...", flush=True)
    await asyncio.gather(start_web_server(), app.start(), idle())
    await app.stop()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
