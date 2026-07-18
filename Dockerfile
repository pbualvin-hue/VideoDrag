# vidrag — single container: FastAPI + in-process worker + backup scheduler.
# ffmpeg (transcode) and deno (yt-dlp JS runtime) are baked in so no host
# setup is needed beyond Docker itself (UX.md: 唯一終端機操作=首次安裝).
# Python >= 3.12.4 required: the article SSRF guard relies on ipaddress
# range classification fixed in CVE-2024-4032 (3.14 is well past it).
FROM python:3.14-slim

# ffmpeg for audio preprocessing; deno for yt-dlp's YouTube JS extraction;
# curl to fetch deno.
# ffmpeg: audio preprocessing; curl/unzip: fetch deno; rclone: encrypted
# off-device backup replication to the user's NAS (CLAUDE.md rule 16).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates rclone \
    && rm -rf /var/lib/apt/lists/*

# deno (yt-dlp EJS runtime) — pinned install to /usr/local/bin.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/

# All persistent state lives here; mounted as a volume by compose (rule 20).
ENV VIDRAG_DATA_DIR=/data
# Model cache lives in the image layer (NOT under /data) so the runtime
# volume mount can't mask the model baked at build time.
ENV HF_HOME=/opt/hf_cache
# yt-dlp finds deno on PATH; no FFMPEG_PATH needed (ffmpeg is on PATH here).
VOLUME ["/data"]
EXPOSE 8080

# Pre-download the embedding model at build time so first ingest isn't slow
# and works even if the model host is unreachable at runtime.
RUN python -c "from fastembed import TextEmbedding; \
    TextEmbedding(model_name='BAAI/bge-small-zh-v1.5')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
