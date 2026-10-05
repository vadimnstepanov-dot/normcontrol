import os,sys,json,urllib.request,time
from pathlib import Path
from .transport import Transport,secret

def check(mode):
    if mode=='gateway':
        cfg=json.loads(Path('/run/secrets/gateway.json').read_text())
        # Probe the container itself. Windows forwarding is an external dependency
        # and may start later than Docker after a reboot. TLS still verifies the
        # certificate's existing 127.0.0.1 subject alternative name.
        transport=Transport('https://127.0.0.1:8098',cfg['token'],cfg['certificate'])
        assert transport.json('/core/health',timeout=4)['ready']
    elif mode=='api':
        with urllib.request.urlopen('http://127.0.0.1:8096/health',timeout=4) as response:assert json.load(response)['ready']
    elif mode=='worker':
        # Bridge lease must belong to a live local process, including idle polling.
        from .runtime import initialize
        initialize()
        from nc5.runtime import Lease,BusyError
        try:
            with Lease(Path(os.environ['NORMCONTROL_NATIVE_DATA'])/'bridge.lock'):raise RuntimeError('Bridge worker not running')
        except BusyError:pass
    else:raise ValueError('Health target')

if __name__=='__main__':check(sys.argv[1])
