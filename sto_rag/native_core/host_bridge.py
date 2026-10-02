"""Small Windows-only boundary: physical RAM, local model and exact Word export."""
import hashlib,hmac,ipaddress,json,ssl,subprocess,threading,urllib.request,urllib.error,base64
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from .transport import secret

def serve(config_path):
    cfg=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    token=secret(cfg['token_file']);allowed=[ipaddress.ip_network(n) for n in cfg['allowed_networks']]
    root=Path(cfg['word_data']).resolve();root.mkdir(parents=True,exist_ok=True)
    word_lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def reply(self,status,value):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            if status>=400:self.close_connection=True
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
        def dispatch(self):
            if not any(ipaddress.ip_address(self.client_address[0]) in n for n in allowed):return self.reply(403,{'error':'client not allowed'})
            if not hmac.compare_digest(self.headers.get('Authorization','').removeprefix('Bearer '),token):return self.reply(401,{'error':'authentication required'})
            routes={'/health','/props','/v1/models','/host/resources'} if self.command=='GET' else {'/apply-template','/tokenize','/v1/chat/completions','/model/profile','/word/docx','/word/pdf','/word/read','/word/anchors','/word/review','/host/file'}
            if self.path not in routes:return self.reply(404,{'error':'route not available'})
            try:
                limit=80*1024**2 if self.path in ('/word/review','/word/anchors') else 50*1024**2 if self.path.startswith('/word/') else 8*1024**2
                size=int(self.headers.get('Content-Length','0'))
                if not 0<=size<=limit:return self.reply(413,{'error':'request too large'})
                raw=self.rfile.read(size) if self.command=='POST' else None
                if self.path=='/host/resources':
                    from pipeline import memory_mb
                    total,available=memory_mb()
                    return self.reply(200,{'total_mb':total,'available_mb':available,'reserve_mb':2048})
                if self.path=='/model/profile':
                    value=json.loads(raw)
                    if set(value)!={'profile'} or value['profile'] not in ('text','vision'):raise ValueError('Profile')
                    from llm_sidecar import ensure_profile
                    return self.reply(200,ensure_profile(value['profile']))
                if self.path=='/host/file':
                    value=json.loads(raw);source=Path(value['path']).resolve(strict=True)
                    authorized=[Path(p).resolve() for p in cfg.get('import_files',[])]
                    if source not in authorized or source.suffix.lower() not in ('.doc','.docx'):raise ValueError('Import path')
                    if source.stat().st_size>50*1024**2:raise ValueError('Import size')
                    body=source.read_bytes();digest=hashlib.sha256(body).hexdigest()
                    self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(len(body)));self.send_header('X-Artifact-SHA256',digest);self.end_headers();self.wfile.write(body);return
                if self.path.startswith('/word/'):
                    if self.path in ('/word/read','/word/anchors','/word/review'):
                        from .word_service import operation
                        with word_lock:
                            target,digest,report=operation(self.path,raw,self.headers,root)
                            self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(target.stat().st_size));self.send_header('X-Source-SHA256',digest);self.send_header('X-Artifact-SHA256',hashlib.sha256(target.read_bytes()).hexdigest())
                            if report:self.send_header('X-Word-Report',base64.b64encode(json.dumps(report).encode()).decode())
                            self.end_headers()
                            with target.open('rb') as file:
                                while chunk:=file.read(65536):self.wfile.write(chunk)
                        return
                    suffix=self.headers.get('X-Source-Format');digest=hashlib.sha256(raw).hexdigest()
                    if suffix not in ('doc','docx') or digest!=self.headers.get('X-Source-SHA256'):raise ValueError('Source identity')
                    fmt=self.path.rsplit('/',1)[-1];folder=root/digest;folder.mkdir(exist_ok=True)
                    source=folder/('source.'+suffix);target=folder/('export.'+fmt)
                    with word_lock:
                        if not source.exists():source.write_bytes(raw)
                        if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('Stored source identity')
                        from nc5.conversion import inspect_legacy
                        from nc5.documents import inspect_file
                        if suffix=='doc':inspect_legacy(source)
                        else:inspect_file(source)
                        if not target.exists():
                            subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',cfg['word_script'],'-Source',str(source),'-Destination',str(target),'-Format',fmt],timeout=210,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                        if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('Word changed source')
                        if fmt=='docx':inspect_file(target)
                        elif target.read_bytes()[:5]!=b'%PDF-':raise ValueError('PDF signature')
                        if target.stat().st_size>64*1024**2:raise ValueError('Output size')
                        checksum=hashlib.sha256(target.read_bytes()).hexdigest()
                        self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(target.stat().st_size));self.send_header('X-Source-SHA256',digest);self.send_header('X-Artifact-SHA256',checksum);self.end_headers()
                        with target.open('rb') as file:
                            while chunk:=file.read(65536):self.wfile.write(chunk)
                    return
                if self.path=='/v1/chat/completions' and json.loads(raw).get('stream'):return self.reply(400,{'error':'non-streaming requests only'})
                request=urllib.request.Request('http://127.0.0.1:8082'+self.path,data=raw,headers={'Content-Type':'application/json'},method=self.command)
                with urllib.request.urlopen(request,timeout=360) as response:
                    body=response.read(24*1024**2+1)
                    if len(body)>24*1024**2:raise ValueError('Response size')
                    self.reply(response.status,body)
            except urllib.error.HTTPError as error:self.reply(error.code,{'error':'model request failed'})
            except Exception as error:
                print('Host bridge request failed: '+type(error).__name__,flush=True)
                self.reply(503,{'error':'Windows service unavailable'})
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
    server=ThreadingHTTPServer((cfg['host'],cfg.get('port',8123)),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cfg['certificate'],cfg['private_key']);server.socket=context.wrap_socket(server.socket,server_side=True)
    print('Windows host bridge ready',flush=True);server.serve_forever()

if __name__=='__main__':
    import sys
    serve(sys.argv[1])
