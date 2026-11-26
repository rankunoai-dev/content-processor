#!/usr/bin/env bash
# Rerun wrapper: the integration branch's code, the main folder's John Hancock data (read-only).
M="C:/Users/RankUno/Desktop/content-processor"
cd "C:/Users/RankUno/Desktop/content-processor-merge3" || exit 1
CP_APP_DATA_ROOT="$M/app_data" CP_KNOWLEDGE_ROOT="$M/knowledge_store" CP_MODEL_CACHE_DIR="$M/model_cache" \
CP_EVIDENCE_ROOT="$M/evidence" PYTHONIOENCODING=utf-8 \
python docs/eval/analyst-100-2026-11-26/tool.py "$@"
