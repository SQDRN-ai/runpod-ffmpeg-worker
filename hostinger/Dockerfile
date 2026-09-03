# Build exactly against the currently validated RunPod handlers. The handler code is
# retained verbatim except for its RunPod serverless bootstrap; app.worker invokes it.
FROM python:3.11-slim AS base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements-api.txt /app/
RUN pip install --no-cache-dir -r requirements-api.txt
COPY app /app/app

FROM base AS render
ARG FFMPEG_WORKER_REF=1f450adf3ff5e54e124c5199e3e1c3c86cd16531
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir boto3==1.34.0 \
    && curl --fail --silent --show-error --location \
      "https://raw.githubusercontent.com/SQDRN-ai/runpod-ffmpeg-worker/${FFMPEG_WORKER_REF}/handler.py" \
      --output /app/render_handler.py \
    && sed -i '/^import runpod$/d; /runpod\.serverless\.start/d' /app/render_handler.py
CMD ["python", "-m", "app.entrypoint", "api"]

FROM base AS voice
ARG VOICE_WORKER_REF=943e77860342877028c62ddd0652c294593f1b9b
ENV TORCH_HOME=/models/torch XDG_CACHE_HOME=/models/cache
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.2.2 \
    && pip install --no-cache-dir boto3 requests demucs==4.0.1 \
    && curl --fail --silent --show-error --location \
      "https://raw.githubusercontent.com/SQDRN-ai/runpod-delete-voice/${VOICE_WORKER_REF}/handler.py" \
      --output /app/voice_handler.py \
    && sed -i '/^import runpod$/d; /runpod\.serverless\.start/d' /app/voice_handler.py
CMD ["python", "-m", "app.entrypoint", "api"]

