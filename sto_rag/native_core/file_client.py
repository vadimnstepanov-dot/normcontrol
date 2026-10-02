"""Preserve local Windows-file selection through a bounded, authenticated import."""
import hashlib,json,re,os
from pathlib import Path,PureWindowsPath

class Importer:
    def __init__(self,transport,directory):self.transport=transport;self.directory=Path(directory)
    def path(self,value):
        text=str(value)
        if not re.match(r'^[A-Za-z]:[\\/]',text):return Path(value)
        name=PureWindowsPath(text).name
        if PureWindowsPath(name).suffix.lower() not in ('.doc','.docx'):raise ValueError('Word file required')
        raw=json.dumps({'path':text}).encode()
        with self.transport.open('/host/file',raw,{'Content-Type':'application/json'},timeout=30) as response:
            body=response.read(50*1024**2+1);digest=hashlib.sha256(body).hexdigest()
            if len(body)>50*1024**2 or digest!=response.headers.get('X-Artifact-SHA256'):raise ValueError('Imported source identity')
        target=self.directory/digest/name;target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            temporary=target.with_name(name+'.tmp');temporary.write_bytes(body);os.replace(temporary,target)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('Stored source changed')
        return target
