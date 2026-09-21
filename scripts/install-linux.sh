#!/usr/bin/env bash
set -euo pipefail

mode="${1:---full}"
if [[ "$mode" != "--full" && "$mode" != "--portal-only" ]]; then
  echo "Usage: $0 [--full|--portal-only]" >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python was not found: $python_bin" >&2
  exit 1
fi

python_version="$($python_bin -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$python_version" != "3.11" && "$python_version" != "3.12" ]]; then
  echo "Python 3.11 or 3.12 is required; found $python_version" >&2
  exit 1
fi

if [[ ! -x "$project_root/.venv/bin/python" ]]; then
  "$python_bin" -m venv "$project_root/.venv"
fi

venv_python="$project_root/.venv/bin/python"
"$venv_python" -m pip install --upgrade pip
if [[ "$mode" == "--full" ]]; then
  "$venv_python" -m pip install -r "$project_root/sto_rag/nc5/requirements.txt"
fi
"$venv_python" -m pip install -r "$project_root/normcontrol-web/requirements.txt"

if [[ "$mode" == "--full" ]]; then
  mkdir -p "$project_root/sto_rag/data/nc5"
  if [[ ! -f "$project_root/sto_rag/data/nc5/config.json" ]]; then
    cp "$project_root/config/config.example.json" "$project_root/sto_rag/data/nc5/config.json"
  fi
fi

echo "Installation completed in $mode mode."
echo "Virtual environment: $project_root/.venv"
