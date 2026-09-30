"""Publish only the existing canonical DOCX of converted DOC, once per claim."""
import base64
from pathlib import Path
from .common import read,DATA
def publish(engine,state,request):
 jid=state['job'];path=DATA/'jobs'/jid/'documents.json'
 if not path.exists():return
 known=state.setdefault('review_source_hashes',[])
 by={d['sha256']:d for d in state['claim']['files']}
 for doc in read(path):
  source=doc.get('source_sha256',doc['sha256'])
  if source==doc['sha256'] or doc['sha256'] in known:continue
  original=by.get(source)
  if not original:continue
  working=Path(doc['path'])
  if working.stat().st_size>18*1024**2:continue
  from .conversion import checksum
  if checksum(working)!=doc['sha256']:raise ValueError('Canonical review source changed')
  request(f'/worker/{state["lease"]}/review-source/{original["id"]}/',{'source_sha256':source,'working_sha256':doc['sha256'],'docx':base64.b64encode(working.read_bytes()).decode('ascii')})
  known.append(doc['sha256'])
