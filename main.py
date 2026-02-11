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

# --- LOGGING SETUP (Fixed for Real-Time) ---
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

# --- 3. ROBUST NETWORK ENGINE ---
def safe_api_get(url, referer=None):
    """
    Safely fetches JSON from an API. 
    If Cloudflare blocks it (HTML response), it logs the error instead of crashing.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": referer if referer else "https://animepahe.si/"
    }
    
    LOGGER.info(f"📡 Fetching: {url}")
    
    try:
        # 🟢 Use newer Chrome impersonation
        response = cffi_requests.get(url, impersonate="chrome124", headers=headers, timeout=30)
        
        # Check HTTP Status
        if response.status_code != 200:
            LOGGER.error(f"❌ HTTP Error {response.status_code} for {url}")
            return None, f"HTTP Error {response.status_code}"

        # Try Parse JSON
        try:
            return response.json(), None
        except Exception:
            # If JSON fails, it's likely a Cloudflare HTML page
            snippet = response.text[:200].replace("\n", " ")
            LOGGER.error(f"❌ JSON Parse Failed. Response start: {snippet}")
            return None, "Cloudflare Blocked Request (Received HTML instead of JSON)"

    except Exception as e:
        LOGGER.error(f"❌ Network Error: {e}")
        return None, str(e)

def solve_kwik(url):
    LOGGER.info(f"🔓 Solving Kwik: {url}")
    try:
        response = cffi_requests.get(
            url, 
            impersonate="chrome124", 
            headers={"Referer": "https://animepahe.si/"},
            timeout=15
        )
        match = re.search(r"const\s+source\s*=\s*'([^']+)'", response.text)
        if match:
            return match.group(1)
        return None
    except Exception as e:
        LOGGER.error(f"Kwik Error: {e}")
        return None

async def download_anime_episode(command_args, status_msg):
    # 1. Parse Args
    name_match = re.search(r'-a\s+["\']([^"\']+)["\']', command_args)
    ep_match = re.search(r'-e\s+(\d+)', command_args)
    
    if not name_match or not ep_match:
        await status_msg.edit_text("❌ Usage: `/dl -a \"Jujutsu Kaisen\" -e 1`")
        return None

    query = name_match.group(1)
    episode_num = int(ep_match.group(1))
    
    await status_msg.edit_text(f"🔍 **Searching:** `{query}`")

    # 2. Search AnimePahe
    search_url = f"https://animepahe.si/api?m=search&q={requests.utils.quote(query)}"
    data, error = safe_api_get(search_url)
    
    if not data:
        await status_msg.edit_text(f"❌ **Search Failed:** {error}")
        return None
        
    if not data.get("data"):
        await status_msg.edit_text("❌ Anime not found.")
        return None
        
    anime = data["data"][0]
    session_id = anime["session"]
    title = anime["title"]
    
    await status_msg.edit_text(f"✅ Found: **{title}**\n📥 Finding Episode {episode_num}...")
    
    # 3. Get Episode List (Page 1)
    eps_url = f"https://animepahe.si/api?m=release&id={session_id}&sort=episode_asc&page=1"
    eps_data, error = safe_api_get(eps_url)
    
    if not eps_data:
        await status_msg.edit_text(f"❌ **Episode List Failed:** {error}")
        return None

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
    
    # Kwik extraction logic
    try:
        html_response = cffi_requests.get(play_url, impersonate="chrome124", headers={"Referer": "https://animepahe.si/"})
        kwik_links = re.findall(r'https://kwik\.cx/e/\w+', html_response.text)
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to load player: {e}")
        return None

    if not kwik_links:
        await status_msg.edit_text("❌ No stream links found.")
        return None
        
    best_link = kwik_links[-1] # Usually 1080p
    
    # 5. Solve Kwik
    m3u8 = solve_kwik(best_link)
    if not m3u8:
        await status_msg.edit_text("❌ Failed to bypass Kwik protection.")
        return None
        
    # 6. Download
    filename = f"{title} - Episode {episode_num}.mp4"
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    
    cmd = [
        "yt-dlp",
        "--external-downloader", "aria2c",
        "--external-downloader-args", "-x 16 -k 1M",
        "--referer", "https://kwik.cx/",
        "-o", filepath,
        m3u8
    ]
    
    await status_msg.edit_text(f"⬇️ **Downloading...**\nFile: `{filename}`")
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()
    
    if os.path.exists(filepath):
        return filepath
    else:
        LOGGER.error(f"Aria2 Error: {stderr.decode()}")
        await status_msg.edit_text("❌ Download Failed (See Logs).")
        return None

# --- 4. BOT HANDLERS ---
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
        await status_msg.edit_text(f"❌ Fatal Error: {e}")
    
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
