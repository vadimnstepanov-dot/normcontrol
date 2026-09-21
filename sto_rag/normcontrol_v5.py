"""Run as a script: Python normcontrol_v5.py --help. Imports never read a benchmark."""
import argparse
import json
from pathlib import Path
from nc5.common import config,DATA,write,dumps
from nc5.engine import Engine

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('catalog');sub.add_parser('catalog-audit');sub.add_parser('probe');sub.add_parser('serve')
    worker=sub.add_parser('worker');worker.add_argument('url')
    run=sub.add_parser('run');run.add_argument('paths',nargs='+');run.add_argument('--no-language',action='store_true');run.add_argument('--no-sto',action='store_true');run.add_argument('--no-logic',action='store_true')
    resume=sub.add_parser('resume');resume.add_argument('job')
    stat=sub.add_parser('status');stat.add_argument('job',nargs='?')
    export=sub.add_parser('export');export.add_argument('job')
    args=p.parse_args();e=Engine()
    if args.command=='catalog':
        from nc5.catalog import compile_catalog
        c=compile_catalog();print(dumps({'version':c['version'],'profiles':len(c['profiles']),'cards':len(c['cards']),'coverage':c['coverage']}))
    elif args.command=='probe':print(dumps(e.client.probe()))
    elif args.command=='catalog-audit':
        from nc5.catalog_validation import audit
        print(dumps(audit()))
    elif args.command=='serve':
        from nc5.service import serve
        serve(e)
    elif args.command=='worker':
        from nc5.worker import run
        run(args.url)
    elif args.command in ('run','resume'):
        if args.command=='run':
            jid=e.create(args.paths,{'check_language':not args.no_language,'check_sto':not args.no_sto,'check_logic':not args.no_logic});print(dumps({'job':jid}),flush=True)
        else:jid=args.job
        e.run(jid)
        from nc5.report import export
        export(e,jid);print(dumps(e.status(jid)),flush=True)
    elif args.command=='status':print(dumps(e.status(args.job) if args.job else e.store.jobs()))
    elif args.command=='export':
        from nc5.report import export
        print(export(e,args.job))

if __name__=='__main__':main()
