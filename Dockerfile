# Use a lightweight Python base image
FROM python:3.9-slim-buster

# 1. Install system dependencies (Aria2 & FFmpeg are CRITICAL for speed)
RUN apt-get update && apt-get install -y \
    aria2 \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. Set the working directory
WORKDIR /app

# 3. Copy your project files into the container
COPY . .

# 4. Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# 5. Create the config folder for yt-dlp (Optimization)
RUN mkdir -p /root/.config/yt-dlp

# 6. Inject the High-Speed Config for Aria2 + yt-dlp
#    This forces yt-dlp to use Aria2 with 16 connections per file
RUN echo "--external-downloader aria2c\n--external-downloader-args '-x 16 -k 1M'\n--no-mtime\n--buffer-size 16M" > /root/.config/yt-dlp/config

# 7. Command to run your bot
CMD ["python3", "main.py"]
