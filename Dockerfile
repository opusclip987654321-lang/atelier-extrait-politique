FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.* requests==2.32.* "yt-dlp[default]" pillow numpy "rembg[cpu]"
RUN mkdir -p /usr/share/fonts/anton && curl -fsSL -o /usr/share/fonts/anton/Anton-Regular.ttf https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf
ENV U2NET_HOME=/models
RUN python -c "from rembg import new_session; new_session('u2net_human_seg')"
RUN pip install --no-cache-dir python-multipart
WORKDIR /app
COPY app.py thumb.py tiktok.py /app/
ENV DATA_DIR=/data
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
