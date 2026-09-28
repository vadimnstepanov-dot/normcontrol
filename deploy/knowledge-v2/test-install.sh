#!/usr/bin/env bash
# Isolated acceptance smoke test. Requires Docker Engine, Compose and cached E5 model.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$PWD"
tmp="$(mktemp -d /tmp/kv2-install-XXXXXX)"
tag="$(basename "$tmp" | tr '[:upper:]' '[:lower:]')"
first="${tag}-first"
second="${tag}-restore"
env1="$tmp/first.env"
env2="$tmp/restore.env"
model_cache="${1:-/opt/normcontrol/models}"
python3 - "$root/deploy/knowledge-v2/.env.example" "$env1" "$env2" "$tmp" "$model_cache" <<'PY'
import pathlib,secrets,sys
template=pathlib.Path(sys.argv[1]).read_text()
for name,path,base,cache in [('first',sys.argv[2],sys.argv[4],sys.argv[5]),
                             ('restore',sys.argv[3],sys.argv[4],sys.argv[5])]:
    content=template.replace('/opt/normcontrol/models',cache).replace('/opt/normcontrol/data',base+'/'+name+'-data')
    content=content.replace('KNOWLEDGE_WORKER_TOKEN=\n','KNOWLEDGE_WORKER_TOKEN='+secrets.token_urlsafe(36)+'\n')
    content=content.replace('APP_SECRET=\n','APP_SECRET='+secrets.token_urlsafe(48)+'\n')
    content+='\nQDRANT_BIND=127.0.0.1:0:6333\nEMBEDDINGS_BIND=127.0.0.1:0:8109\nPORTAL_BIND=127.0.0.1:0:8110\n'
    pathlib.Path(path).write_text(content)
    pathlib.Path(path).chmod(0o600)
PY
mkdir -p "$tmp/first-data" "$tmp/restore-data"
docker run --rm --user 0 -v "$tmp:/work" python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e chown -R 65532:65532 /work/first-data /work/restore-data
dc() { docker compose -p "$1" -f "$root/deploy/knowledge-v2/compose.yaml" --env-file "$2" "${@:3}"; }
# Stop only the two uniquely named acceptance projects, even after a failed
# assertion. Preserve volumes, evidence and backups for diagnosis.
cleanup() {
  dc "$first" "$env1" --profile portal down --timeout 60 || true
  dc "$second" "$env2" --profile portal down --timeout 60 || true
}
trap cleanup EXIT
dc "$first" "$env1" config -q
dc "$first" "$env1" up -d --build qdrant embeddings index-worker
for _ in $(seq 1 30); do
  state="$(dc "$first" "$env1" ps --format json | python3 -c 'import json,sys; data=[json.loads(x) for x in sys.stdin if x.strip()]; print(sum(x.get("Health")=="healthy" for x in data if x.get("Service") in ("qdrant","embeddings","index-worker")))')"
  [ "$state" = 3 ] && break
  sleep 5
done
[ "$state" = 3 ] || { dc "$first" "$env1" ps; exit 1; }
dc "$first" "$env1" --profile portal build knowledge-portal
dc "$first" "$env1" --profile portal run --rm knowledge-portal python manage.py migrate --noinput
dc "$first" "$env1" --profile portal up -d knowledge-portal
for _ in $(seq 1 50); do
  portal="$(dc "$first" "$env1" ps --all --format json | python3 -c 'import json,sys; print(next((x.get("Health","") for x in map(json.loads,sys.stdin) if x.get("Service")=="knowledge-portal"),""))')"
  [ "$portal" = healthy ] && break
  sleep 3
done
[ "$portal" = healthy ] || { dc "$first" "$env1" ps; exit 1; }
dc "$first" "$env1" exec -T knowledge-portal python -c "import urllib.request; r=urllib.request.Request('http://127.0.0.1:8110/normcontol/static/knowledge-workspace.css',headers={'X-Forwarded-Proto':'https'}); x=urllib.request.urlopen(r,timeout=5); assert x.status==200 and x.headers.get('Content-Type','').startswith('text/css')"
dc "$first" "$env1" exec -T index-worker python -c "import sqlite3,time; db=sqlite3.connect('/data/knowledge-v2.sqlite3'); db.execute(\"INSERT INTO tasks(id,operation,dedupe_key,payload,state,max_attempts,cursor,created) VALUES('stage9-cursor','test.persist','stage9-cursor','{}','pending',3,'{\\\"part\\\":7}',?)\",(time.time(),)); db.commit()"
dc "$first" "$env1" up -d --force-recreate index-worker
dc "$first" "$env1" exec -T index-worker python -c "import sqlite3; db=sqlite3.connect('/data/knowledge-v2.sqlite3'); assert db.execute(\"SELECT cursor FROM tasks WHERE id='stage9-cursor'\").fetchone()[0] == '{\"part\":7}'"
dc "$first" "$env1" --profile portal stop -t 60 knowledge-portal index-worker embeddings qdrant
python3 deploy/knowledge-v2/ops.py --project "$first" --env-file "$env1" backup --data-dir "$tmp/first-data" --target "$tmp/backup"
python3 deploy/knowledge-v2/ops.py --project "$first" --env-file "$env1" verify --source "$tmp/backup"
dc "$second" "$env2" --profile portal create qdrant knowledge-portal
python3 deploy/knowledge-v2/ops.py --project "$second" --env-file "$env2" restore --data-dir "$tmp/restore-data" --source "$tmp/backup"
dc "$second" "$env2" up -d qdrant embeddings index-worker
dc "$second" "$env2" exec -T index-worker python -c "import sqlite3; db=sqlite3.connect('/data/knowledge-v2.sqlite3'); assert db.execute(\"SELECT cursor FROM tasks WHERE id='stage9-cursor'\").fetchone()[0] == '{\"part\":7}'"
echo "ACCEPTED: empty launch, health, cursor after recreate, verified backup, restore: $tmp"
