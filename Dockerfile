# 🟢 CHANGED: Switched from 'buster' (dead) to 'bookworm' (supported)
FROM python:3.9-slim-bookworm

# 1. Install System Dependencies
#    We install 'npm' as well to ensure the node environment is complete for the scraper
RUN apt-get update && apt-get install -y \
    aria2 \
    ffmpeg \
    git \
    jq \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# 2. Install Python Requirements
RUN pip3 install --no-cache-dir -r requirements.txt

# 3. Permissions
#    Make sure the bash script is executable
RUN chmod +x animepahe-dl.sh

# 4. Inject High-Speed Config for yt-dlp (The "l5-master" logic)
#    This forces the bash script to use Aria2c with 16 connections automatically.
RUN mkdir -p /root/.config/yt-dlp && \
    echo "--external-downloader aria2c\n--external-downloader-args '-x 16 -k 1M'\n--no-mtime\n--buffer-size 16M" > /root/.config/yt-dlp/config

CMD ["python3", "main.py"]
