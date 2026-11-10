#!/bin/sh
# Start the web app server in a container (the Dockerfile's CMD).
#
# Every folder that must survive a redeploy is placed under CP_DATA_ROOT (/data: mount a
# persistent volume there) unless its own variable is already set. PORT comes from the
# platform (Railway sets it); CP_BIND_HOST is 0.0.0.0 in the image so the platform can
# reach the server. CP_ALLOWED_HOSTS must name the public host (see deploy/README.md).
set -eu

: "${CP_DATA_ROOT:=/data}"
export CP_APP_DATA_ROOT="${CP_APP_DATA_ROOT:-$CP_DATA_ROOT/app_data}"
export CP_EVIDENCE_ROOT="${CP_EVIDENCE_ROOT:-$CP_DATA_ROOT/evidence}"
export CP_URL_HISTORY_ROOT="${CP_URL_HISTORY_ROOT:-$CP_DATA_ROOT/url_history}"
export CP_KNOWLEDGE_ROOT="${CP_KNOWLEDGE_ROOT:-$CP_DATA_ROOT/knowledge_store}"
export CP_MODEL_CACHE_DIR="${CP_MODEL_CACHE_DIR:-$CP_DATA_ROOT/model_cache}"
export CP_SPEND_DIR="${CP_SPEND_DIR:-$CP_DATA_ROOT/spend}"
EXPORTS="${CP_EXPORTS_DIR:-$CP_DATA_ROOT/exports}"

mkdir -p "$CP_APP_DATA_ROOT" "$CP_EVIDENCE_ROOT" "$CP_URL_HISTORY_ROOT" \
    "$CP_KNOWLEDGE_ROOT" "$CP_MODEL_CACHE_DIR" "$CP_SPEND_DIR" "$EXPORTS"

exec python -m src.modules.content_processor.webapi \
    --exports "$EXPORTS" \
    --host "${CP_BIND_HOST:-0.0.0.0}" \
    --port "${PORT:-${CP_BIND_PORT:-8780}}" \
    --static webui/dist
