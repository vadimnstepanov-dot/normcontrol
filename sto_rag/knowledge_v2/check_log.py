"""Opt-in immutable diagnostic events. No extra inference or hardware polling."""
import contextvars
import functools
import hashlib
import json
import time
import uuid
import threading
from contextlib import contextmanager
from .store import encode, checksum

active = contextvars.ContextVar('knowledge_check_log', default=None)
SECRETS = {'authorization','cookie','set-cookie','api_key','key_encrypted','password','secret','access_token','refresh_token','worker_token'}


def clean(value):
    if isinstance(value, dict):
        return {k: ('[СКРЫТО]' if str(k).casefold() in SECRETS or str(k).casefold().endswith(('_password','_secret','_api_key','_worker_token')) else clean(v)) for k,v in value.items()}
    if isinstance(value, list): return [clean(v) for v in value]
    if isinstance(value,str) and value.startswith('data:image/'):
        return {'image_sha256':hashlib.sha256(value.encode()).hexdigest(),'representation':'image data omitted'}
    return value


class Journal:
    def __init__(self, directory, job):
        directory.mkdir(parents=True,exist_ok=True)
        self.path = directory/(str(uuid.UUID(job))+'.jsonl')
        self.job = job
        self.lock=threading.Lock()
        self.delivery_error=self.path.with_suffix('.delivery-error')
    def append(self, kind, value):
        event=dict(id=str(uuid.uuid4()),job_id=self.job,kind=kind,at=time.time(),value=clean(value))
        # Append a complete line. Export never reads a partial last line.
        with self.lock,self.path.open('a',encoding='utf8') as f: f.write(encode(event)+'\n')
    def deliver(self, bridge, claim, max_chunks=None):
        sent=0
        sequence=0;offset=0
        marker=self.path.with_suffix('.delivered')
        cursor=self.path.with_suffix('.cursor')
        acknowledged=int(marker.read_text()) if marker.exists() else -1
        if cursor.exists():
            saved=json.loads(cursor.read_text());sequence=saved['sequence'];offset=saved['offset']
            if sequence>acknowledged+1:raise ValueError('Journal cursor ahead of acknowledgement')
        def send(entries):
            if sequence<=acknowledged:return
            bridge.transport('/worker/checks/log/',dict(command_id=claim['command_id'],lease=claim['lease'],job_id=self.job,
                sequence=sequence,entries=entries,digest=checksum(entries)))
            marker.write_text(str(sequence),encoding='ascii')
        with self.lock,self.path.open('rb') as f:
            f.seek(offset)
            while True:
                line=f.readline()
                if not line:break
                if not line.endswith(b'\n'):break
                try:event=json.loads(line)
                except ValueError:break
                # Each object is split losslessly for bounded transport and XLSX cells.
                encoded=encode(event);total=(len(encoded)+23999)//24000
                event_hash=checksum(event)
                for start in range(0,total,20):
                    if max_chunks is not None and sent>=max_chunks and sequence>acknowledged:return
                    if getattr(bridge,'stop_event',None) is not None and bridge.stop_event.is_set():return
                    pieces=[dict(event_id=event['id'],kind=event['kind'],part=n,total=total,
                        text=encoded[n*24000:(n+1)*24000],sha256=event_hash) for n in range(start,min(total,start+20))]
                    send(pieces)
                    if sequence>acknowledged:sent+=1
                    sequence+=1
                temporary=cursor.with_suffix('.cursor.tmp')
                temporary.write_text(json.dumps(dict(sequence=sequence,offset=f.tell())),encoding='ascii')
                temporary.replace(cursor)


def emit(kind,value):
    journal=active.get()
    if journal:
        try:journal.append(kind,value)
        except OSError as exc:
            # Preserve the model result but record that the diagnostic stream has a gap.
            try:journal.path.with_suffix('.write-error').write_text(type(exc).__name__,encoding='utf8')
            except OSError:pass


