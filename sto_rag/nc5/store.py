"""SQLite transactions are the source of truth; the LRU contains only reproducible data."""
import json
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from .common import dumps, digest, DATA

class ClosingConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()

class LRU:
    def __init__(self, limit):
        self.limit = limit; self.size = 0; self.items = OrderedDict(); self.lock = threading.RLock()
    def get(self, key):
        with self.lock:
            if key not in self.items: return None
            value = self.items.pop(key); self.items[key] = value
            return json.loads(value)
    def put(self, key, value):
        raw = dumps(value).encode()
        with self.lock:
            self.size -= len(self.items.pop(key, b''))
            if len(raw) > self.limit: return
            self.items[key] = raw; self.size += len(raw)
            while self.size > self.limit:
                _, old = self.items.popitem(last=False); self.size -= len(old)

class Store:
    def __init__(self, path=None):
        self.path = str(path or DATA/'review.sqlite3'); __import__('pathlib').Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, state TEXT, created REAL, updated REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, job TEXT, stage TEXT, state TEXT, attempts INTEGER DEFAULT 0,
              started REAL, ended REAL, payload TEXT, result TEXT, error TEXT, cache_key TEXT);
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(job,stage,state);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, at REAL, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, job TEXT, status TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, created REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, job TEXT, state TEXT, created REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS dispositions(job TEXT, finding TEXT, state TEXT, updated REAL, data TEXT, PRIMARY KEY(job,finding));
            CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY, active INTEGER, created REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS runtime(job TEXT PRIMARY KEY, at REAL, pid INTEGER, cache_bytes INTEGER, rss_bytes INTEGER);
            ''')
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection); c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA synchronous=FULL'); return c
    def event(self, job, kind, data, conn=None):
        if conn is not None: conn.execute('INSERT INTO events(job,at,kind,data) VALUES(?,?,?,?)',(job,time.time(),kind,dumps(data)))
        else:
            with self.connect() as c: self.event(job,kind,data,c)
    def create(self, data):
        jid = uuid.uuid4().hex; now=time.time()
        with self.connect() as c:
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',(jid,'preparing',now,now,dumps(data)))
            self.event(jid,'created',{},c)
        return jid
    def job(self, jid):
        with self.connect() as c: row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not row: raise KeyError(jid)
        out=dict(row); out['data']=json.loads(out['data']); return out
    def jobs(self):
        with self.connect() as c: rows=c.execute('SELECT id,state,created,updated,data FROM jobs ORDER BY created DESC').fetchall()
        result=[]
        for row in rows:
            item=dict(row);data=json.loads(item.pop('data'));docs=data.get('documents',[])
            item['documents']=[{'name':d.get('name',''),'type':d.get('profile',{}).get('type','')} for d in docs]
            result.append(item)
        return result
    def update(self, jid, state=None, data=None):
        with self.connect() as c:
            if state: c.execute('UPDATE jobs SET state=?,updated=? WHERE id=?',(state,time.time(),jid))
            if data is not None: c.execute('UPDATE jobs SET data=?,updated=? WHERE id=?',(dumps(data),time.time(),jid))
            self.event(jid,'state',{'state':state},c)
    def add(self,jid,stage,payload,key=None):
        tid=digest([jid,stage,payload]); key=key or digest([stage,payload])
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO tasks(id,job,stage,state,payload,cache_key) VALUES(?,?,?,?,?,?)',(tid,jid,stage,'pending',dumps(payload),key))
        return tid
    def tasks(self,jid,stage=None):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM tasks WHERE job=?'+(' AND stage=?' if stage else '')+' ORDER BY rowid',(jid,stage) if stage else (jid,)).fetchall()
        out=[]
        for row in rows:
            r=dict(row);r['payload']=json.loads(r['payload']);r['result']=json.loads(r['result']) if r['result'] else None;out.append(r)
        return out
    def claim(self,jid,stage):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            j=c.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()
            if not j or j[0]!='running': return None
            row=c.execute("SELECT * FROM tasks WHERE job=? AND stage=? AND state='pending' ORDER BY rowid LIMIT 1",(jid,stage)).fetchone()
            if not row: return None
            c.execute("UPDATE tasks SET state='running',started=?,attempts=attempts+1 WHERE id=?",(time.time(),row['id']))
            self.event(jid,'task_started',{'task':row['id'],'stage':stage},c)
            r=dict(row); r['payload']=json.loads(r['payload']); return r
    def finish(self,task,result=None,error=None,state=None):
        state=state or ('failed' if error else 'done')
        with self.connect() as c:
            row=c.execute('SELECT state FROM tasks WHERE id=?',(task['id'],)).fetchone()
            if not row or row[0] != 'running': return False
            c.execute('UPDATE tasks SET state=?,ended=?,result=?,error=? WHERE id=?',(state,time.time(),dumps(result) if result is not None else None,error,task['id']))
            self.event(task['job'],'task_finished',{'task':task['id'],'state':state,'stage':task['stage']},c)
            c.execute('UPDATE jobs SET updated=? WHERE id=?',(time.time(),task['job']))
        return True
    def finding(self,jid,value,status='candidate'):
        # Identical arithmetic operands or identical proposed edits at identical
        # source locations represent one defect even when stages phrase its title differently.
        identity='arithmetic' if value.get('category')=='арифметика' else ' '.join(value.get('suggestion',value.get('issue','')).casefold().split())
        category=value.get('category');evidence=sorted((e.get('document'),e.get('locator'),e.get('quote')) for e in value['evidence'])
        # Only the same numbered-reference defect at the same pair of locations.
        # Other defects in the same paragraph must remain separate.
        title=value.get('issue','')
        if not value.get('requirement_id') and re.search(r'кирилл|смешан[а-я]*\s+алфавит',title+' '+value.get('explanation',''),re.I):
            mixed=[(e.get('document'),e.get('locator'),w) for e in value['evidence'] for w in re.findall(r'\b[\w]+\b',e['quote']) if re.search('[A-Za-z]',w) and re.search('[А-Яа-я]',w)]
            if len(set(mixed))==1:identity=['mixed_alphabet',mixed[0][2]];category='identifier';evidence=sorted({(e[0],e[1]) for e in mixed})
        if not value.get('requirement_id') and len(evidence)>=2 and re.search(r'таблиц|рисунк|приложени',title,re.I) and re.search(r'номер|нумерац|ссылк',title,re.I):
            numbers=sorted(set(re.findall(r'\d+(?:\.\d+)*',' '.join(e[2] for e in evidence))))
            if len(numbers)>=2:
                identity=['numbered_reference',numbers];category='reference';evidence=sorted({(e[0],e[1]) for e in evidence})
        fid=digest([jid,category,evidence,identity])[:24]
        value={**value,'id':fid}
        with self.connect() as c: c.execute('INSERT OR IGNORE INTO findings VALUES(?,?,?,?)',(fid,jid,status,dumps(value)))
        return fid
    def findings(self,jid):
        with self.connect() as c: return [{**json.loads(r['data']),'status':r['status']} for r in c.execute('SELECT * FROM findings WHERE job=? ORDER BY rowid',(jid,))]
    def set_disposition(self,jid,fid,state,comment=''):
        if state not in ('new','in_work','fixed','disputed'):raise ValueError('Неизвестный статус решения')
        if state=='disputed' and len(comment.strip())<8:raise ValueError('Для несогласия укажите обоснование')
        value={'state':state,'comment':comment.strip(),'author':'Локальный пользователь','updated':time.time()}
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO dispositions VALUES(?,?,?,?,?)',(jid,fid,state,value['updated'],dumps(value)))
        return value
    def dispositions(self,jid):
        with self.connect() as c:return {r['finding']:json.loads(r['data']) for r in c.execute('SELECT finding,data FROM dispositions WHERE job=?',(jid,))}
    def verdict(self,fid,status,reason,verified_explanation=False,suggestion=None):
        with self.connect() as c:
            row=c.execute('SELECT data FROM findings WHERE id=?',(fid,)).fetchone()
            if row:
                d=json.loads(row[0]);d['verification']=reason
                if verified_explanation and status=='confirmed':d.setdefault('initial_explanation',d['explanation']);d['explanation']=reason
                if suggestion is not None and status=='confirmed':d.setdefault('initial_suggestion',d.get('suggestion',''));d['suggestion']=suggestion
                c.execute('UPDATE findings SET status=?,data=? WHERE id=?',(status,dumps(d),fid))
    def cached(self,key):
        with self.connect() as c: row=c.execute('SELECT data FROM cache WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def cache(self,key,value):
        with self.connect() as c: c.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(key,time.time(),dumps(value)))
    def heartbeat(self,jid,cache_bytes=0):
        import os
        try:
            import psutil
            rss=psutil.Process().memory_info().rss
        except ImportError:rss=None
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO runtime VALUES(?,?,?,?,?)',(jid,time.time(),os.getpid(),cache_bytes,rss))
    def runtime(self,jid):
        with self.connect() as c:r=c.execute('SELECT * FROM runtime WHERE job=?',(jid,)).fetchone()
        return dict(r) if r else None
    def recover(self,jid=None):
        # Caller must hold the OS lease. A scoped recovery never changes another job.
        with self.connect() as c:
            c.execute("UPDATE tasks SET state='pending' WHERE state='running'"+(' AND job=?' if jid else ''),(jid,) if jid else ())
            c.execute("UPDATE jobs SET state='paused' WHERE state IN ('running','preparing')"+(' AND id=?' if jid else ''),(jid,) if jid else ())
