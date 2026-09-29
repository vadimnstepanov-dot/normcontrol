"""Read-only CPU benchmark and optional read-only token probes of a saved plan.

No document text, norm text, model responses or credentials are written to the
summary. Token probes send existing packets only to the explicit endpoint.
This is measurement, not a claim that a document conforms to a standard.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'sto_rag'))
from knowledge_v2.performance import session, span, summary


def distribution(values):
    values = sorted(values)
    return dict(n=len(values), min=min(values), median=statistics.median(values), max=max(values),
                total=sum(values)) if values else dict(n=0)


def historical(plan):
    payload, cursor = plan['payload'], plan['cursor']
    results = list(cursor.get('results', {}).values())
    failures = list(cursor.get('failures', {}).values())
    calls = [c for result in results+failures for c in result.get('model_calls', [])]
    stages = {}
    for stage in sorted({c['stage'] for c in calls}):
        selected = [c for c in calls if c['stage']==stage]
        stages[stage] = dict(seconds=distribution([c['seconds'] for c in selected]),
            usage={key: sum(c.get('usage', {}).get(key, 0) for c in selected)
                   for key in ('prompt_tokens', 'completion_tokens')},
            timings={key: sum(c.get('timings', {}).get(key, 0) for c in selected)
                     for key in ('prompt_ms', 'predicted_ms', 'prompt_n', 'predicted_n')})
    blocks = {b['id']: b for d in payload['documents'] for b in d['blocks']}
    repeated = [b for packet in payload['batches'] for b in packet['payload']['documents']]
    return dict(snapshot=payload['snapshot'], batches=len(payload['batches']), rows=len(payload['rows']),
        completed_batches=len(results), failed_batches=len(failures),
        batch_seconds=distribution([r['seconds'] for r in results if 'seconds' in r]),
        calls=len(calls), stages=stages,
        unique_blocks=len(blocks), submitted_block_occurrences=len(repeated),
        unique_characters=sum(len(b['text']) for b in blocks.values()),
        submitted_characters=sum(len(b['text']) for b in repeated),
        # Whitelisted categories only: never copy exception messages into telemetry.
        quote_validation_failures=sum('Evidence quote' in r.get('error', '') for r in failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--endpoint', help='Optional trusted llama.cpp endpoint for token probes, no generation')
    parser.add_argument('--probe-count', type=int, default=8)
    parser.add_argument('--plan', action='store_true', help='Also rebuild the saved normative plan twice; no inference')
    args = parser.parse_args()
    if args.repeats < 1 or args.probe_count < 1:
        parser.error('repeats and probe-count must be positive')
    if args.endpoint and not args.baseline:
        parser.error('--endpoint requires --baseline')
    if args.plan and not (args.baseline and args.endpoint):
        parser.error('--plan requires --baseline and --endpoint')
    source = args.document.resolve(strict=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    output = dict(document_sha256=before, file_bytes=source.stat().st_size,
                  python=platform.python_version(), platform=platform.platform(), runs=[])
    from nc5.documents import parse, coalesce_groups
    from nc5.checks import deterministic
    from nc5.formatting import measure, document_layout
    from nc5.language_candidates import candidates
    from nc5.planning import document_registry
    from knowledge_v2.review import corpus
    empty = dict(profiles=[], cards=[], sources=[], limitations=[])
    for index in range(args.repeats):
        path = args.output/('cpu-'+str(index)+'.jsonl')
        if path.exists():
            parser.error('Use a new output directory to avoid mixing benchmark runs')
        with session(path), span('cpu.total'):
            old = parse(source, empty)
            new = corpus([source])[0]
            registry = document_registry(old)
            local = deterministic(old)
            with span('cpu.format_measure'):
                formatting = measure(source)
                layout = document_layout(source)
            language = candidates(old)
            with span('cpu.grouping'):
                groups = {stage: len(coalesce_groups(old, target_chars=limit))
                          for stage, limit in [('language', 14000), ('logic', 12000)]}
        output['runs'].append(summary(path))
        output['counts'] = dict(nc5_blocks=len(old['blocks']), v2_blocks=len(new['blocks']),
            v2_gaps=len(new['gaps']), headings=len(old['headings']),
            deterministic_findings=len(local), language_candidates=len(language), groups=groups,
            tables=len({b['table'] for b in new['blocks'] if b.get('table')}))
    if args.baseline:
        plan = json.loads(args.baseline.read_text(encoding='utf8'))
        if before not in {d['sha256'] for d in plan['payload']['documents']}:
            raise ValueError('Baseline is from a different document revision')
        output['historical'] = historical(plan)
        if args.endpoint:
            from knowledge_v2.review_client import LlamaClient
            packets = plan['payload']['batches']
            indices = sorted({round(i*(len(packets)-1)/max(1, args.probe_count-1))
                              for i in range(min(len(packets), args.probe_count))})
            with session(args.output/'tokens.jsonl'), span('token_probe.total'):
                client = LlamaClient(args.endpoint)
                output['probe_model_signature'] = client.signature
                output['probe_context'] = client.context
                output['token_probes'] = []
                for index in indices:
                    packet = packets[index]['payload']
                    times = []
                    for _ in range(2):
                        started = time.perf_counter()
                        count = client.count(packet)
                        times.append(time.perf_counter()-started)
                    output['token_probes'].append(dict(index=index, tokens=count,
                                                       cold_seconds=times[0], cached_seconds=times[1]))
                connection = getattr(client, '_token_connection', None)
                if connection:
                    connection.close()
            output['token_metrics'] = summary(args.output/'tokens.jsonl')
            if args.plan:
                from knowledge_v2.review import plan as build_plan
                client = LlamaClient(args.endpoint)
                output['planning_runs'] = []
                payload = plan['payload']
                for attempt in range(2):
                    timing_path = args.output/('planning-'+str(attempt)+'.jsonl')
                    with session(timing_path), span('planning.total'):
                        batches, oversized = [], []
                        for identity, scope in payload['scopes'].items():
                            selected = [r for r in payload['rows'] if r['document_id']==identity
                                        and not r.get('execution_issues', r['issues'])
                                        and r['applicability']['result']=='applicable']
                            expected = set(scope['expected_ids'])
                            blocks = [b for d in payload['documents'] for b in d['blocks']
                                      if b['id'] in expected]
                            packed, failed = build_plan(selected, blocks, client, scope)
                            batches.extend(packed)
                            oversized.extend(failed)
                    output['planning_runs'].append(dict(batches=len(batches), oversized=len(oversized),
                        identical_to_saved=[b['id'] for b in batches]==[b['id'] for b in payload['batches']],
                        performance=summary(timing_path)))
                    # Save each finished measurement even if a later probe fails.
                    (args.output/'planning-summary.json').write_text(json.dumps(output['planning_runs'], indent=2), encoding='utf8')
                output['model_unchanged_during_planning'] = LlamaClient(args.endpoint).signature == client.signature
                connection = getattr(client, '_token_connection', None)
                if connection:
                    connection.close()
    output['source_unchanged'] = hashlib.sha256(source.read_bytes()).hexdigest()==before
    (args.output/'summary.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(dict(output=str(args.output/'summary.json'), counts=output['counts'],
                          source_unchanged=output['source_unchanged']), ensure_ascii=True))


if __name__ == '__main__':
    main()
