import os
import asyncio
import glob
import shutil
import time
import re
import logging
import sys
import requests
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

# --- 3. NETWORK ENGINE (Session-Based) ---
def get_browser_session():
    """Creates a session that behaves like a real Chrome browser."""
    session = cffi_requests.Session(impersonate="chrome124")
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://animepahe.si/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    return session

def solve_kwik(session, url, referer_url):
    LOGGER.info(f"🔓 Solving Kwik: {url}")
    try:
        # Update referer for this specific request
        headers = session.headers.copy()
        headers["Referer"] = referer_url
        
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

        LOGGER.warning(f"❌ Failed to extract m3u8. Page Start: {response.text[:100]}")
        return None

    except Exception as e:
        LOGGER.error(f"Kwik Exception: {e}")
        return None

async def download_anime_episode(command_args, status_msg):
    # 1. Parse Args
    name_match = re.search(r'-a\s+["\']([^"\']+)["\']', command_args)
    ep_match = re.search(r'-e\s+(\d+)', command_args)
    
    if not name_match or not ep_match:
        await status_msg.edit_text("❌ Usage: `/dl -a \"Name\" -e 1`")
        return None

    query = name_match.group(1)
    episode_num = int(ep_match.group(1))
    
    await status_msg.edit_text(f"🔍 **Searching:** `{query}`")

    # 🟢 START SESSION (Critical for Cloudflare)
    s = get_browser_session()

    try:
        # 2. Search
        search_url = f"https://animepahe.si/api?m=search&q={requests.utils.quote(query)}"
        LOGGER.info(f"📡 Fetching: {search_url}")
        
        r = s.get(search_url, timeout=30)
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
        
        # 3. Get Episode List
        # Small delay to mimic human behavior
        await asyncio.sleep(1)
        
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

        # 4. Get Stream Links (The step that was failing)
        play_url = f"https://animepahe.si/play/{session_id}/{target_session}"
        LOGGER.info(f"📡 Fetching Player: {play_url}")
        
        # Update Referer to trick AnimePahe that we came from the home page
        s.headers["Referer"] = "https://animepahe.si/"
        
        html_response = s.get(play_url, timeout=30)
        
        # 🟢 IMPROVED REGEX: Find links in data-src attributes (standard for AnimePahe)
        # Looks for: https://kwik.cx/e/xXxX
        kwik_links = re.findall(r'(https?://kwik\.[a-z]+/e/\w+)', html_response.text)
        
        if not kwik_links:
            # Logging the HTML title to see if we got hit by Cloudflare
            page_title = re.search(r'<title>(.*?)</title>', html_response.text)
            title_text = page_title.group(1) if page_title else "No Title"
            LOGGER.error(f"❌ No links. Page Title: {title_text}")
            await status_msg.edit_text(f"❌ No stream links found.\nPage: {title_text}")
            return None
            
        # Get the last link (usually the best quality/latest added)
        best_link = kwik_links[-1]
        
        # 5. Solve Kwik (Pass session to keep cookies)
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
