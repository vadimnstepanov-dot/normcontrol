"""Opt-in durable recovery of saved trace packets; original attempts stay intact."""
import copy,json,threading,time
from collections import Counter
from .store import checksum,Conflict
from .trace import validate_trace
from .model_queue import model_turn
from .review_wire import TRACE_REFS_VERSION,TRACE_COMPACT_VERSION

VERSION='trace-saved-recovery-v2'
MAX_BLOCKS=256
MAX_DEPTH=12
MAX_CALLS_PER_ROOT=64


def check_plan(plan,docs):
    """Check exact sources and exhaustive collector coverage before any reuse."""
    blocks={b['id']:dict(b,document_name=d['name']) for d in docs for b in d['blocks']}
    batches={b['id']:b for b in plan['batches']}
    if len(batches)!=len(plan['batches']):raise Conflict('Duplicate trace packet')
    for b in batches.values():
        p=b['payload']
        if checksum(p)!=b['id']:raise Conflict('Trace packet changed')
        scope=p['completeness']
        selected=[next(d for d in docs if d['id']==did) for did in scope['document_ids']]
        if scope['gaps']!=[g for d in selected for g in d.get('gaps',[])]:raise Conflict('Trace source gaps changed')
        for x in p['documents']+b.get('source_blocks',[]):
            if blocks.get(x['id'])!=x:raise Conflict('Trace source changed')
        if b.get('collectors'):
            source=b['source_blocks']
            expected=[x['id'] for x in source]
            if scope['expected_ids']!=expected:raise Conflict('Trace expected scope changed')
            read=[]
            for cid in b['collectors']:
                child=batches[cid]['payload']
                if child['stage']!='trace_collect' or child['obligations']!=p['obligations']:
                    raise Conflict('Trace collection dependency changed')
                if child['completeness']['gaps']!=scope['gaps'] or child['completeness']['expected_ids']!=expected:
                    raise Conflict('Trace collection scope changed')
                read.extend(x['id'] for x in child['documents'])
            if read!=expected:raise Conflict('Trace collection is not exhaustive and ordered')
    return batches


def validated_seeds(plan,old):
    seeds={}
    for b in plan['batches']:
        if b['id'] not in old.get('results',{}):continue
        p=b['payload']
        if p['stage']!='trace_collect':
            if old.get('provenance',{}).get(b['id'],{}).get('kind')!='model_verified':continue
            if b.get('collectors'):
                if any(k not in old['results'] for k in b['collectors']):raise Conflict('Inherited final lacks collectors')
                picked={e['block_id'] for k in b['collectors'] for d in old['results'][k] for e in d['evidence']}
                p=dict(p,documents=[x for x in b['source_blocks'] if x['id'] in picked])
        result=old['results'][b['id']]
        valid=validate_trace(p,dict(decisions=result))
        if valid!=result:raise Conflict('Inherited trace evidence changed during validation')
        seeds[b['id']]=copy.deepcopy(valid)
    return seeds


