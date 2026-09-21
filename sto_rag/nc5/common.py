import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'sto_rag/data/nc5'

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else dumps(value).encode()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(dumps(value)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

DEFAULT_CONFIG = dict(endpoint='http://127.0.0.1:8082', model='local-qwen', context=20480,
    output=4096, margin=1536, timeout=300, retries=1, ram_bytes=8*1024**3,
    port=8096, formatting='off', check_language=True, check_logic=True, check_sto=True,
    language_chunk_chars=14000, logic_chunk_chars=12000, reference_group_size=3,
    verification_group_size=3, sto_group_size=10, sto_group_chars=52000,
    max_file_bytes=50*1024**2, max_unpacked_bytes=200*1024**2)

def config():
    return {**DEFAULT_CONFIG, **(read(DATA/'config.json') if (DATA/'config.json').exists() else {})}
