FROM python:3.9-slim-bookworm

# 1. Install System Dependencies
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

# 2. Install Python Dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# 3. Permissions
RUN chmod +x animepahe-dl.sh

# 4. Inject High-Speed Config
RUN mkdir -p /root/.config/yt-dlp && \
    echo "--external-downloader aria2c\n--external-downloader-args '-x 16 -k 1M'\n--no-mtime\n--buffer-size 16M" > /root/.config/yt-dlp/config

# 5. 🟢 FIX LOGS: Force Python to be unbuffered
ENV PYTHONUNBUFFERED=1

# 6. Expose Port
EXPOSE 8000

CMD ["python3", "main.py"]
