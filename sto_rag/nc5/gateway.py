"""TLS gateway for the explicitly configured VPS; exposes only model inference APIs."""
import hmac
import json
import ssl
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from .common import DATA,read
from .runtime import Lease,BusyError

def serve(config_path):
    cfg=read(config_path)
    allowed_get={'/health','/props','/v1/models'}
    allowed_post={'/apply-template','/tokenize','/v1/chat/completions','/model/profile','/pipeline/ticket','/pipeline/artifact','/pipeline/status','/pipeline/phase'}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def send(self,status,raw):
            if status>=400:self.close_connection=True
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
        def dispatch(self):
            if self.client_address[0] not in cfg['allowed_clients']:return self.send(403,b'{"error":"client not allowed"}')
            auth=self.headers.get('Authorization','').removeprefix('Bearer ')
            if not hmac.compare_digest(auth,cfg['token']):return self.send(401,b'{"error":"authentication required"}')
            if self.path not in (allowed_get if self.command=='GET' else allowed_post):return self.send(404,b'{"error":"route not available"}')
            lease=None
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<=n<=8*1024**2:return self.send(413,b'{"error":"request too large"}')
                raw=self.rfile.read(n) if self.command=='POST' else None
                if self.path.startswith('/pipeline/'):
                    from pipeline import host,artifact_path
                    import uuid
                    value=json.loads(raw);pipeline=str(uuid.UUID(value['pipeline']))
                    if pipeline!=value['pipeline']:raise ValueError('Pipeline identity')
                    coordinator=host()
                    if self.path=='/pipeline/ticket':
                        stage=value['stage'];resource=value['resource']
                        if not isinstance(stage,str) or len(stage)>64:raise ValueError('Stage')
                        if value['action']=='release':coordinator.release(value['id'],pipeline);result={'released':True}
                        elif value['action']=='acquire':result=coordinator.ticket(pipeline,stage,resource,value.get('id'),value.get('ram_mb',512))
                        else:raise ValueError('Ticket action')
                    elif self.path=='/pipeline/status':result={'phases':coordinator.status(pipeline),'resources':coordinator.resources(pipeline)}
                    elif self.path=='/pipeline/phase':
                        coordinator.phase(pipeline,value['stage'],value['state']);result={'accepted':True}
                    else:
                        path=artifact_path(pipeline)
                        if not path.exists():return self.send(202,b'{"ready":false}')
                        if value.get('metadata'):result={'ready':True,'bytes':path.stat().st_size}
                        else:
                            # Do not materialize two complete JSON copies on the host.
                            with path.open('rb') as source:
                                if source.read(1)!=b'{':raise ValueError('Preparation artifact format')
                                prefix=b'{"ready":true,'
                                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(path.stat().st_size+len(prefix)-1));self.send_header('Cache-Control','no-store');self.end_headers()
                                self.wfile.write(prefix)
                                while True:
                                    chunk=source.read(64*1024)
                                    if not chunk:break
                                    self.wfile.write(chunk)
                            return
                    return self.send(200,json.dumps(result,ensure_ascii=False).encode())
                if self.path=='/model/profile':
                    if not cfg.get('allow_model_profiles',False):return self.send(404,b'{"error":"profile control disabled"}')
                    data=json.loads(raw)
                    if not isinstance(data,dict) or set(data)!={'profile'} or data['profile'] not in ('text','vision'):
                        return self.send(400,b'{"error":"invalid profile"}')
                    from llm_sidecar import ensure_profile
                    return self.send(200,json.dumps(ensure_profile(data['profile'])).encode())
                if self.path=='/v1/chat/completions':
                    data=json.loads(raw)
                    if data.get('stream'):return self.send(400,b'{"error":"non-streaming requests only"}')
                    lease=Lease(DATA/'model.lock');lease.__enter__()
                req=urllib.request.Request(cfg['upstream']+self.path,data=raw,headers={'Content-Type':'application/json'},method=self.command)
                with urllib.request.urlopen(req,timeout=cfg.get('timeout',300)) as r:
                    response=r.read(16*1024**2+1)
                    if len(response)>16*1024**2:return self.send(502,b'{"error":"response too large"}')
                    return self.send(r.status,response)
            except BusyError:return self.send(409,b'{"error":"model busy"}')
            except urllib.error.HTTPError as e:return self.send(e.code,b'{"error":"model request failed"}')
            except Exception:return self.send(502,b'{"error":"model unavailable"}')
            finally:
                if lease:lease.__exit__()
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
    server=ThreadingHTTPServer((cfg['host'],cfg['port']),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cfg['certificate'],cfg['private_key']);server.socket=context.wrap_socket(server.socket,server_side=True)
    print('LLM gateway ready on '+cfg['host']+':'+str(cfg['port']),flush=True);server.serve_forever()

if __name__=='__main__':
    import sys
    serve(Path(sys.argv[1]))
