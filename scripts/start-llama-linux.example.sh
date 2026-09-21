#!/usr/bin/env bash
set -euo pipefail

llama_dir="${LLAMA_DIR:-/opt/llama.cpp}"
model_file="${MODEL_FILE:-/opt/models/model.gguf}"

exec "$llama_dir/llama-server" \
  -m "$model_file" \
  -c 20480 \
  -ngl 99 \
  -t 8 \
  -n 4096 \
  --host 127.0.0.1 \
  --port 8082 \
  --flash-attn on \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --batch-size 2048 \
  --ubatch-size 512 \
  --parallel 1 \
  --temp 0.2 \
  --top-p 0.9 \
  --jinja
