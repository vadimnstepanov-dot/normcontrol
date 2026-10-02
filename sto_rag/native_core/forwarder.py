"""Byte-only Windows port forwarding; TLS and queue ownership stay in Linux."""
import ipaddress,json,select,socket,socketserver,subprocess,sys,threading,time
from pathlib import Path

class Address:
    def __init__(self,distro):self.distro=distro;self.value=None;self.at=0;self.lock=threading.Lock()
    def get(self,refresh=False):
        with self.lock:
            if refresh or self.value is None or time.monotonic()-self.at>30:
                raw=subprocess.check_output(['wsl.exe','-d',self.distro,'--','hostname','-I'],text=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                values=[v for v in raw.split() if ipaddress.ip_address(v).version==4 and ipaddress.ip_address(v).is_private]
                if not values:raise RuntimeError('WSL address unavailable')
                self.value=values[0];self.at=time.monotonic()
            return self.value

def serve(path):
    cfg=json.loads(Path(path).read_text(encoding='utf-8-sig'));address=Address(cfg['distro'])
    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                allowed=self.server.allowed_clients
                if self.client_address[0] not in allowed and self.client_address[0]!=address.get():return
                for attempt in range(2):
                    try:upstream=socket.create_connection((address.get(bool(attempt)),self.server.target_port),timeout=10);break
                    except OSError:
                        if attempt:raise
                with upstream:
                    pair=(self.request,upstream)
                    for sock in pair:sock.settimeout(10)
                    while True:
                        ready,_,_=select.select(pair,[],[],420)
                        if not ready:return
                        for source in ready:
                            chunk=source.recv(65536)
                            if not chunk:return
                            (upstream if source is self.request else self.request).sendall(chunk)
            except (OSError,subprocess.SubprocessError):pass
    class Server(socketserver.ThreadingTCPServer):allow_reuse_address=True;daemon_threads=True
    servers=[]
    for item in cfg['ports']:
        server=Server((item['host'],item['port']),Relay);server.target_port=item['target_port'];server.allowed_clients=item['allowed_clients'];servers.append(server)
        threading.Thread(target=server.serve_forever,daemon=True).start()
    print('Windows byte forwarding ready',flush=True)
    threading.Event().wait()

if __name__=='__main__':serve(sys.argv[1])