def recover_packets(plan,client,cursor,save,allowed,checkpoint=lambda *a:False,turn=None):
    """Splits retain exact ordered blocks/scope; concatenation issues no verdict."""
    from contextlib import nullcontext
    turn=turn or nullcontext
    for key in ('results','failures','splits','attempts','provenance','root_calls'):
        cursor.setdefault(key,{})
    cursor.setdefault('history',[])
    roots=plan['batches']
    def gate():
        if not allowed():raise PermissionError('Trace ACL or immutable snapshot revoked')
    def failed(bid,error):
        cursor['failures'][bid]=dict(error=str(error)[:300]);save()
        return False
    def split(p,bid,root,depth,reason):
        if len(p['documents'])<2 or depth>=MAX_DEPTH:
            return failed(bid,'Indivisible trace collection remains incomplete: '+reason)
        mid=len(p['documents'])//2
        children=[dict(p,documents=p['documents'][a:z]) for a,z in ((0,mid),(mid,len(p['documents'])))]
        ids=[checksum(x) for x in children]
        old=cursor['splits'].get(bid)
        if old and old['children']!=ids:raise Conflict('Trace split changed')
        cursor['splits'][bid]=dict(children=ids,reason=reason,root=root)
        save()
        completed=[collect(x,root,depth+1) for x in children]
        if not all(completed):return failed(bid,'Incomplete trace child collection')
        rows=[]
        for obligation in p['obligations']:
            evidence=[];seen=set()
            for cid in ids:
                row=next(d for d in cursor['results'][cid] if d['obligation_id']==obligation['id'])
                for e in row['evidence']:
                    key=(e['block_id'],e['quote'])
                    if key not in seen:evidence.append(e);seen.add(key)
            rows.append(dict(obligation_id=obligation['id'],outcome='unknown',claim='unknown',
                reason='Объединены доказательства всех дочерних частей; вердикт о связи ещё не вынесен.',evidence=evidence))
        cursor['results'][bid]=validate_trace(p,dict(decisions=rows))
        cursor['provenance'][bid]=dict(kind='cpu_evidence_union',children=ids)
        cursor['failures'].pop(bid,None);save();return True
    def ask(p,bid,root):
        gate()
        if cursor['root_calls'].get(root,0)>=MAX_CALLS_PER_ROOT:
            raise ValueError('Trace recovery call budget exhausted')
        cursor['attempts'][bid]=cursor['attempts'].get(bid,0)+1
        cursor['root_calls'][root]=cursor['root_calls'].get(root,0)+1
        save()
        with turn():raw=client.complete(p)
        gate()
        result=validate_trace(p,raw)
        return result
    def collect(p,root,depth=0):
        bid=checksum(p)
        if bid in cursor['results']:
            validate_trace(p,dict(decisions=cursor['results'][bid]));return True
        gate()
        if bid in cursor['splits']:return split(p,bid,root,depth,cursor['splits'][bid]['reason'])
        if len(p['documents'])>MAX_BLOCKS:return split(p,bid,root,depth,'Output evidence budget')
        if client.count(p)+client.output_tokens+512>client.context:
            return split(p,bid,root,depth,'Input context budget')
        try:
            rows=ask(p,bid,root)
        except Conflict:raise
        except ValueError as exc:
            cursor['history'].append(dict(batch=bid,root=root,error=str(exc)[:300],at=time.time()));save()
            return split(p,bid,root,depth,str(exc))
        cursor['results'][bid]=rows;cursor['failures'].pop(bid,None)
        cursor['provenance'][bid]=dict(kind='model',model=client.signature,wire=client.wire_version)
        save();return True
    for b in roots:
        if checkpoint(sum(x['id'] in cursor['results'] for x in roots),len(roots)):return False
        gate();p=b['payload'];bid=b['id']
        if bid in cursor['results']:continue
        if p['stage']=='trace_collect':collect(p,bid);continue
        if b.get('collectors'):
            if any(k not in cursor['results'] for k in b['collectors']):
                failed(bid,'Incomplete trace collection');continue
            picked={e['block_id'] for k in b['collectors'] for r in cursor['results'][k] for e in r['evidence']}
            p=dict(p,documents=[x for x in b['source_blocks'] if x['id'] in picked])
        try:
            reserve=client.output_tokens if client.wire_version==TRACE_COMPACT_VERSION else 2*client.output_tokens
            if client.count(p)+reserve+512>client.context:
                raise ValueError('All collected evidence exceeds context; none was truncated')
            # Store first pass to avoid repeating a successful call after interruption.
            proposed_id=checksum(['trace-first-pass',bid,p])
            proposed=cursor.get('proposed',{}).get(proposed_id)
            if proposed is None:
                proposed=ask(p,proposed_id,bid)
                cursor.setdefault('proposed',{})[proposed_id]=proposed;save()
            second=dict(p,stage='trace_verify',proposed=proposed)
            if client.count(second)+client.output_tokens+512>client.context:
                raise ValueError('Trace verification exceeds context; none was truncated')
            valid=ask(second,bid,bid)
            cursor['results'][bid]=valid;cursor['failures'].pop(bid,None)
            cursor['provenance'][bid]=dict(kind='model_verified',model=client.signature,wire=client.wire_version)
            save()
        except Conflict:raise
        except ValueError as exc:
            cursor['history'].append(dict(batch=bid,error=str(exc)[:300],at=time.time()));failed(bid,exc)
    return True


