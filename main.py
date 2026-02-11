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

# --- 3. NETWORK ENGINE (With Cookie Injection) ---
def get_browser_session():
    """
    Creates a session with:
    1. Chrome Impersonation (Cloudflare Bypass)
    2. __ddg2_ Cookie (AnimePahe Bypass)
    """
    session = cffi_requests.Session(impersonate="chrome124")
    
    # 🟢 GENERATE THE MAGIC COOKIE (Same logic as your Bash script)
    ddg2_value = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    
    session.cookies.update({
        "__ddg2_": ddg2_value
    })
    
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://animepahe.si/",
        "Origin": "https://animepahe.si",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest" # Crucial for API calls
    }
    
    LOGGER.info(f"🍪 Injected Cookie __ddg2_: {ddg2_value}")
    return session

def solve_kwik(session, url, referer_url):
    LOGGER.info(f"🔓 Solving Kwik: {url}")
    try:
        headers = session.headers.copy()
        headers["Referer"] = referer_url
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        
        response = session.get(url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            LOGGER.error(f"❌ Kwik HTTP {response.status_code}")
            return None

        # Strategy 1: Standard 'const source'
        match = re.search(r"const\s+source\s*=\s*'([^']+)'", response.text)
        if match: return match.group(1)
            
        # Strategy 2: Fallback (any m3u8)
        match = re.search(r"(https?://[\w\-\.]+/[\w\-\.]+\.m3u8[^\"']*)", response.text)
        if match: return match.group(1)

        LOGGER.warning(f"❌ Failed to extract m3u8.")
        return None

    except Exception as e:
        LOGGER.error(f"Kwik Exception: {e}")
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

    # 🟢 START SESSION & WARM UP
    s = get_browser_session()
    
    try:
        # Visit Home Page first (Sets Cloudflare cookies)
        LOGGER.info("🏠 Visiting Homepage...")
        s.get("https://animepahe.si/", timeout=20)
        await asyncio.sleep(2) # Let cookies settle

        # 2. Search
        search_query = urllib.parse.quote(query)
        search_url = f"https://animepahe.si/api?m=search&q={search_query}"
        LOGGER.info(f"📡 Fetching: {search_url}")
        
        r = s.get(search_url, timeout=30)
        
        # Debugging 403
        if r.status_code == 403:
            await status_msg.edit_text("❌ **Access Denied (403).**\nTrying to rotate cookies...")
            return None
            
        if r.status_code != 200:
            await status_msg.edit_text(f"❌ Search HTTP {r.status_code}")
            return None
            
        data = r.json()
        if not data.get("data"):
            await status_msg.edit_text("❌ Anime not found.")
            return None
            
        anime = data["data"][0]
        session_id = anime["session"]
        title = anime["title"]
        
        await status_msg.edit_text(f"✅ **{title}**\n📥 Finding Ep {episode_num}...")
        await asyncio.sleep(1)
        
        # 3. Get Episode List
        eps_url = f"https://animepahe.si/api?m=release&id={session_id}&sort=episode_asc&page=1"
        LOGGER.info(f"📡 Fetching Eps: {eps_url}")
        
        r = s.get(eps_url, timeout=30)
        eps_data = r.json()

        target_session = None
        for ep in eps_data["data"]:
            if int(ep["episode"]) == episode_num:
                target_session = ep["session"]
                break
        
        if not target_session:
            await status_msg.edit_text(f"❌ Episode {episode_num} not found on Page 1.")
            return None

        # 4. Get Stream Links
        play_url = f"https://animepahe.si/play/{session_id}/{target_session}"
        LOGGER.info(f"📡 Fetching Player: {play_url}")
        
        # Important: Reset headers for HTML navigation (not API)
        s.headers.pop("X-Requested-With", None)
        s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        s.headers["Referer"] = "https://animepahe.si/"
        
        html_response = s.get(play_url, timeout=30)
        
        # Extract Kwik Links
        kwik_links = re.findall(r'(https?://kwik\.[a-z]+/e/\w+)', html_response.text)
        
        if not kwik_links:
            page_title_match = re.search(r'<title>(.*?)</title>', html_response.text)
            page_title = page_title_match.group(1) if page_title_match else "Unknown"
            LOGGER.error(f"❌ No links. Title: {page_title}")
            await status_msg.edit_text(f"❌ No stream links found.\nPage: {page_title}")
            return None
            
        best_link = kwik_links[-1]
        
        # 5. Solve Kwik
        m3u8 = solve_kwik(s, best_link, referer_url=play_url)
        
        if not m3u8:
            await status_msg.edit_text("❌ Failed to bypass Kwik.")
            return None
            
        # 6. Download
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
        LOGGER.error(f"Critical Engine Error: {e}")
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
        await status_msg.edit_text(f"❌ Error: {e}")
    
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