@contextmanager
def call(request, configuration):
    identity=str(uuid.uuid4());start=time.monotonic()
    emit('request',dict(call_id=identity,request=request,configuration=configuration))
    try:yield lambda response:emit('response',dict(call_id=identity,seconds=time.monotonic()-start,response=response))
    except Exception as e:
        emit('error',dict(call_id=identity,seconds=time.monotonic()-start,error=type(e).__name__,message=str(e)))
        raise


def logged(fn):
    @functools.wraps(fn)
    def wrapper(bridge,claim,*args,**kwargs):
        if not claim['payload'].get('logging',{}).get('enabled'):return fn(bridge,claim,*args,**kwargs)
        journal=Journal(bridge.store.directory/'check-logs',claim['payload']['job_id'])
        token=active.set(journal)
        try:
            emit('passport',claim['payload'])
            result=fn(bridge,claim,*args,**kwargs)
            emit('result',result)
            return result
        except Exception as exc:
            emit('error',dict(error=type(exc).__name__,message=str(exc)));raise
        finally:
            try:
                fault=journal.path.with_suffix('.write-error')
                if fault.exists():journal.append('error',dict(journal_incomplete=True,reason='Local journal write failed: '+fault.read_text()))
                journal.deliver(bridge,claim)
                journal.delivery_error.unlink(missing_ok=True)
            except Exception as exc:
                # Persisted spool is replayed on continuation. Report completeness explicitly.
                journal.delivery_error.write_text(type(exc).__name__,encoding='utf8')
            finally:active.reset(token)
    return wrapper


def compact_plan(plan):
    """Exact references remove repeated rows, blocks, scopes and row contexts."""
    from .review_wire import Pool
    fields={k:v for k,v in plan.items() if k not in ('batches','rows')}
    fields['journal_representation']='plan-references-v2'
    shared=Pool('R');scope_bases=Pool('S');fields['rows']=[];fields['batches']=[]
    for row in plan['rows']:
        packed=dict(row);refs={}
        for key in ('context','applicability','publication_trust','composition_group','profile_versions'):
            if key in packed and len(encode(packed[key]))>1200:refs[key]=shared.add(packed.pop(key))
        if refs:packed['journal_value_refs']=refs
        fields['rows'].append(packed)
    for batch in plan['batches']:
        packet=batch['payload'];body={k:v for k,v in packet.items() if k not in ('documents','obligations')}
        scope=packet.get('completeness')
        if isinstance(scope,dict):
            local={k:v for k,v in scope.items() if k in ('part','parts','full_text','submitted_ids')}
            base={k:v for k,v in scope.items() if k not in local}
            body['completeness']=dict(local,journal_base_ref=scope_bases.add(base))
        fields['batches'].append(dict(id=batch['id'],payload=body,
            document_refs=[b['id'] for b in packet['documents']],obligation_refs=[r['id'] for r in packet['obligations']]))
    fields['journal_row_values']=shared.items;fields['journal_scope_bases']=scope_bases.items
    return fields


def expand_plan(compact):
    """Verify the normalized journal can reconstruct its canonical plan exactly."""
    fields={k:v for k,v in compact.items() if k not in ('journal_representation','journal_row_values','journal_scope_bases','rows','batches')}
    fields['rows']=[]
    for row in compact['rows']:
        value={k:v for k,v in row.items() if k!='journal_value_refs'}
        value.update({k:compact['journal_row_values'][ref] for k,ref in row.get('journal_value_refs',{}).items()});fields['rows'].append(value)
    rows={r['id']:r for r in fields['rows']};blocks={b['id']:b for d in fields['documents'] for b in d['blocks']}
    fields['batches']=[]
    for batch in compact['batches']:
        packet=dict(batch['payload']);scope=packet.get('completeness')
        if isinstance(scope,dict) and 'journal_base_ref' in scope:
            packet['completeness']=dict(compact['journal_scope_bases'][scope['journal_base_ref']],**{k:v for k,v in scope.items() if k!='journal_base_ref'})
        packet.update(documents=[blocks[k] for k in batch['document_refs']],obligations=[rows[k] for k in batch['obligation_refs']])
        fields['batches'].append(dict(id=batch['id'],payload=packet))
    return fields
