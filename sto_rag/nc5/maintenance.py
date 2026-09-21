"""Consistent local backup, validated extraction, and explicit crash recovery."""
import argparse,hashlib,json,sqlite3,tempfile,zipfile
from contextlib import closing
from pathlib import Path
from .common import DATA,dumps,write
from .runtime import Lease
from .store import Store

def backup(destination,data=DATA):
    data=Path(data).resolve();destination=Path(destination).resolve()
    if destination==data or data in destination.parents:raise ValueError('Сохраняйте резервную копию вне каталога данных')
    destination.parent.mkdir(parents=True,exist_ok=True)
    store=Store(data/'review.sqlite3')
    with Lease(store.path+'.worker.lock'),tempfile.TemporaryDirectory() as tmp:
        snapshot=Path(tmp)/'review.sqlite3'
        with store.connect() as source,closing(sqlite3.connect(snapshot)) as target:source.backup(target)
        manifest={}
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
            files=[(snapshot,'review.sqlite3')]
            # Reproducible caches and logs are not needed to recover a job.
            for name in ('jobs','catalogs','sources','remote'):
                folder=data/name
                if folder.exists():files.extend((p,p.relative_to(data).as_posix()) for p in folder.rglob('*') if p.is_file() and not p.name.endswith('.tmp'))
            files.extend((p,p.name) for p in data.glob('*.json') if p.name in ('config.json','catalog-current.json','bridge-state.json'))
            for path,name in files:
                content=path.read_bytes();manifest[name]=hashlib.sha256(content).hexdigest();archive.writestr(name,content)
            archive.writestr('backup-manifest.json',dumps({'version':1,'files':manifest}))
    return {'archive':str(destination),'files':len(manifest),'bytes':destination.stat().st_size}

def restore(archive,destination):
    destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):raise ValueError('Восстановление разрешено только в новую пустую папку; рабочие данные не перезаписываются')
    with zipfile.ZipFile(archive) as z:
        manifest=json.loads(z.read('backup-manifest.json'))
        if manifest.get('version')!=1:raise ValueError('Неподдерживаемая копия')
        for name,checksum in manifest['files'].items():
            target=(destination/name).resolve()
            if destination not in target.parents or '\\' in name:raise ValueError('Небезопасный путь в архиве')
            if hashlib.sha256(z.read(name)).hexdigest()!=checksum:raise ValueError('Повреждена копия: '+name)
        for name in manifest['files']:
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(name))
    with closing(sqlite3.connect(destination/'review.sqlite3')) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('База данных повреждена')
    return {'restored':str(destination),'files':len(manifest['files'])}

def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('backup');p.add_argument('destination')
    p=sub.add_parser('restore');p.add_argument('archive');p.add_argument('destination')
    p=sub.add_parser('recover');p.add_argument('job',nargs='?')
    a=parser.parse_args()
    if a.action=='backup':print(dumps(backup(a.destination)))
    elif a.action=='restore':print(dumps(restore(a.archive,a.destination)))
    else:
        store=Store()
        with Lease(store.path+'.worker.lock'):store.recover(a.job)
        print(dumps({'recovered':a.job or 'all','state':'paused','next':'resume JOB_ID'}))

if __name__=='__main__':main()
