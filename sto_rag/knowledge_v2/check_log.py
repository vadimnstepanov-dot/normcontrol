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
    def deliver(self, bridge, claim):
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
                encoded=encode(event);parts=[encoded[n:n+24000] for n in range(0,len(encoded),24000)]
                pieces=[dict(event_id=event['id'],kind=event['kind'],part=n,total=len(parts),text=part,sha256=checksum(event)) for n,part in enumerate(parts)]
                for n in range(0,len(pieces),20):
                    send(pieces[n:n+20])
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
