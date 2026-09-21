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
    allowed_post={'/apply-template','/tokenize','/v1/chat/completions'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,raw):
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
