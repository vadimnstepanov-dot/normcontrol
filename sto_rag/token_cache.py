"""Exact, bounded token measurements keyed by model identity and actual request."""
import hashlib,json,time

def key(signature,request):
 # Keep the existing normative key format so previous planning work is reusable.
 raw=json.dumps([signature,request],sort_keys=True,separators=(',',':'),ensure_ascii=False)
 return hashlib.sha256(raw.encode()).hexdigest()

class TokenCache:
 def __init__(self,connection):self.connection=connection;self.ready=False;self.writes=0
 def prepare(self,db):
  if not self.ready:
   db.execute('CREATE TABLE IF NOT EXISTS token_measurements(key TEXT PRIMARY KEY,tokens INTEGER NOT NULL,created REAL NOT NULL)');self.ready=True
 def get(self,signature,request):
  if not signature:return None
  with self.connection() as db:
   self.prepare(db);row=db.execute('SELECT tokens FROM token_measurements WHERE key=?',(key(signature,request),)).fetchone()
  value=row[0] if row else None
  return value if isinstance(value,int) and value>=0 else None
 def put(self,signature,request,tokens):
  if not signature or isinstance(tokens,bool) or not isinstance(tokens,int) or tokens<0:return
  with self.connection() as db:
   self.prepare(db);db.execute('INSERT OR REPLACE INTO token_measurements VALUES(?,?,?)',(key(signature,request),tokens,time.time()))
   self.writes+=1
   if self.writes%1000==0:db.execute('DELETE FROM token_measurements WHERE key IN (SELECT key FROM token_measurements ORDER BY created DESC LIMIT -1 OFFSET 40000)')
