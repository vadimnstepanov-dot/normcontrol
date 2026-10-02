"""Linux owner of the unified queue, TLS gateway and model maintenance boundary."""
import hmac,ipaddress,json,os,ssl,threading,time,uuid,urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from .runtime import initialize
initialize()
from .transport import Transport,secret
from .resources import HostProbe
from nc5.common import DATA,read,write
from nc5.runtime import Lease,BusyError
from pipeline import Coordinator,artifact_path

class Queue(Coordinator):
    def __init__(self,path,probe):
        self.probe=probe
        self.pause_file=DATA/'queue-maintenance.json'
        super().__init__(path,memory_probe=self.budget)
    def budget(self):
        total,available=self.probe.memory()
        return total,0 if self.pause_file.exists() else available
    def ticket(self,*args,**kwargs):
        result=super().ticket(*args,**kwargs)
        if self.pause_file.exists() and result['state']=='waiting':
            result['waiting_reason']='resource';result['available_mb']=self.probe.memory()[1]
        return result
    def resources(self,pipeline):
        result=super().resources(pipeline);sample=self.probe.snapshot()
        result.update(available_mb=self.probe.memory()[1],host_available_mb=sample['available_mb'],linux_available_mb=sample['linux_available_mb'],linux_reserve_mb=sample['linux_reserve_mb'],maintenance=self.pause_file.exists(),owner='linux-gateway')
        if result['maintenance']:
            for item in result['queue']:
                if item['state']=='waiting':item['waiting_reason']='resource'
        return result