def run(store,job,plan,old,docs,client,allowed,checkpoint,parent):
    if client.wire_version not in (TRACE_REFS_VERSION,TRACE_COMPACT_VERSION):raise ValueError('Trace recovery needs v7/v8 transport')
    if not allowed():raise PermissionError('Trace ACL or snapshot revoked')
    check_plan(plan,docs);seeds=validated_seeds(plan,old)
    pin=dict(version=VERSION,job_id=job,parent=parent,plan=checksum(plan),old_cursor=checksum(old),
        documents=checksum(docs),model=client.signature,wire=client.wire_version,
        max_blocks=MAX_BLOCKS,max_depth=MAX_DEPTH,max_calls=MAX_CALLS_PER_ROOT)
    # A fixed job cannot silently change model, policy or input on resume.
    tid=store.enqueue('trace.recover','trace-recover:'+job,dict(pin=pin),max_attempts=5)
    task=store.claim(operation='trace.recover',task_id=tid,ttl=690)
    if task:
        cursor=task['cursor'];stop=threading.Event();lost=threading.Event()
        def renew():
            while not stop.wait(20):
                with store.connection() as db:
                    if db.execute("UPDATE tasks SET lease_until=? WHERE id=? AND lease=? AND state='running' AND lease_until>?",
                        (time.time()+690,tid,task['lease'],time.time())).rowcount!=1:lost.set();return
        def save():
            if lost.is_set():raise Conflict('Trace recovery lease lost')
            store.checkpoint(tid,task['lease'],cursor)
        heart=threading.Thread(target=renew,daemon=True);heart.start()
        try:
            if not cursor:
                cursor.update(results=seeds,provenance={k:dict(kind='inherited_validated',parent=parent,wire=parent.get('wire','normative-wire-v6'),
                    inherited_provenance=old.get('provenance',{}).get(k)) for k in seeds})
                save()
            finished=recover_packets(plan,client,cursor,save,allowed,checkpoint,lambda:model_turn(store,client))
            if finished:store.checkpoint(tid,task['lease'],cursor,done=True)
            else:
                with store.connection() as db:
                    db.execute("UPDATE tasks SET state='pending',attempts=MAX(0,attempts-1),lease=NULL,lease_until=NULL WHERE id=? AND lease=?",(tid,task['lease']))
        except Exception as exc:
            store.fail_task(tid,task['lease'],str(exc),permanent=isinstance(exc,(PermissionError,Conflict)));raise
        finally:stop.set();heart.join(timeout=1)
    with store.connection() as db:record=db.execute('SELECT state,cursor FROM tasks WHERE id=?',(tid,)).fetchone()
    cursor=json.loads(record['cursor']);rows=list(plan.get('initial',[]))
    for b in plan['batches']:
        p=b['payload']
        if p['stage']=='trace_collect':continue
        spec=p['obligations'][0];decision=next(iter(cursor.get('results',{}).get(b['id'],[])),None)
        if decision is None:
            rows.append(dict(spec,state='unknown',reason='Не завершена проверка связи.',evidence=[],error=cursor.get('failures',{}).get(b['id'])));continue
        state={'satisfied':'checked','violated':'violated','unknown':'unknown'}[decision['outcome']]
        limited=bool(p['completeness']['gaps']) or not spec['link']['trusted']
        preliminary=state=='violated' and limited
        rows.append(dict(spec,state='unknown' if limited else state,preliminary_violation=preliminary,
            reason=decision['reason'],evidence=decision['evidence'],claim=decision['claim']))
    errors=cursor.get('failures',{})
    return dict(version=VERSION,task_id=tid,job_id=job,pin=pin,state='partial' if errors else ('done' if record['state']=='done' else 'paused'),
        rows=rows,counts=dict(Counter(r['state'] for r in rows)),errors=errors,
        completed_roots=sum(b['id'] in cursor.get('results',{}) for b in plan['batches']),
        total_roots=len(plan['batches']),derived_splits=len(cursor.get('splits',{})),
        limitations=['Сбор доказательств не подтверждает положительное покрытие каждой обязанности ДТЗ.',
                    'Неподтверждённые нормативные связи и ограничения чтения сохраняют предварительный статус.'])
