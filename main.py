import os
import asyncio
import glob
import shutil
import time
import re
import logging
import sys
import requests  # 🟢 FIXED: Was missing, causing the metadata crash
from aiohttp import web
from pyrogram import Client, filters, idle

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
    # Prevent immediate crash so logs can be read
    API_ID, API_HASH, BOT_TOKEN = 0, "", ""

STICKER_ID = "CAACAgUAAxkBAAEQj6hpV0JDpDDOI68yH7lV879XbIWiFwACGAADQ3PJEs4sW1y9vZX3OAQ"
DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ACTIVE_TASKS = {}
SETTINGS = {"ch1": False, "ch2": False, "ch3": True}

app = Client(
    "anime_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    ipv6=False,
    workers=16
)

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

# --- 3. JIKAN API (METADATA) ---
def get_anime_details(query):
    try:
        url = f"https://api.jikan.moe/v4/anime?q={query}&limit=1"
        response = requests.get(url, timeout=10)
        data = response.json()
        if data['data']:
            anime = data['data'][0]
            image_url = anime['images']['jpg']['large_image_url']
            if anime.get('trailer') and anime['trailer'].get('images'):
                max_img = anime['trailer']['images'].get('maximum_image_url')
                if max_img: image_url = max_img
            duration_raw = anime.get('duration', '24 min').replace(" per ep", "")
            return {
                "title": anime['title'],
                "native": anime.get('title_japanese', ''),
                "duration": duration_raw,
                "url": anime['url'],
                "image": image_url
            }
    except Exception as e:
        LOGGER.warning(f"Metadata Fetch Error: {e}")
    return None

# --- 4. HELPERS ---
def format_time_duration(seconds):
    if seconds < 0: seconds = 0
    if seconds < 60: return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    sec = int(seconds % 60)
    return f"{minutes}m {sec}s"

def parse_episodes(ep_string):
    episodes = []
    try:
        parts = ep_string.split(',')
        for part in parts:
            if '-' in part:
                start, end = map(int, part.split('-'))
                episodes.extend(range(start, end + 1))
            else:
                episodes.append(int(part))
        return sorted(list(set(episodes)))
    except ValueError:
        return []

async def get_video_resolution(filepath):
    try:
        cmd = f"ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=s=x:p=0 '{filepath}'"
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        height = stdout.decode().strip()
        if height.isdigit(): return f"{height}p"
    except: pass
    return "Unknown"

# 🟢 Capture logs for Telegram reporting
async def consume_stream(process, log_buffer):
    while True:
        line = await process.stdout.readline()
        if not line: break
        
        decoded_line = line.decode().strip()
        print(f"[SCRIPT] {decoded_line}", flush=True) # Real-time server logs
        log_buffer.append(decoded_line)

# --- 5. COMMANDS ---
@app.on_message(filters.command(["ch1", "ch2", "ch3"]))
async def toggle_channel(client, message):
    cmd = message.command[0]
    try: state = message.command[1].lower()
    except: return await message.reply(f"⚠️ Usage: `/{cmd} on` or `/{cmd} off`")

    if cmd in SETTINGS:
        SETTINGS[cmd] = (state == "on")
        await message.reply(f"✅ **{cmd.upper()} is now {'ENABLED' if state == 'on' else 'DISABLED'}.**")
    else:
        await message.reply("⚠️ Invalid channel.")

@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    await message.reply("`/dl -a \"Name\" -e 1 -r 1080`\n`/cancel` - Stop Task")

