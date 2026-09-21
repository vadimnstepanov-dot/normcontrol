import json
import secrets
import threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from .common import DATA,dumps,write,config
from .report import build,html_report,markdown
from . import feedback

def serve(engine):
    token=secrets.token_urlsafe(32);port=engine.config['port']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,value,kind='application/json; charset=utf-8'):
            raw=(dumps(value) if kind.startswith('application/json') else value).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(raw)
        def allowed(self,mutate=False):
            host=self.headers.get('Host','')
            if host not in (f'127.0.0.1:{port}',f'localhost:{port}'):
                self.send(403,{'error':'Недопустимый Host'});return False
            if mutate and (self.headers.get('X-Review-Token')!=token or self.headers.get('Origin',f'http://{host}')!=f'http://{host}'):
                self.send(403,{'error':'Нужен локальный сеанс'});return False
            return True
        def do_GET(self):
            if not self.allowed():return
            path=urlparse(self.path).path
            try:
                if path=='/':
                    text=Path(__file__).with_name('ui.html').read_text(encoding='utf-8').replace('__TOKEN__',token)
                    return self.send(200,text,'text/html; charset=utf-8')
                if path=='/health':return self.send(200,{'version':5,'ready':True})
                if path=='/api/jobs':return self.send(200,engine.store.jobs())
                if path=='/api/settings':return self.send(200,engine.config)
                if path=='/api/catalog':
                    from .catalog import load_catalog
                    c=load_catalog();return self.send(200,{k:c[k] for k in ('version','sources','profiles','coverage','limitations')})
                if path=='/api/catalog/cards':
                    from .catalog import load_catalog
                    return self.send(200,load_catalog()['cards'])
                if path=='/api/catalog/validation':
                    from .catalog import load_catalog
                    from .common import read
                    c=load_catalog();return self.send(200,read(DATA/'catalogs'/c['version']/'validation.json'))
                if path=='/api/catalog/matrix':
                    from .catalog import load_catalog
                    from .common import read
                    c=load_catalog();return self.send(200,read(DATA/'catalogs'/c['version']/'profile-matrix.json'))
                pieces=path.strip('/').split('/')
                if len(pieces)>=3 and pieces[:2]==['api','jobs']:
                    jid=pieces[2]
                    if len(pieces)==3:return self.send(200,engine.status(jid))
                    if pieces[3]=='findings':return self.send(200,engine.store.findings(jid))
                    if pieces[3]=='dispositions':return self.send(200,engine.store.dispositions(jid))
                    if pieces[3]=='feedback':return self.send(200,feedback.list_feedback(engine,jid))
                    if pieces[3]=='tasks':return self.send(200,[{k:t[k] for k in ('id','stage','state','attempts','started','ended','error')} for t in engine.store.tasks(jid)])
                if len(pieces)==3 and pieces[0]=='report':
                    report=build(engine,pieces[1]);fmt=pieces[2]
                    if fmt=='html':return self.send(200,html_report(report),'text/html; charset=utf-8')
                    if fmt=='md':return self.send(200,markdown(report),'text/markdown; charset=utf-8')
                    if fmt=='json':return self.send(200,report)
                return self.send(404,{'error':'Не найдено'})
            except (ValueError,KeyError,FileNotFoundError) as e:return self.send(400,{'error':str(e)})
        def do_POST(self):
            if not self.allowed(True):return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if n>100000:raise ValueError('Запрос слишком большой')
                body=json.loads(self.rfile.read(n));path=urlparse(self.path).path
                if path=='/api/probe':return self.send(200,engine.client.probe())
                if path=='/api/wake':
                    from .runtime import wake_once
                    active=any(j['state'] in ('running','preparing') for j in engine.store.jobs())
                    return self.send(200,{'active':active,'signalled':bool(active and wake_once())})
                if path=='/api/settings':
                    if any(j['state'] in ('running','preparing') for j in engine.store.jobs()):raise ValueError('Настройки можно изменить между проверками')
                    allowed={'endpoint','model','context','output','margin','timeout','retries','ram_bytes','formatting'}
                    if set(body)-allowed:raise ValueError('Неизвестные настройки')
                    c={**engine.config,**body}
                    if not 4096<=int(c['context'])<=131072 or not 512<=int(c['output'])<=8192 or int(c['output'])+int(c['margin'])>=int(c['context']):raise ValueError('Недопустимый бюджет контекста')
                    if int(c['ram_bytes'])>8*1024**3 or int(c['ram_bytes'])<0:raise ValueError('RAM-кэш: до 8 ГиБ')
                    if not 30<=int(c['timeout'])<=600 or not 0<=int(c['retries'])<=2 or not 512<=int(c['margin'])<=8192:raise ValueError('Ожидание: 30–600 с; повторы: 0–2; резерв: 512–8192 токена')
                    if c['formatting'] not in ('off','xml','render'):raise ValueError('Неизвестный режим оформления')
                    url=urlparse(c['endpoint'])
                    if url.hostname not in ('127.0.0.1','localhost') or url.scheme!='http':raise ValueError('Только локальный endpoint; облачная передача отключена')
                    write(DATA/'config.json',c);engine.config=c
                    from .model import Client
                    from .store import LRU
                    engine.client=Client(c);engine.cache=LRU(c['ram_bytes']);return self.send(200,{'saved':True})
                if path=='/api/jobs':
                    if any(j['state'] in ('running','preparing') for j in engine.store.jobs()):raise ValueError('Дождитесь завершения или приостановите текущую проверку')
                    options=body.get('options',{})
                    if set(options)-{'check_language','check_logic','check_sto','formatting'}:raise ValueError('Неизвестные опции')
                    jid=engine.create(body['paths'],options);engine.start(jid);return self.send(201,{'id':jid})
                if path=='/api/jobs/prepare':
                    if any(j['state'] in ('running','preparing') for j in engine.store.jobs()):raise ValueError('Дождитесь завершения или приостановите текущую проверку')
                    options=body.get('options',{})
                    if set(options)-{'check_language','check_logic','check_sto','formatting'}:raise ValueError('Неизвестные опции')
                    paths=[Path(value).resolve() for value in body.get('paths',[])]
                    if not 1<=len(paths)<=20:raise ValueError('Нужен комплект из 1–20 документов')
                    documents=[]
                    for value in paths:
                        if not value.is_file() or value.suffix.casefold() not in ('.doc','.docx'):raise ValueError('Файл Word .doc или .docx не найден: '+str(value))
                        name=value.name;lower=name.casefold();kind='Частное техническое задание' if 'чтз' in lower else 'Описание информационной технологии' if 'оит' in lower else 'Техническое задание' if 'тз' in lower else 'Технический документ — тип уточнится по структуре'
                        documents.append({'path':str(value),'name':name,'type':kind,'size':value.stat().st_size})
                    return self.send(200,{'documents':documents,'options':options,'interdocument':len(documents)>1})
                p=path.strip('/').split('/')
                if len(p)==4 and p[:2]==['api','jobs']:
                    jid,action=p[2:]
                    if action in ('pause','cancel'):
                        engine.store.update(jid,'paused' if action=='pause' else 'cancelled');return self.send(200,engine.status(jid))
                    if action=='resume':
                        if any(j['state'] in ('running','preparing') for j in engine.store.jobs()):raise ValueError('Модель занята')
                        engine.start(jid);return self.send(200,{'resumed':True})
                    if action=='start':
                        if any(j['state'] in ('running','preparing') for j in engine.store.jobs()):raise ValueError('Модель занята')
                        engine.start(jid);return self.send(200,{'started':True})
                    if action=='feedback':return self.send(201,{'id':feedback.submit(engine,jid,body['finding_id'],body['comment'])})
                    if action=='disposition':
                        if body.get('finding_id') not in {f['id'] for f in engine.store.findings(jid)}:raise ValueError('Замечание не найдено')
                        return self.send(200,engine.store.set_disposition(jid,body['finding_id'],body.get('state','new'),body.get('comment','')))
                if len(p)==4 and p[:2]==['api','feedback']:
                    if p[3]=='review':
                        if any(j['state'] in ('running','preparing') for j in engine.store.jobs()):raise ValueError('Перепроверка обратной связи доступна после текущего прохода')
                        return self.send(200,feedback.review(engine,p[2]))
                    if p[3]=='deactivate':feedback.deactivate(engine,p[2]);return self.send(200,{'active':False})
                return self.send(404,{'error':'Не найдено'})
            except Exception as e:return self.send(400,{'error':str(e)})
    print(dumps({'url':f'http://127.0.0.1:{port}/'}),flush=True)
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