def serve(config_path):
    cfg=read(config_path)
    bridge=Transport(cfg['upstream'],secret(cfg['host_token_file']),cfg['certificate'])
    queue=Queue(DATA/'pipeline.sqlite3',HostProbe(bridge))
    allowed=[ipaddress.ip_network(value) for value in cfg['allowed_networks']]
    control=threading.Lock()
    get_routes={'/health','/props','/v1/models','/core/health'}
    post_routes={'/apply-template','/tokenize','/v1/chat/completions','/model/profile','/word/read','/word/anchors','/word/review','/pipeline/ticket','/pipeline/artifact','/pipeline/status','/pipeline/phase','/pipeline/register','/control/native/pause','/control/native/resume','/control/native/timing','/control/native/resources'}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def send(self,status,value):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
        def dispatch(self):
            if not any(ipaddress.ip_address(self.client_address[0]) in network for network in allowed):return self.send(403,{'error':'client not allowed'})
            if not hmac.compare_digest(self.headers.get('Authorization','').removeprefix('Bearer '),cfg['token']):return self.send(401,{'error':'authentication required'})
            if self.path not in (get_routes if self.command=='GET' else post_routes):return self.send(404,{'error':'route not available'})
            lease=None
            try:
                size=int(self.headers.get('Content-Length','0'))
                limit=80*1024**2 if self.path in ('/word/review','/word/anchors') else 50*1024**2 if self.path=='/word/read' else 8*1024**2
                if not 0<=size<=limit:return self.send(413,{'error':'request too large'})
                raw=self.rfile.read(size) if self.command=='POST' else None
                if self.path.startswith('/word/'):
                    headers={key:self.headers[key] for key in ('Content-Type','X-Source-SHA256','X-Source-Format') if key in self.headers}
                    with bridge.open(self.path,raw,headers,timeout=300) as response:
                        length=int(response.headers['Content-Length'])
                        if length>220*1024**2:raise ValueError('Word response limit')
                        self.send_response(response.status)
                        for key in ('Content-Type','Content-Length','X-Source-SHA256','X-Artifact-SHA256','X-Word-Report'):
                            if key in response.headers:self.send_header(key,response.headers[key])
                        self.send_header('Cache-Control','no-store');self.end_headers()
                        while chunk:=response.read(65536):self.wfile.write(chunk)
                    return
                if self.path=='/core/health':return self.send(200,{'ready':True,'queue_owner':'linux','version':'native-core-v1'})
                if self.path.startswith('/control/'):
                    if not hmac.compare_digest(self.headers.get('X-Core-Control-Token',''),bridge.token):return self.send(403,{'error':'maintenance authentication required'})
                    from .checkpoint import pause,wait,resume
                    value=json.loads(raw)
                    with control:
                        if self.path.endswith('/resources'):
                            total,available=queue.probe.memory()
                            with queue.db() as db:items=[dict(row) for row in db.execute('SELECT state,ram_mb,resource FROM tickets WHERE expires>?',(time.time(),))]
                            return self.send(200,{'total_mb':total,'available_mb':available,'queue':items})
                        if self.path.endswith('/timing'):
                            from nc5.store import Store
                            with Store().connect() as db:
                                row=db.execute("SELECT ended,stage,result FROM tasks WHERE state='done' AND ended>? AND result IS NOT NULL ORDER BY ended DESC LIMIT 1",(value.get('after',0),)).fetchone()
                            result={'ended':row['ended'],'stage':row['stage'],'result':json.loads(row['result'])} if row else None
                            return self.send(200,result)
                        if self.path.endswith('/pause'):
                            if queue.pause_file.exists():
                                wait();return self.send(200,read(queue.pause_file))
                            token,ids=pause()
                            write(queue.pause_file,{'token':token,'jobs':ids})
                            wait()
                            result={'token':token,'jobs':ids}
                        else:
                            state=read(queue.pause_file)
                            if state['token']!=value['token']:raise ValueError('Maintenance ownership')
                            ids=resume(value['token']);queue.pause_file.unlink();result={'resumed':ids}
                    return self.send(200,result)
                if self.path.startswith('/pipeline/'):
                    value=json.loads(raw);pipeline=str(uuid.UUID(value['pipeline']))
                    if pipeline!=value['pipeline']:raise ValueError('Pipeline identity')
                    if self.path=='/pipeline/ticket':
                        stage=value['stage'];resource=value['resource']
                        if not isinstance(stage,str) or len(stage)>64:raise ValueError('Stage')
                        if value['action']=='release':queue.release(value['id'],pipeline);result={'released':True}
                        elif value['action']=='acquire':result=queue.ticket(pipeline,stage,resource,value.get('id'),value.get('ram_mb',512))
                        else:raise ValueError('Ticket action')
                    elif self.path=='/pipeline/register':
                        directions=value['directions']
                        if not isinstance(directions,list) or not set(directions)<=set(('language','logic','sto','formatting')):raise ValueError('Directions')
                        queue.register(pipeline,directions);result={'registered':True}
                    elif self.path=='/pipeline/status':result={'phases':queue.status(pipeline),'resources':queue.resources(pipeline)}
                    elif self.path=='/pipeline/phase':queue.phase(pipeline,value['stage'],value['state'],value.get('error',''));result={'accepted':True}
                    else:
                        path=artifact_path(pipeline)
                        if not path.exists():return self.send(202,{'ready':False})
                        if value.get('metadata'):result={'ready':True,'bytes':path.stat().st_size}
                        else:
                            with path.open('rb') as source:
                                if source.read(1)!=b'{':raise ValueError('Artifact format')
                                prefix=b'{"ready":true,'
                                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(path.stat().st_size+len(prefix)-1));self.end_headers();self.wfile.write(prefix)
                                while chunk:=source.read(65536):self.wfile.write(chunk)
                            return
                    return self.send(200,result)
                if self.path=='/v1/chat/completions':
                    if json.loads(raw).get('stream'):return self.send(400,{'error':'non-streaming requests only'})
                    lease=Lease(DATA/'model.lock');lease.__enter__()
                with bridge.open(self.path,raw,{'Content-Type':'application/json'},timeout=cfg.get('timeout',360)) as response:
                    body=response.read(24*1024**2+1)
                    if len(body)>24*1024**2:raise ValueError('Model response size')
                    return self.send(response.status,body)
            except BusyError:return self.send(409,{'error':'model busy'})
            except urllib.error.HTTPError as error:return self.send(error.code,{'error':'upstream unavailable'})
            except Exception:return self.send(503,{'error':'core service unavailable'})
            finally:
                if lease:lease.__exit__()
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
    server=ThreadingHTTPServer((cfg.get('host','0.0.0.0'),cfg.get('port',8098)),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cfg['certificate'],cfg['private_key']);server.socket=context.wrap_socket(server.socket,server_side=True)
    print('Linux core gateway ready',flush=True);server.serve_forever()

if __name__=='__main__':
    import sys
    serve(sys.argv[1])
