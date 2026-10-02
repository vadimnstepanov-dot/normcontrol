"""Adapt platform services before importing nc5; the rule implementation stays byte-identical."""
import argparse,json,os,signal,sys,types,uuid,socket
from pathlib import Path
from .transport import Transport,secret

def initialize():
    from nc5 import common
    common.DATA=Path(os.environ.get('NORMCONTROL_NATIVE_DATA','/data/nc5'))
    common.ROOT=Path(os.environ.get('NORMCONTROL_NATIVE_ROOT','/app'))
    common.DATA.mkdir(parents=True,exist_ok=True)
    return common

def configure_adapters():
    common=initialize()
    from .queue_client import RemoteCoordinator
    from .resources import HostProbe,container_available,LINUX_RESERVE_MB
    from .word_client import install
    import pipeline
    certificate=os.environ['NORMCONTROL_LLM_CA_FILE']
    queue=Transport(os.environ['NORMCONTROL_QUEUE_ENDPOINT'],secret(os.environ['NORMCONTROL_QUEUE_TOKEN_FILE']),certificate)
    bridge=Transport(os.environ['NORMCONTROL_HOST_ENDPOINT'],secret(os.environ['NORMCONTROL_HOST_TOKEN_FILE']),certificate)
    remote=RemoteCoordinator(queue);probe=HostProbe(bridge)
    pipeline.host=lambda:remote
    pipeline.memory_mb=probe.memory
    original_budget=pipeline.cache_budget
    def cache_budget(requested):
        _,free=container_available()
        return min(original_budget(requested),max(0,free-LINUX_RESERVE_MB)*1048576//2)
    pipeline.cache_budget=cache_budget
    install(bridge)
    from .file_client import Importer
    importer=Importer(bridge,common.DATA/'local-imports')
    model_adapter=types.ModuleType('llm_sidecar')
    model_adapter.ensure_profile=lambda profile:bridge.json('/model/profile',{'profile':profile},timeout=240)
    sys.modules['llm_sidecar']=model_adapter
    os.environ['NORMCONTROL_LLM_API_KEY']=bridge.token
    common.core_importer=importer
    from .output_guard import install as install_output_guard
    install_output_guard()
    return common

def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['worker','api']);args=parser.parse_args()
    common=configure_adapters()
    from nc5.engine import Engine
    from nc5.store import Store
    from .checkpoint import pause,resume
    instance=uuid.uuid4().hex
    store=Store()
    with store.connect() as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS core_owners(job TEXT PRIMARY KEY,instance TEXT,container TEXT,pid INTEGER)')
    original_heartbeat=Store.heartbeat
    def heartbeat(store,job,cache_bytes=0):
        original_heartbeat(store,job,cache_bytes)
        with store.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO core_owners VALUES(?,?,?,?)',(job,instance,socket.gethostname(),os.getpid()))
    Store.heartbeat=heartbeat
    state=common.DATA/('core-shutdown-'+args.mode+'.json')
    restored=[]
    if state.exists():
        marker=json.loads(state.read_text());restored=resume(marker['token'],state)
    engines=[]
    class PlatformEngine(Engine):
        def __init__(self,*a,**kw):super().__init__(*a,**kw);engines.append(self)
        def create(self,paths,options=None,owner='local'):
            options=dict(options or {})
            imported={str(path):str(common.core_importer.path(path)) for path in paths}
            paths=list(imported.values())
            if options.get('document_roles'):options['document_roles']={imported.get(key,key):value for key,value in options['document_roles'].items()}
            if not options.get('pipeline_id'):
                options['pipeline_id']=str(uuid.uuid4())
                options['pipeline_directions']=[name for name in ('language','logic','sto') if options.get('check_'+name,self.config.get('check_'+name,True))]
            return super().create(paths,options,owner)
    def shutdown(*unused):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        store=Store()
        with store.connect() as connection:
            owned=[row[0] for row in connection.execute("SELECT jobs.id FROM jobs JOIN core_owners ON core_owners.job=jobs.id WHERE jobs.state IN ('running','preparing') AND core_owners.instance=?",(instance,))]
        if owned:pause('shutdown',state,owned)
        for engine in engines:
            if engine.thread and engine.thread.is_alive():engine.thread.join(330)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    if args.mode=='worker':
        envfile=Path(os.environ['NORMCONTROL_WORKER_ENV_FILE'])
        for line in envfile.read_text(encoding='utf-8-sig').splitlines():
            key,sep,value=line.partition('=')
            if sep and key in ('NORMCONTROL_WORKER_TOKEN','NORMCONTROL_WORKER_NAME','NORMCONTROL_PORTAL_URL'):os.environ[key]=value
        from nc5 import worker
        worker.Engine=PlatformEngine
        worker.run(os.environ['NORMCONTROL_PORTAL_URL'])
    else:
        from nc5 import service
        service.Path=common.core_importer.path
        original_server=service.ThreadingHTTPServer
        service.ThreadingHTTPServer=lambda address,handler:original_server(('0.0.0.0',address[1]),handler)
        engine=PlatformEngine()
        if restored:engine.start(restored[0])
        service.serve(engine)

if __name__=='__main__':main()
