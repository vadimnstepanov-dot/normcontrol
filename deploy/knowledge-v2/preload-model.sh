#!/usr/bin/env bash
# Download the exact, allowlisted embedding revision into a local Linux cache.
set -euo pipefail
cd "$(dirname "$0")/../.."
cache="${1:?Pass an absolute Linux cache path}"
model="${2:-intfloat/multilingual-e5-small}"
case "$cache" in /*) ;; *) echo 'Cache path must be absolute' >&2; exit 2;; esac
mkdir -p "$cache"
docker build -q -t normcontrol/embeddings:preload -f sto_rag/knowledge_v2/Dockerfile sto_rag >/dev/null
docker run --rm --user 0 --entrypoint python -e HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}" \
  -v "$cache:/models" normcontrol/embeddings:preload -c \
  'import sys; from huggingface_hub import snapshot_download; from knowledge_v2.embedding import MODELS; model=sys.argv[1]; assert model in MODELS; root=snapshot_download(model,revision=MODELS[model]["revision"],cache_dir="/models",local_files_only=__import__("os").environ.get("HF_HUB_OFFLINE")=="1",allow_patterns=["*.json","*.safetensors","*.txt","*.model"]); print(root)' "$model"
docker run --rm --user 65532:65532 --entrypoint python -e HF_HUB_OFFLINE=1 \
  -v "$cache:/models:ro" normcontrol/embeddings:preload -c \
  'import sys; from huggingface_hub import snapshot_download; from knowledge_v2.embedding import MODELS,EmbeddingSpace; model=sys.argv[1]; root=snapshot_download(model,revision=MODELS[model]["revision"],cache_dir="/models",local_files_only=True,allow_patterns=["*.json","*.safetensors","*.txt","*.model"]); print(EmbeddingSpace.from_model_files(model,root).id)' "$model"
