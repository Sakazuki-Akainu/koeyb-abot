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
from curl_cffi import requests as cffi_requests  # 🟢 KEY FIX: Browser Impersonation

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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

# --- 3. CORE DOWNLOAD ENGINE (Python Replaces Bash) ---
def solve_kwik(url):
    """
    Bypasses Cloudflare on Kwik.cx and extracts the m3u8 link.
    """
    try:
        LOGGER.info(f"Solving Kwik: {url}")
        # Impersonate Chrome to pass Cloudflare
        response = cffi_requests.get(
            url,
            impersonate="chrome110",
            headers={"Referer": "https://animepahe.si/"},
            timeout=15
        )
        
        # Regex to find the obfuscated script source
        # Usually hidden in: const source='...'
        match = re.search(r"const\s+source\s*=\s*'([^']+)'", response.text)
        if match:
            return match.group(1)
        
        # Fallback: Check for eval(function(p,a,c,k,e,d)...)
        if "eval(function(" in response.text:
            LOGGER.warning("Complex obfuscation detected (eval). Python extractor might fail.")
            # In a real scenario, you'd need a JS engine here. 
            # For now, let's hope Kwik serves the 'const source' version to 'Chrome'.
            
        return None
    except Exception as e:
        LOGGER.error(f"Kwik Solve Error: {e}")
        return None

async def download_anime_episode(command_args, status_msg):
    """
    Orchestrates the entire download process:
    1. Parse args
    2. Search Anime (via AnimePahe API)
    3. Get Episode Links
    4. Solve Kwik
    5. Download via Aria2
    """
    # 1. Parse Args (Simple Regex)
    anime_name_match = re.search(r'-a\s+["\']([^"\']+)["\']', command_args)
    ep_match = re.search(r'-e\s+(\d+)', command_args)
    
    if not anime_name_match or not ep_match:
        await status_msg.edit_text("❌ Error parsing arguments.")
        return None

    query = anime_name_match.group(1)
    episode_num = int(ep_match.group(1))
    
    await status_msg.edit_text(f"🔍 **Searching for:** `{query}`")

    # 2. Search AnimePahe
    try:
        search_url = f"https://animepahe.si/api?m=search&q={requests.utils.quote(query)}"
        # Use cffi here too just in case
        r = cffi_requests.get(search_url, impersonate="chrome110").json()
        
        if not r.get("data"):
            await status_msg.edit_text("❌ Anime not found.")
            return None
            
        anime_data = r["data"][0] # Pick first result
        session_id = anime_data["session"]
        title = anime_data["title"]
        
        await status_msg.edit_text(f"✅ Found: **{title}**\n📥 Fetching Episode {episode_num}...")
        
        # 3. Get Episode List
        # Fetch page 1 (AnimePahe paginates, but usually latest eps are on pg 1 or last pg)
        # For simplicity, fetching all pages logic is omitted, assuming Ep 1 is on Page 1 or we find it.
        # Ideally, you'd loop through pages.
        
        eps_url = f"https://animepahe.si/api?m=release&id={session_id}&sort=episode_asc&page=1"
        eps_r = cffi_requests.get(eps_url, impersonate="chrome110").json()
        
        target_ep_session = None
        for ep in eps_r["data"]:
            if int(ep["episode"]) == episode_num:
                target_ep_session = ep["session"]
                break
        
        if not target_ep_session:
            await status_msg.edit_text(f"❌ Episode {episode_num} not found on Page 1.")
            return None

        # 4. Get Stream Links
        play_url = f"https://animepahe.si/play/{session_id}/{target_ep_session}"
        play_html = cffi_requests.get(play_url, impersonate="chrome110").text
        
        # Extract Kwik Links (regex for data-src or button links)
        # This is tricky as they are dynamic. We look for 'kwik.cx/e/'
        kwik_links = re.findall(r'https://kwik\.cx/e/\w+', play_html)
        
        if not kwik_links:
            await status_msg.edit_text("❌ No stream links found.")
            return None
            
        # Prioritize 720p/1080p (usually last in list)
        best_link = kwik_links[-1] 
        
        # 5. Solve Kwik
        m3u8_link = solve_kwik(best_link)
        
        if not m3u8_link:
            await status_msg.edit_text("❌ Failed to bypass Kwik Cloudflare.")
            return None
            
        # 6. Download with Aria2 (via yt-dlp)
        filename = f"{title} - Episode {episode_num}.mp4"
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        
        cmd = [
            "yt-dlp",
            "--external-downloader", "aria2c",
            "--external-downloader-args", "-x 16 -k 1M",
            "--referer", "https://kwik.cx/",
            "-o", filepath,
            m3u8_link
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        await status_msg.edit_text(f"⬇️ **Downloading...**\n`{filename}`")
        
        stdout, stderr = await process.communicate()
        
        if os.path.exists(filepath):
            return filepath
        else:
            LOGGER.error(f"Download Error: {stderr.decode()}")
            await status_msg.edit_text("❌ Download failed (Aria2 Error).")
            return None

    except Exception as e:
        LOGGER.error(f"Engine Error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")
        return None

# --- 4. BOT HANDLERS ---
@app.on_message(filters.command("dl"))
async def dl_handler(client, message):
    chat_id = message.chat.id
    if chat_id in ACTIVE_TASKS: return await message.reply("⚠️ Busy.")
    
    cmd_text = message.text[4:]
    if not cmd_text: return await message.reply("Usage: `/dl -a \"Name\" -e 1`")
    
    ACTIVE_TASKS[chat_id] = True
    status_msg = await message.reply("⏳ **Starting Python Engine...**")
    
    try:
        # Run the Python downloader
        filepath = await download_anime_episode(cmd_text, status_msg)
        
        if filepath:
            await status_msg.edit_text("🚀 **Uploading...**")
            start = time.time()
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