# --- 6. MAIN LOGIC ---
@app.on_message(filters.command("dl"))
async def dl_cmd(client, message):
    chat_id = message.chat.id
    if chat_id in ACTIVE_TASKS: return await message.reply("⚠️ Busy.")

    cmd_text = message.text[4:]
    if not cmd_text: return await help_cmd(client, message)

    USE_POST = "-post" in cmd_text
    USE_STICKER = "-sticker" in cmd_text
    cmd_text = cmd_text.replace("-post", "").replace("-sticker", "").strip()

    try:
        ep_match = re.search(r'-e\s+([\d,-]+)', cmd_text)
        if not ep_match: return await message.reply("❌ Missing `-e`")
        episode_list = parse_episodes(ep_match.group(1))

        resolutions_list = ["1080", "720", "360"]
        if "-r" in cmd_text:
            if "all" in cmd_text:
                resolutions = ["1080", "720", "360"]
            else:
                res_match = re.search(r'-r\s+(\d+)', cmd_text)
                res = res_match.group(1) if res_match else "best"
                resolutions = [res]
                resolutions_list = [res]
            cmd_text = re.sub(r'-r\s+(\d+|all)', '', cmd_text)
        else:
            resolutions = ["1080", "720", "360"]

        audio_lang = "jpn"
        if "-o eng" in cmd_text: audio_lang = "eng"
        cmd_text = cmd_text.replace("-o eng", "").replace("-o jpn", "")

        name_match = re.search(r'-a\s+["\']([^"\']+)["\']', cmd_text)
        anime_query = name_match.group(1) if name_match else "anime"
        base_args = re.sub(r'-e\s+[\d,-]+', '', cmd_text).strip()

    except Exception as e: return await message.reply(f"❌ Error: {e}")

    status_msg = await message.reply("⏳ **Starting...**")

    # Fetch Metadata
    anime_info = get_anime_details(anime_query)
    if not anime_info:
        anime_info = {"title": anime_query.title(), "native": "", "duration": "24 min", "url": "", "image": None}

    display_title = anime_info['title']
    if audio_lang == "eng":
        display_title = f"{display_title} [English Dub]"

    caption_template = f"{display_title} | {anime_info['native']}\nQuality: {', '.join([f'{r}p' for r in resolutions_list])}"

    ACTIVE_TASKS[chat_id] = {"status": "running"}
    log_buffer = []

    try:
        for i, ep_num in enumerate(episode_list):
            if chat_id not in ACTIVE_TASKS: break

            if USE_POST and i == 0:
                 for ch_key, ch_id in [("ch1", CHANNEL_1), ("ch2", CHANNEL_2), ("ch3", CHANNEL_3)]:
                    if ch_id and SETTINGS[ch_key]:
                        try:
                            if anime_info['image']: await client.send_photo(ch_id, photo=anime_info['image'], caption=caption_template)
                            else: await client.send_message(ch_id, caption_template)
                        except: pass

            for current_res in resolutions:
                if chat_id not in ACTIVE_TASKS: break

                res_flag = f"-r {current_res}" if current_res != "best" else ""
                audio_flag = f"-o {audio_lang}" if audio_lang else ""

                if current_res != resolutions[0]: await asyncio.sleep(2)

                try: await status_msg.edit_text(f"📥 **Downloading Ep {ep_num}...**\nQuality: {current_res}p")
                except: pass

                # Run Bash Script
                current_cmd = f"./animepahe-dl.sh {base_args} -e {ep_num} {res_flag} {audio_flag} 2>&1"
                
                print(f"Running: {current_cmd}", flush=True)

                process = await asyncio.create_subprocess_shell(
                    current_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=os.setsid
                )
                
                ACTIVE_TASKS[chat_id]["proc"] = process
                
                await consume_stream(process, log_buffer)
                await process.wait()

                mp4s = glob.glob("**/*.mp4", recursive=True)

                if not mp4s:
                    # 🔴 FAILURE: Send logs to Telegram
                    error_filename = f"error_log_ep{ep_num}.txt"
                    with open(error_filename, "w", encoding="utf-8") as f:
                        f.write("\n".join(log_buffer[-60:]))
                    
                    await client.send_document(
                        chat_id, 
                        error_filename, 
                        caption=f"❌ **Failed Ep {ep_num}**\nSee logs."
                    )
                    os.remove(error_filename)
                    continue 

                # Success
                file_to_up = max(mp4s, key=os.path.getctime)
                detected_quality = await get_video_resolution(file_to_up)

                try: await status_msg.edit_text(f"🚀 **Uploading Ep {ep_num}...**")
                except: pass

                file_caption = f"{display_title}\n• Episode {ep_num} [{detected_quality}]"
                
                sent = await client.send_document(chat_id, file_to_up, caption=file_caption)
                
                for ch_key, ch_id in [("ch1", CHANNEL_1), ("ch2", CHANNEL_2), ("ch3", CHANNEL_3)]:
                    if ch_id and SETTINGS[ch_key]:
                        try: await client.send_document(ch_id, sent.document.file_id, caption=file_caption)
                        except: pass

                try: os.remove(file_to_up); shutil.rmtree(os.path.dirname(file_to_up))
                except: pass
                
                log_buffer.clear() 

    except Exception as e:
        LOGGER.error(f"Critical Error: {e}")
        await message.reply(f"❌ Critical Error: {e}")
    
    finally:
        if chat_id in ACTIVE_TASKS:
            await status_msg.delete()
            del ACTIVE_TASKS[chat_id]

@app.on_message(filters.command("cancel"))
async def text_cancel(client, message):
    cid = message.chat.id
    if cid in ACTIVE_TASKS:
        if "proc" in ACTIVE_TASKS[cid]:
            try: os.killpg(os.getpgid(ACTIVE_TASKS[cid]["proc"].pid), 15)
            except: pass
        del ACTIVE_TASKS[cid]
        await message.reply("🛑 **Stopped.**")
    else:
        await message.reply("⚠️ No task.")

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
