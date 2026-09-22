"""Authenticated, restartable bridge. Server IDs never become arbitrary local paths."""
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from .common import DATA,dumps,write,read,digest,config
from .engine import Engine
from .report import build
from .runtime import Lease
from . import feedback

# The portal owns connection limits, while the desktop worker owns document
# planning.  Reload the latter before every claim so a long-lived worker cannot
# silently keep obsolete chunk/group sizes after a local tuning change.
REMOTE_CONFIG_KEYS={'model','context','output','timeout'}

def rag_summary(cfg=None):
    """Public, non-secret description of the currently loaded normative RAG."""
    from .catalog import load_catalog
    cat=load_catalog();cfg=cfg or config()
    return {'catalog':cat['version'],'requirements':len(cat.get('cards',[])),
        'contract_version':cat.get('normative_contract_version'),
        'unresolved_dependencies':sum(bool(c.get('unresolved_dependencies')) for c in cat.get('cards',[])),
        'settings':{'requirements_per_group':cfg.get('sto_group_size'),
            'evidence_chars_per_group':cfg.get('sto_group_chars'),
            'reference_group_size':cfg.get('reference_group_size'),
            'verification_group_size':cfg.get('verification_group_size'),
            'search_normalization':'Русская морфология и точные цитаты'},
        'sources':[{'name':s.get('document') or s.get('standard') or s.get('file','Источник'),
            'sha256':s.get('sha256','')[:12],'blocks':s.get('blocks',0),'tables':s.get('tables',0),
            'warnings':len(s.get('warnings',[]))} for s in cat.get('sources',[])]}

def runtime_config(remote):
    local=config()
    local.update({k:v for k,v in remote.items() if k in REMOTE_CONFIG_KEYS})
    if os.getenv('NORMCONTROL_REMOTE_LLM_CONFIG')=='1' and 'endpoint' in remote:
        local['endpoint']=remote['endpoint']
    return local

def run(url):
    with Lease(DATA/'bridge.lock'):_run(url)

def _run(url):
    u=urlparse(url)
    if u.scheme!='https' and u.hostname not in ('localhost','127.0.0.1'):raise ValueError('Для сайта требуется HTTPS')
    token=os.environ.get('NORMCONTROL_WORKER_TOKEN','')
    if len(token)<32:raise ValueError('Нужен NORMCONTROL_WORKER_TOKEN длиной не менее 32 символов')
    url=url.rstrip('/');name=os.environ.get('NORMCONTROL_WORKER_NAME',os.environ.get('COMPUTERNAME','local'))
    def request(path,data=None,raw=False):
        req=urllib.request.Request(url+path,data=dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=45) as r:
            if raw:
                content=r.read(50*1024**2+1)
                if len(content)>50*1024**2:raise ValueError('Размер файла превышает лимит')
                return content
            return json.load(r)
    def review_feedback(items):
        for f in items:
            key=feedback.submit(engine,f['local_id'],f['finding_id'],f['comment'],key=digest([url,f['lease'],f['id']]))
            decision=feedback.review(engine,key)
            request(f'/worker/{f["lease"]}/feedback/{f["id"]}/',{'state':decision['decision'],'decision':decision})
    engine=Engine();state_path=DATA/'bridge-state.json';state=read(state_path) if state_path.exists() else None;started=None;last_probe=0.0;model_ok=False;rag=rag_summary(engine.config)
    while True:
        try:
            active=any(j['state'] in ('running','preparing') for j in engine.store.jobs())
            if time.time()-last_probe>=15:
                engine.client.probe();model_ok=True;last_probe=time.time()
            request('/worker/ping/',{'worker':name,'state':('busy' if active else 'idle') if model_ok else 'error','rag':rag})
            if state is None:
                if active:time.sleep(5);continue
                settings=request('/worker/config/')
                key=settings.pop('api_key','')
                if os.getenv('NORMCONTROL_REMOTE_LLM_CONFIG')=='1':os.environ['NORMCONTROL_LLM_API_KEY']=key
                engine.config=runtime_config(settings)
                from .model import Client
                engine.client=Client(engine.config);engine.client.probe()
                review_feedback(request('/worker/feedback/',{'worker':name})['feedback'])
                claim=request('/worker/claim/',{'worker':name})
                if not claim['job']:time.sleep(5);continue
                state={'claim':claim,'lease':claim['lease'],'sequence':0};write(state_path,state)
            if not state.get('job'):
                claim=state['claim'];paths=[];folder=DATA/'remote'/str(claim['job'])
                if not re.fullmatch(r'[a-fA-F0-9-]{32,36}',str(claim['job'])):raise ValueError('Неверный идентификатор пакета')
                folder.mkdir(parents=True,exist_ok=True)
                for f in claim['files']:
                    if not isinstance(f['id'],int):raise ValueError('Недопустимый идентификатор документа')
                    raw=request(f'/worker/{claim["lease"]}/files/{f["id"]}/',raw=True)
                    if hashlib.sha256(raw).hexdigest()!=f['sha256']:raise ValueError('Контрольная сумма загруженного документа не совпала')
                    filename=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',f['name'])[:180]
                    if Path(filename).suffix.casefold() not in ('.doc','.docx'):raise ValueError('Нужен документ Word .doc или .docx')
                    path=folder/(str(f['id'])+'-'+filename);path.write_bytes(raw);paths.append(str(path))
                jid=next((j['id'] for j in engine.store.jobs() if engine.store.job(j['id'])['data'].get('remote_lease')==claim['lease']),None)
                if jid is None:
                    options={f'check_{k}':k in claim['checks'] for k in ('language','logic','sto')}
                    options['reuse_cache']=not claim.get('fresh_review',False)
                    options['formatting']='xml' if 'formatting' in claim['checks'] else 'off'
                    jid=engine.create(paths,options,owner='remote:'+claim['owner']);d=engine.store.job(jid)['data'];d['remote_lease']=claim['lease'];engine.store.update(jid,data=d)
                state['job']=jid;write(state_path,state)
            jid=state['job'];status=engine.status(jid)
            if (started!=jid or not engine.thread or not engine.thread.is_alive()) and status['state'] in ('preparing','running'):
                engine.start(jid);started=jid
            state['sequence']+=1;write(state_path,state)
            payload={'sequence':state['sequence'],'local_id':jid,'state':status['state'],'snapshot':status}
            if state.get('last_report_version')!=status['updated']:
                from .report import markdown
                report=build(engine,jid);report['markdown_export']=markdown(report);payload['report']=report
            response=request(f'/worker/{state["lease"]}/update/',payload)
            if response.get('wake') and status['state'] in ('preparing','running'):
                from .runtime import wake_once
                wake_once()
            if 'report' in payload:state['last_report_version']=status['updated'];write(state_path,state)
            if response.get('cancel') and status['state'] in ('running','preparing','paused'):engine.store.update(jid,'cancelled')
            elif response.get('control')=='pause' and status['state'] in ('running','preparing'):engine.store.update(jid,'paused')
            elif response.get('control')=='resume' and status['state']=='paused' and not engine.lock.locked():engine.start(jid)
            if status['state'] in ('completed','partial','failed','cancelled') and not engine.lock.locked():
                review_feedback([{**f,'local_id':jid,'lease':state['lease']} for f in response.get('feedback',[])])
                state=None;started=None;write(state_path,None)
            time.sleep(5)
        except KeyboardInterrupt:return
        except Exception as e:
            model_ok=False;last_probe=0.0
            try:request('/worker/ping/',{'worker':name,'state':'error'})
            except Exception:pass
            print('Worker connection/task error: '+type(e).__name__,flush=True);time.sleep(10)
