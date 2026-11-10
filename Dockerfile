# Content processor: the web app server, scrapes (Chromium, Tesseract) and reports in one image.
# See deploy/README.md for memory guidance, the volume layout and the environment variables.
#
#   docker build -t content-processor .
#   docker run -p 8780:8780 -v cp-data:/data -e CP_ALLOWED_HOSTS=localhost,127.0.0.1 content-processor

# ---- 1. the web app (static files) --------------------------------------------------
FROM node:20-bookworm-slim AS webui
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webui/ ./
RUN npm run build

# ---- 2. the server ------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Browsers installed here at build time are found whatever user runs the server.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # No Google Chrome in the image: render with Playwright's bundled Chromium.
    CP_BROWSER_CHANNEL=chromium \
    # One document at a time keeps a scrape's peak within a small container.
    CP_DOC_PARALLEL_TASKS=1 \
    # Listen for the platform; the host names it may be reached by are set at deploy time.
    CP_BIND_HOST=0.0.0.0 \
    # Everything that must survive a redeploy goes under this folder (mount a volume).
    CP_DATA_ROOT=/data

# Tesseract (OCR of images and scanned PDFs, with script detection) and fonts for the
# browser so rendered text and screenshots are legible in Latin, CJK and Indic scripts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tesseract-ocr \
        tesseract-ocr-osd \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python packages first, so this layer is reused while only the source changes. The
# project's own package is built from pyproject.toml (hatchling, `packages = ["src"]`);
# a stub `src` satisfies the build until the real sources are copied below.
# Extras: browser (Playwright), docs (PDF, OCR, images), unblock (curl_cffi for T2),
# office (Word, Excel, PowerPoint readers and the .xlsx reports). Add `fallback` for the
# CPU embedding model (about 220 MB downloaded at first use) and `claude`/`mcp` as needed.
ARG EXTRAS="browser,docs,unblock,office"
COPY pyproject.toml README.md ./
RUN mkdir -p src && touch src/__init__.py \
    && pip install ".[${EXTRAS}]" \
    && rm -rf src

# Playwright's Chromium and its system libraries (the version pinned by the installed
# playwright package), into PLAYWRIGHT_BROWSERS_PATH.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# The sources, installed in place so `src` resolves to /app/src (REPO_ROOT = /app, where
# the server's helper processes and default folders are rooted).
COPY . .
RUN pip install --no-deps -e . \
    && chmod +x deploy/start.sh

COPY --from=webui /build/webui/dist ./webui/dist

# Chromium runs with its sandbox off (Playwright's default), so the image works as root
# and on platforms that do not allow user namespaces. Data lives on the volume at /data.
VOLUME ["/data"]
EXPOSE 8780

# Railway (and most platforms) set PORT; the start script also creates the data folders.
CMD ["deploy/start.sh"]
