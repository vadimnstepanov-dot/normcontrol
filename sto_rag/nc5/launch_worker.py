"""Credentials stay in a private file and process environment, not command-line arguments."""
import os
import sys
from pathlib import Path
from .worker import run

root=Path(__file__).resolve().parents[2]
envfile=Path(sys.argv[1]) if len(sys.argv)>1 else root/'worker.env'
if not envfile.exists():raise FileNotFoundError('Create worker.env from config/worker.env.example or pass its path as the first argument')
for line in envfile.read_text(encoding='utf-8-sig').splitlines():
    key,sep,value=line.partition('=')
    if sep and key in ('NORMCONTROL_WORKER_TOKEN','NORMCONTROL_WORKER_NAME','NORMCONTROL_PORTAL_URL'):os.environ[key]=value
os.environ.setdefault('NORMCONTROL_WORKER_NAME',os.environ.get('COMPUTERNAME','local-worker'))
portal=os.environ.get('NORMCONTROL_PORTAL_URL','').strip()
if not portal:raise ValueError('NORMCONTROL_PORTAL_URL is required in the private worker env file')
run(portal)
