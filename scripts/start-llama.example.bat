@echo off
setlocal

rem Copy this file, then set both paths for your installation.
set "LLAMA_DIR=C:\path\to\llama.cpp"
set "MODEL_FILE=C:\path\to\model.gguf"

cd /d "%LLAMA_DIR%"
llama-server.exe ^
  -m "%MODEL_FILE%" ^
  -c 20480 ^
  -ngl 99 ^
  -t 8 ^
  -n 4096 ^
  --host 127.0.0.1 ^
  --port 8082 ^
  --flash-attn on ^
  --cache-type-k q4_0 ^
  --cache-type-v q4_0 ^
  --batch-size 2048 ^
  --ubatch-size 512 ^
  --parallel 1 ^
  --temp 0.2 ^
  --top-p 0.9 ^
  --jinja

endlocal
