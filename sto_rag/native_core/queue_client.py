import threading,time
from contextlib import contextmanager
from pipeline import QueuePaused,waiting

class RemoteCoordinator:
    """All mutations belong to the gateway's Linux queue, never to a Windows share."""
    def __init__(self,transport):self.transport=transport
    def register(self,pipeline,directions):return self.transport.json('/pipeline/register',{'pipeline':pipeline,'directions':directions})
    def phase(self,pipeline,stage,state,error=''):return self.transport.json('/pipeline/phase',{'pipeline':pipeline,'stage':stage,'state':state,'error':error})
    def status(self,pipeline):return self.transport.json('/pipeline/status',{'pipeline':pipeline})['phases']
    def resources(self,pipeline):return self.transport.json('/pipeline/status',{'pipeline':pipeline})['resources']
    def ticket(self,pipeline,stage,resource,identifier=None,ram_mb=512):
        value={'pipeline':pipeline,'stage':stage,'resource':resource,'action':'acquire','ram_mb':ram_mb}
        if identifier:value['id']=identifier
        return self.transport.json('/pipeline/ticket',value)
    def release(self,identifier,pipeline):
        return self.transport.json('/pipeline/ticket',{'pipeline':pipeline,'stage':'release','resource':'gpu','action':'release','id':identifier})
    @contextmanager
    def turn(self,pipeline,stage,resource,timeout=600,ram_mb=512,on_wait=None):
        from .resources import container_available,LINUX_RESERVE_MB
        if resource=='cpu':
            while True:
                total,available=container_available()
                if ram_mb>total-LINUX_RESERVE_MB:raise MemoryError('CPU task exceeds container budget')
                if available-ram_mb>=LINUX_RESERVE_MB:break
                waiting({'state':'waiting','waiting_reason':'ram','available_mb':available,'reserve_mb':LINUX_RESERVE_MB,'scope':'container'},on_wait)
                time.sleep(.5)
        ticket=self.ticket(pipeline,stage,resource,ram_mb=ram_mb);stop=threading.Event();thread=None;lost=[]
        try:
            while ticket['state']!='running':
                waiting(ticket,on_wait);time.sleep(.5)
                ticket=self.ticket(pipeline,stage,resource,ticket['id'],ram_mb)
            def renew():
                while not stop.wait(15):
                    try:
                        if self.ticket(pipeline,stage,resource,ticket['id'],ram_mb)['state']!='running':lost.append(True);return
                    except Exception:lost.append(True);return
            thread=threading.Thread(target=renew,daemon=True);thread.start()
            yield ticket['id']
            if lost:raise RuntimeError('Linux queue lease lost')
        finally:
            stop.set()
            if thread:thread.join(2)
            self.release(ticket['id'],pipeline)
