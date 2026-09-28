import argparse
import json
from pathlib import Path
import sqlite3
from .store import KnowledgeStore

parser=argparse.ArgumentParser(description='Initialize or inspect a separate empty knowledge-v2 store')
parser.add_argument('action',choices=['init','status'])
parser.add_argument('--data',required=True)
args=parser.parse_args()
if args.action=='init':
    store=KnowledgeStore(args.data)
    print(json.dumps({'schema':1,'counts':store.counts()}))
else:
    path=Path(args.data).resolve()/'knowledge-v2.sqlite3'
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        version=db.execute('SELECT version FROM schema_info').fetchone()[0]
        if version!=1:raise SystemExit('Unsupported schema')
        print(json.dumps({'schema':version,'records':db.execute('SELECT count(*) FROM records').fetchone()[0]}))
