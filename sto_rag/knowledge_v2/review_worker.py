"""Explicit v2 runtime entry point, deployable in the worker container.

The CLI accepts trusted operator input, never an unauthenticated browser path.
All knowledge access is reauthorized by the portal for the named owner.
"""
import argparse
import json
import os
from pathlib import Path

from .bridge import Bridge
from .review import ReviewRunner, corpus, render_report, release_records
from .review_client import LlamaClient
from .store import KnowledgeStore, encode


def main():
    parser = argparse.ArgumentParser(description='Snapshot-bound RAG v2 normative review')
    parser.add_argument('--data-dir', default=os.getenv('NORMCONTROL_KNOWLEDGE_DATA'))
    parser.add_argument('--portal', default=os.getenv('NORMCONTROL_KNOWLEDGE_PORTAL'))
    parser.add_argument('--user-id', required=True, type=int)
    parser.add_argument('--endpoint', default=os.getenv('NORMCONTROL_LLM_ENDPOINT'))
    parser.add_argument('--context', type=int, default=24576)
    parser.add_argument('--output-tokens', type=int, default=2048)
    parser.add_argument('--experience-scope')
    parser.add_argument('--embedding-url', default=os.getenv('KNOWLEDGE_EMBEDDING_URL','http://127.0.0.1:8109'))
    parser.add_argument('--qdrant-url', default=os.getenv('KNOWLEDGE_QDRANT_URL','http://127.0.0.1:6333'))
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create'); create.add_argument('--spec', required=True)
    commands.add_parser('run-once')
    report = commands.add_parser('report'); report.add_argument('task_id'); report.add_argument('--output', required=True)
    pause = commands.add_parser('pause'); pause.add_argument('task_id')
    resume = commands.add_parser('resume'); resume.add_argument('task_id')
    args = parser.parse_args()
    store = KnowledgeStore(args.data_dir)
    bridge = Bridge(store, args.portal, os.environ['NORMCONTROL_KNOWLEDGE_WORKER_TOKEN'])
    authorize = bridge.authorization_for(args.user_id)
    # Reading reports and issuing pause/resume must work while the model is off.
    client = LlamaClient(args.endpoint, args.context, args.output_tokens,store=store) if args.command in ('create','run-once') else None
    selector=None
    if client and args.experience_scope:
        from .experience import ExperienceSelector
        from .embedding import RemoteEncoder
        from .search import HybridSearch,QdrantIndex
        selector=ExperienceSelector(HybridSearch(store,RemoteEncoder(args.embedding_url),QdrantIndex(args.qdrant_url),authorize),args.experience_scope,lambda *args:False)
    runner = ReviewRunner(store, client, authorize, owner=args.user_id,experience_selector=selector)
    if args.command == 'create':
        spec = json.loads(Path(args.spec).read_text(encoding='utf-8'))
        docs = corpus(spec['paths'])
        lookup = {d['name']: d for d in docs}
        if len(lookup) != len(docs): raise ValueError('Ambiguous filenames: use distinct names')
        profiles = {lookup[name]['id']: value for name, value in spec['profiles'].items()}
        facts = {}
        for doc in docs:
            source_ids = []
            for rid in profiles[doc['id']]:
                _, records = release_records(store, rid, authorize)
                source_ids.extend(key[0] for key,r in records.items() if r['kind']=='source_revision')
            facts[doc['id']] = {'selected_sources': dict(value=sorted(set(source_ids)), complete=True,
                evidence=[dict(source=doc['id'], locator='selected-releases', selected_sources=sorted(set(source_ids)))])}
            # Document type is only established by an exact title quotation.
            title = spec.get('document_types', {}).get(doc['name'])
            if title:
                matches = [b for b in doc['blocks'][:35] if title.casefold() in b['text'].casefold()]
                if matches:
                    facts[doc['id']]['document_type'] = dict(value=title,
                        evidence=[dict(source=doc['id'], locator=matches[0]['locator'], quote=matches[0]['text'])])
        def verify_fact(name, value, evidence):
            doc = next((d for d in docs if d['id']==evidence.get('source')), None)
            if not doc: return False
            if name == 'selected_sources': return value == evidence.get('selected_sources')
            return (name=='document_type' and isinstance(value,str)
                    and any(b['locator']==evidence.get('locator') and b['text']==evidence.get('quote')
                            and value.casefold() in b['text'].casefold() for b in doc['blocks'][:35]))
        # Reuse the parsed, hashed corpus: no second document extraction.
        print(runner.create(spec['paths'], profiles, facts, verify_fact, prepared_docs=docs,experience_releases=spec.get('experience_releases',[])))
    elif args.command == 'run-once':
        print(encode({'processed': runner.run_once()}))
    elif args.command in ('pause', 'resume'):
        runner.pause(args.task_id, args.command == 'pause')
        print(encode({'paused': args.command == 'pause'}))
    else:
        value = runner.report(args.task_id)
        target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_report(value) if target.suffix == '.html' else encode(value), encoding='utf-8')
        print(str(target))


if __name__ == '__main__': main()
