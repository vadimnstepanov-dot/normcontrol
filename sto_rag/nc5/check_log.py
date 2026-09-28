"""Bind diagnostics inside the engine thread; transport is done by the bridge thread."""
import functools
from .common import DATA
from knowledge_v2.check_log import Journal,active,emit

def logged(fn):
    @functools.wraps(fn)
    def wrapper(engine,jid,*args,**kwargs):
        job=engine.store.job(jid)
        if not job['data']['options'].get('logging_enabled'):return fn(engine,jid,*args,**kwargs)
        journal=Journal(DATA/'check-logs',jid);token=active.set(journal)
        try:
            emit('passport',job['data'])
            result=fn(engine,jid,*args,**kwargs)
            emit('plan',engine.store.tasks(jid));emit('result',engine.status(jid))
            return result
        except Exception as e:
            emit('error',dict(error=type(e).__name__,message=str(e)));raise
        finally:active.reset(token)
    return wrapper

def deliver(jid,claim,request):
    class Bridge:
        def transport(self,path,data):
            return request(f'/worker/{claim["lease"]}/log/',dict(sequence=data['sequence'],entries=data['entries'],digest=data['digest']))
    journal=Journal(DATA/'check-logs',jid)
    if journal.path.exists():journal.deliver(Bridge(),dict(command_id=claim['job'],lease=claim['lease']))
