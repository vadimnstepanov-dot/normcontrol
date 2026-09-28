"""Cold backup/restore and low-cost diagnostics for an isolated knowledge-v2 stack.

Run with Python 3.12 on the Linux Docker host (or inside WSL2). This program
deliberately never stops a running service or overwrites an existing data set.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy/knowledge-v2/compose.yaml'
ARCHIVE_IMAGE = 'python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e'
SERVICES = ('qdrant', 'embeddings', 'index-worker', 'knowledge-worker', 'expert-worker', 'review-worker', 'knowledge-portal')


def run(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def compose(project, env_file, *args, capture=False):
    return run('docker', 'compose', '-p', project, '-f', str(COMPOSE),
               '--env-file', str(env_file), *args, capture=capture)


def container(project, env_file, service):
    ids=run('docker','ps','-a','--filter',f'label=com.docker.compose.project={project}',
            '--filter',f'label=com.docker.compose.service={service}','--format','{{.ID}}',capture=True).splitlines()
    if len(ids)>1:raise RuntimeError(f'{service} has multiple replicas; stop and inspect before backup')
    return ids[0] if ids else ''


def require_stopped(project, env_file):
    active=run('docker','ps','--filter',f'label=com.docker.compose.project={project}',
               '--format','{{.Names}}',capture=True)
    if active:raise RuntimeError('Project containers are running; pause work and stop the WHOLE stack before backup/restore')


def volume_name(cid, destination):
    mounts = json.loads(run('docker', 'inspect', '-f', '{{json .Mounts}}', cid, capture=True))
    found = [m['Name'] for m in mounts if m['Type'] == 'volume' and m['Destination'] == destination]
    if len(found) != 1:
        raise RuntimeError(f'Expected one named volume at {destination}')
    return found[0]


def volume_archive(volume, target, restore=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    if restore:
        run('docker', 'run', '--rm', '--user', '0', '-v', f'{volume}:/volume',
            '-v', f'{target.parent}:/archive:ro', ARCHIVE_IMAGE,
            'tar', 'xf', f'/archive/{target.name}', '-C', '/volume')
    else:
        run('docker', 'run', '--rm', '--user', '0', '-v', f'{volume}:/volume:ro',
            '-v', f'{target.parent}:/archive', ARCHIVE_IMAGE,
            'tar', 'cf', f'/archive/{target.name}', '-C', '/volume', '.')


def files(root):
    return sorted(x for x in root.rglob('*') if x.is_file())


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def db_check(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError(f'SQLite integrity check failed: {path.name}')


def no_active_tasks(data_dir):
    db_path = data_dir / 'knowledge-v2.sqlite3'
    if not db_path.exists():
        return
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as db:
        if db.execute("SELECT count(*) FROM tasks WHERE state='running'").fetchone()[0]:
            raise RuntimeError('Canonical tasks have running leases; wait for checkpoint/lease recovery')


def manifest(directory):
    return {str(p.relative_to(directory)).replace('\\', '/'): digest(p)
            for p in files(directory) if p.name != 'manifest.json'}


def backup(args):
    data_dir = args.data_dir.resolve()
    target = args.target.resolve()
    if target.exists() or target == data_dir or data_dir in target.parents:
        raise RuntimeError('Backup target must be new and outside the canonical data directory')
    require_stopped(args.project, args.env_file)
    no_active_tasks(data_dir)
    target.mkdir(parents=True, mode=0o700)
    try:
        if data_dir.exists():
            shutil.copytree(data_dir, target / 'canonical')
            for db in files(target / 'canonical'):
                if db.suffix == '.sqlite3': db_check(db)
        for service, location, name in (('qdrant', '/qdrant/storage', 'qdrant.tar'),
                                        ('knowledge-portal', '/data', 'portal.tar')):
            cid = container(args.project, args.env_file, service)
            if cid:
                volume_archive(volume_name(cid, location), target / name)
        content = {'format': 1, 'project': args.project, 'created_utc': time.time(),
                   'files': manifest(target)}
        (target / 'manifest.json').write_text(json.dumps(content, indent=2, sort_keys=True), encoding='utf-8')
        os.chmod(target / 'manifest.json', 0o600)
        print(json.dumps({'backup': str(target), 'files': len(content['files'])}))
    except Exception:
        shutil.rmtree(target)
        raise


def verify(target):
    expected = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
    if expected.get('format') != 1 or manifest(target) != expected.get('files'):
        raise RuntimeError('Backup manifest or file hashes differ')
    canonical = target / 'canonical'
    if canonical.exists():
        for db in files(canonical):
            if db.suffix == '.sqlite3': db_check(db)
    for name in ('qdrant.tar', 'portal.tar'):
        archive = target / name
        if not archive.exists(): continue
        with tarfile.open(archive) as bundle:
            for member in bundle:
                parts = Path(member.name).parts
                if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError(f'Unsafe archive member in {name}')
                if name == 'portal.tar' and Path(member.name).name == 'portal.sqlite3':
                    with tempfile.NamedTemporaryFile(suffix='.sqlite3') as copy:
                        shutil.copyfileobj(bundle.extractfile(member), copy)
                        copy.flush()
                        db_check(Path(copy.name))
    print(json.dumps({'verified': str(target), 'files': len(expected['files'])}))
    return expected


def restore(args):
    source = args.source.resolve()
    info = verify(source)
    data_dir = args.data_dir.resolve()
    if data_dir.exists() and any(data_dir.iterdir()):
        raise RuntimeError('Restore requires a new empty canonical data directory')
    require_stopped(args.project, args.env_file)
    volumes = []
    for service, location, name in (('qdrant', '/qdrant/storage', 'qdrant.tar'),
                                    ('knowledge-portal', '/data', 'portal.tar')):
        if (source / name).exists():
            cid = container(args.project, args.env_file, service)
            if not cid: raise RuntimeError(f'Create the stopped {service} container before restore')
            volume = volume_name(cid, location)
            if run('docker', 'run', '--rm', '-v', f'{volume}:/volume:ro', ARCHIVE_IMAGE,
                   'find', '/volume', '-mindepth', '1', '-print', capture=True):
                raise RuntimeError(f'{service} volume is not empty')
            volumes.append((volume, source / name))
    if (source / 'canonical').exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / 'canonical', data_dir, dirs_exist_ok=True)
        run('docker', 'run', '--rm', '--user', '0', '-v', f'{data_dir}:/data',
            ARCHIVE_IMAGE, 'chown', '-R', '65532:65532', '/data')
    for volume, archive in volumes: volume_archive(volume, archive, restore=True)
    print(json.dumps({'restored': str(data_dir), 'source_project': info['project'],
                      'note': 'Validate generation search and portal before enabling jobs'}))


def sample(args):
    # Docker statistics only: no requests to the model or normative database.
    while True:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        try:
            raw = run('docker', 'stats', '--no-stream', '--format',
                      '{{json .}}', capture=True)
            for line in raw.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get('Name', '').startswith(args.project + '-'):
                    print(json.dumps({'time': stamp, 'service': item.get('Name'),
                                      'cpu': item.get('CPUPerc'), 'memory': item.get('MemUsage'),
                                      'network': item.get('NetIO')}, ensure_ascii=False), flush=True)
        except subprocess.CalledProcessError as error:
            print(json.dumps({'time': stamp, 'monitor_error': str(error)}), flush=True)
        if args.data_dir:
            path = args.data_dir / 'knowledge-v2.sqlite3'
            if path.exists():
                try:
                    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=3) as db:
                        rows = db.execute('SELECT operation,state,count(*),min(created) FROM tasks GROUP BY operation,state').fetchall()
                        reviews = db.execute("SELECT id,state,payload,cursor FROM tasks WHERE operation='review.run'").fetchall()
                    for operation, state, count, oldest in rows:
                        print(json.dumps({'time': stamp, 'stage': operation, 'state': state,
                                          'tasks': count, 'oldest_age_seconds': round(time.time()-oldest)}), flush=True)
                    for task_id, state, payload, cursor in reviews:
                        data = json.loads(cursor)
                        results = list(data.get('results', {}).values()) + list(data.get('failures', {}).values())
                        print(json.dumps({'time': stamp, 'stage': 'review.run', 'task_id': task_id,
                                          'state': state, 'completed_batches': len(results),
                                          'total_batches': len(json.loads(payload).get('batches', [])),
                                          'measured_batch_seconds': round(sum(x.get('seconds', 0) for x in results), 2)}), flush=True)
                except (sqlite3.Error, ValueError) as error:
                    print(json.dumps({'time': stamp, 'task_monitor_error': type(error).__name__}), flush=True)
        if shutil.which('nvidia-smi'):
            try:
                gpu = run('nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total',
                          '--format=csv,noheader,nounits', capture=True)
                for i, line in enumerate(gpu.splitlines()):
                    utilization, used, total = [x.strip() for x in line.split(',')]
                    print(json.dumps({'time': stamp, 'gpu': i, 'utilization_percent': utilization,
                                      'vram_used_mib': used, 'vram_total_mib': total}), flush=True)
            except (subprocess.CalledProcessError, ValueError):
                pass
        if args.once: break
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--env-file', required=True, type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('backup', 'restore'):
        command = sub.add_parser(name)
        command.add_argument('--data-dir', required=True, type=Path)
        command.add_argument('--target' if name == 'backup' else '--source', required=True, type=Path)
    sub.add_parser('verify').add_argument('--source', required=True, type=Path)
    monitor = sub.add_parser('monitor')
    monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    if args.action == 'backup': backup(args)
    elif args.action == 'restore': restore(args)
    elif args.action == 'verify': verify(args.source.resolve())
    else: sample(args)


if __name__ == '__main__':
    try: main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Operation refused: {exc}', file=sys.stderr)
        raise SystemExit(1)
