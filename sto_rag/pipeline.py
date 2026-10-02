"""One host-owned resource queue and immutable preparation artifacts.

SQLite stays on Windows; container clients use the authenticated gateway API.
Specialized checking rules and their evidence addresses are never translated.
"""
import json,sqlite3,time,uuid,threading,os
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

VERSION='pipeline-v1'
SYSTEM_RESERVE_MB=2048
DOCUMENT_CACHE_CAP_MB=1024
CPU_POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='pipeline-cpu')

class QueuePaused(InterruptedError):
 """Resource admission was cancelled before model work; this is not a failure."""

def waiting(ticket,callback):
 if callback and callback(ticket):raise QueuePaused('Resource wait paused')

def memory_mb():
 """Physical available RAM, including WSL usage on the Windows host."""
 if os.name=='nt':
  import ctypes
  class Memory(ctypes.Structure):
   _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong) for name in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
  value=Memory();value.length=ctypes.sizeof(value)
  if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):raise OSError('RAM measurement unavailable')
  return value.total//1048576,value.available//1048576
 values={line.split(':')[0]:int(line.split()[1])//1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
 return values['MemTotal'],values['MemAvailable']

def preparation_mb(paths):
 # Conservative working reservation for decompression and two parser views.
 return 512+sum(Path(p).stat().st_size for p in paths)*20//1048576

def cache_budget(requested):
 total,available=memory_mb()
 # Serialized caches coexist with parser objects, WSL and inference buffers.
 usable=max(0,available-system_reserve_mb(total)-1024)
 return min(requested,DOCUMENT_CACHE_CAP_MB*1048576,usable*1048576//4)

def system_reserve_mb(total):
 return SYSTEM_RESERVE_MB

class Coordinator:
 def __init__(self,path,cpu_slots=2,memory_probe=None):
  self.memory_probe=memory_probe or memory_mb
  self.path=str(path);self.cpu_slots=max(1,cpu_slots);Path(path).parent.mkdir(parents=True,exist_ok=True)
  with self.db() as db:
   db.executescript('CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,pipeline TEXT,stage TEXT,resource TEXT,state TEXT,created REAL,expires REAL); CREATE INDEX IF NOT EXISTS resource_queue ON tickets(resource,state,created); CREATE TABLE IF NOT EXISTS phases(pipeline TEXT,stage TEXT,state TEXT,deps TEXT,error TEXT DEFAULT "",PRIMARY KEY(pipeline,stage));')
   if 'ram_mb' not in {r[1] for r in db.execute('PRAGMA table_info(tickets)')}:db.execute('ALTER TABLE tickets ADD COLUMN ram_mb INTEGER NOT NULL DEFAULT 512')
 @contextmanager
 def db(self):
  db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
  try:
   with db:yield db
  finally:db.close()
 def register(self,pipeline,directions):
  stages={'material':[]}
  for name in ('language','logic','formatting'):
   if name in directions:stages[name]=['material']
  if 'logic' in directions:stages.update(cross=['logic'],inter=['logic'])
  stages['verify_native']=['material']
  if 'sto' in directions:stages.update(normative_prepare=['material'],normative=['normative_prepare'],verify_normative=['normative_prepare'],trace=['normative'],vision=['normative','verify_native'])
  stages['complete']=[name for name in ('language','logic','formatting','cross','inter','verify_native','normative','verify_normative','trace','vision') if name in stages]
  with self.db() as db:
   for stage,deps in stages.items():db.execute('INSERT OR IGNORE INTO phases(pipeline,stage,state,deps) VALUES(?,?,?,?)',(pipeline,stage,'waiting',json.dumps(deps)))
 def phase(self,pipeline,stage,state,error=''):
  if state not in ('waiting','running','done','failed','paused'):raise ValueError('Phase state')
  with self.db() as db:
   if state=='running':
    row=db.execute('SELECT deps FROM phases WHERE pipeline=? AND stage=?',(pipeline,stage)).fetchone()
    if row:
     for dep in json.loads(row['deps']):
      predecessor=db.execute('SELECT state FROM phases WHERE pipeline=? AND stage=?',(pipeline,dep)).fetchone()
      if not predecessor or predecessor['state']!='done':raise ValueError('Dependency not complete: '+dep)
   db.execute('UPDATE phases SET state=?,error=? WHERE pipeline=? AND stage=?',(state,error[:300],pipeline,stage))
 def status(self,pipeline):
  with self.db() as db:
   rows=[dict(r) for r in db.execute('SELECT stage,state,deps,error FROM phases WHERE pipeline=?',(pipeline,))]
   states={row['stage']:row['state'] for row in rows}
   for row in rows:
    if row['stage']=='complete' and all(states.get(dep)=='done' for dep in json.loads(row['deps'])):row['state']='done'
   return rows

 def resources(self,pipeline):
  total,available=self.memory_probe()
  with self.db() as db:
   queue=[dict(row) for row in db.execute('SELECT stage,resource,state,ram_mb FROM tickets WHERE pipeline=? AND expires>?',(pipeline,time.time()))]
   reserved=db.execute("SELECT coalesce(sum(ram_mb),0) FROM tickets WHERE state='running' AND expires>?",(time.time(),)).fetchone()[0]
  for row in queue:row['waiting_reason']=('ram' if available-reserved-row['ram_mb']<system_reserve_mb(total) else 'resource') if row['state']=='waiting' else None
  return {'total_mb':total,'available_mb':available,'reserve_mb':system_reserve_mb(total),'cpu_slots':self.cpu_slots,'gpu_slots':1,'queue':queue}
 def ticket(self,pipeline,stage,resource,identifier=None,ram_mb=512):
  if resource not in ('cpu','gpu'):raise ValueError('Resource')
  if isinstance(ram_mb,bool) or not isinstance(ram_mb,int) or ram_mb<64:raise ValueError('RAM reservation')
  total,available=self.memory_probe();reserve=system_reserve_mb(total)
  if ram_mb>total-reserve:raise MemoryError('Task exceeds physical RAM budget')
  now=time.time();identifier=identifier or uuid.uuid4().hex
  with self.db() as db:
   db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM tickets WHERE expires<?',(now,))
   if not db.execute('SELECT 1 FROM tickets WHERE id=?',(identifier,)).fetchone():db.execute('INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?)',(identifier,pipeline,stage,resource,'waiting',now,now+90,ram_mb))
   row=db.execute('SELECT * FROM tickets WHERE id=?',(identifier,)).fetchone()
   if (row['pipeline'],row['stage'],row['resource'])!=(pipeline,stage,resource):raise ValueError('Ticket identity changed')
   if row['ram_mb']!=ram_mb:raise ValueError('RAM reservation changed')
   db.execute('UPDATE tickets SET expires=? WHERE id=?',(now+90,identifier))
   if row['state']=='waiting':
    capacity=1 if resource=='gpu' else self.cpu_slots
    busy=db.execute("SELECT count(*) FROM tickets WHERE resource=? AND state='running'",(resource,)).fetchone()[0]
    reserved=db.execute("SELECT coalesce(sum(ram_mb),0) FROM tickets WHERE state='running'").fetchone()[0]
    # FIFO among tasks that fit RAM. Otherwise a heavy GPU waiter can block a
    # small token probe needed by the CPU task holding its memory reservation.
    first=db.execute("SELECT id FROM tickets WHERE resource=? AND state='waiting' AND ram_mb<=? ORDER BY created,id LIMIT 1",(resource,available-reserved-reserve)).fetchone()
    if busy<capacity and first and first[0]==identifier and available-reserved-ram_mb>=reserve:db.execute("UPDATE tickets SET state='running' WHERE id=?",(identifier,))
   state=db.execute('SELECT state FROM tickets WHERE id=?',(identifier,)).fetchone()[0]
  return {'id':identifier,'state':state,'ram_mb':ram_mb,'available_mb':available,'reserve_mb':reserve,'waiting_reason':('ram' if available-reserved-ram_mb<reserve else 'resource') if state=='waiting' else None}
 def release(self,identifier,pipeline=None):
  with self.db() as db:
   if pipeline is None:db.execute('DELETE FROM tickets WHERE id=?',(identifier,))
   else:db.execute('DELETE FROM tickets WHERE id=? AND pipeline=?',(identifier,pipeline))
 @contextmanager
 def turn(self,pipeline,stage,resource,timeout=600,ram_mb=512,on_wait=None):
  # timeout remains accepted for callers; only actual work has a deadline.
  ticket=self.ticket(pipeline,stage,resource,ram_mb=ram_mb);stop=threading.Event();last_notice=-float('inf')
  try:
   while ticket['state']!='running':
    if time.monotonic()-last_notice>=1:
     waiting(ticket,on_wait);last_notice=time.monotonic()
    # Admission is backpressure, not an LLM attempt or a finite task deadline.
    time.sleep(.25);ticket=self.ticket(pipeline,stage,resource,ticket['id'],ram_mb)
   lost=[]
   def renew():
    while not stop.wait(15):
     try:
      if self.ticket(pipeline,stage,resource,ticket['id'],ram_mb)['state']!='running':lost.append(True);return
     except Exception:lost.append(True);return
   thread=threading.Thread(target=renew,daemon=True);thread.start()
   try:
    yield ticket['id']
    if lost:raise RuntimeError('Resource lease lost')
   finally:stop.set();thread.join(2)
  finally:self.release(ticket['id'])

def host():
 from nc5.common import DATA
 return Coordinator(DATA/'pipeline.sqlite3')

def artifact_path(pipeline):
 from nc5.common import DATA
 if str(uuid.UUID(pipeline))!=pipeline:raise ValueError('Pipeline UUID')
 return DATA/'pipelines'/pipeline/'normative.json'

def prepare_normative(pipeline,paths,prepared_paths,on_wait=None):
 """CPU adapter reuses the inspected DOCX conversion, keeps original source IDs."""
 from knowledge_v2.review import corpus
 from nc5.common import write
 coordinator=host()
 try:
  coordinator.phase(pipeline,'normative_prepare','running')
  with coordinator.turn(pipeline,'normative_prepare','cpu',ram_mb=preparation_mb(paths),on_wait=on_wait):
   docs=corpus(paths,prepared_paths=prepared_paths)
   write(artifact_path(pipeline),{'version':VERSION,'pipeline':pipeline,'documents':docs})
  coordinator.phase(pipeline,'normative_prepare','done')
 except QueuePaused:
  coordinator.phase(pipeline,'normative_prepare','paused');raise
 except Exception as error:
  coordinator.phase(pipeline,'normative_prepare','failed',type(error).__name__)
  raise

def submit_preparation(pipeline,paths,prepared_paths,on_wait=None):
 return CPU_POOL.submit(prepare_normative,pipeline,list(paths),dict(prepared_paths),on_wait)

@contextmanager
def remote_turn(client,stage='normative',resource='gpu',ram_mb=512):
 """Containers never open the host's SQLite file or reuse OS-specific locks."""
 endpoint=getattr(client,'pipeline_endpoint',None)
 if not endpoint:raise ValueError('Pipeline endpoint missing')
 import urllib.request
 def request(action,identifier=None):
  value={'pipeline':client.pipeline_id,'stage':stage,'resource':resource,'action':action,'ram_mb':ram_mb}
  if identifier:value['id']=identifier
  transport=getattr(client,'pipeline_request',None)
  if transport:return transport(value)
  headers={'Content-Type':'application/json','Authorization':'Bearer '+__import__('os').environ.get('NORMCONTROL_LLM_API_KEY','')}
  req=urllib.request.Request(endpoint+'/pipeline/ticket',data=json.dumps(value).encode(),headers=headers)
  with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
 ticket=request('acquire');stop=threading.Event();thread=None;lost=[];last_notice=-float('inf')
 try:
  while ticket['state']!='running':
   if time.monotonic()-last_notice>=1:
    waiting(ticket,getattr(client,'_queue_wait',None));last_notice=time.monotonic()
   time.sleep(.5);ticket=request('acquire',ticket['id'])
  def renew():
   while not stop.wait(15):
    try:
     if request('acquire',ticket['id'])['state']!='running':lost.append(True);return
    except Exception:lost.append(True);return
  thread=threading.Thread(target=renew,daemon=True);thread.start()
  previous=getattr(client,'_model_ticket',None)
  if resource=='gpu':client._model_ticket=ticket['id']
  try:
   yield ticket['id']
   if lost:raise RuntimeError('Unified GPU lease lost')
  finally:
   if resource=='gpu':client._model_ticket=previous
 finally:
  stop.set()
  if thread:thread.join(2)
  request('release',ticket['id'])
