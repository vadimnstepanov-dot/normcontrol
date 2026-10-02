import hashlib,os,json,base64
from pathlib import Path

class WordClient:
    def __init__(self,transport,cache_dir=None):self.transport=transport;self.cache_dir=cache_dir
    def read(self,source):
        from word_source import checksum,VERSION,is_doc,parts
        source=Path(source)
        if not is_doc(source) or not 0<source.stat().st_size<=50*1024**2:raise ValueError('DOC source format/size')
        if self.cache_dir is None:
            from nc5.common import DATA
            folder=DATA/'direct-doc'
        else:folder=Path(self.cache_dir)
        digest=checksum(source);folder.mkdir(parents=True,exist_ok=True)
        target=folder/(digest+'.'+VERSION+'.json')
        if target.exists():
            value=json.loads(target.read_text(encoding='utf-8'))
        else:
            with self.transport.open('/word/read',source.read_bytes(),{'Content-Type':'application/octet-stream','X-Source-Format':'doc','X-Source-SHA256':digest},timeout=300) as response:
                raw=response.read(220*1024**2+1)
                if len(raw)>220*1024**2 or response.headers.get('X-Source-SHA256')!=digest or hashlib.sha256(raw).hexdigest()!=response.headers.get('X-Artifact-SHA256'):raise ValueError('Word read identity/limit')
                value=json.loads(raw)
            if value.get('source_sha256')!=digest or value.get('adapter')!=VERSION:raise ValueError('Word read snapshot identity')
            parts(value)
            temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');os.replace(temporary,target)
        if checksum(source)!=digest or value.get('source_sha256')!=digest or value.get('adapter')!=VERSION:raise ValueError('DOC source changed')
        return value

    def review(self,source,plan,target):
        from word_source import checksum,is_doc,VERSION
        source=Path(source);target=Path(target);digest=checksum(source)
        if not is_doc(source) or plan.get('adapter')!=VERSION or plan.get('source_sha256')!=digest or plan.get('working_sha256')!=digest:raise ValueError('Native review identity')
        raw=json.dumps({'source':base64.b64encode(source.read_bytes()).decode('ascii'),'plan':plan},ensure_ascii=False).encode()
        temporary=target.with_name(target.stem+'.tmp.doc');target.parent.mkdir(parents=True,exist_ok=True)
        try:
            with self.transport.open('/word/review',raw,{'Content-Type':'application/json'},timeout=300) as response:
                expected=response.headers.get('X-Artifact-SHA256');origin=response.headers.get('X-Source-SHA256')
                report=json.loads(base64.b64decode(response.headers['X-Word-Report']))
                h=hashlib.sha256();size=0
                with temporary.open('wb') as out:
                    while chunk:=response.read(65536):
                        size+=len(chunk)
                        if size>64*1024**2:raise ValueError('DOC output exceeds limit')
                        out.write(chunk);h.update(chunk)
                if origin!=digest or expected!=h.hexdigest() or report['output_sha256']!=expected or checksum(source)!=digest or not is_doc(temporary):raise ValueError('DOC output identity')
            os.replace(temporary,target);return report
        finally:temporary.unlink(missing_ok=True)

    def anchors(self,source,requests,paragraph_count):
        from word_source import checksum,VERSION
        source=Path(source);digest=checksum(source);result={}
        encoded=base64.b64encode(source.read_bytes()).decode('ascii')
        for offset in range(0,len(requests),100):
            raw=json.dumps({'source':encoded,'source_sha256':digest,'paragraph_count':paragraph_count,'anchors':requests[offset:offset+100]},ensure_ascii=False).encode()
            with self.transport.open('/word/anchors',raw,{'Content-Type':'application/json'},timeout=300) as response:
                body=response.read(8*1024**2+1)
                if len(body)>8*1024**2 or response.headers.get('X-Source-SHA256')!=digest or hashlib.sha256(body).hexdigest()!=response.headers.get('X-Artifact-SHA256'):raise ValueError('Anchor response identity/limit')
                value=json.loads(body)
                if value.get('source_sha256')!=digest or value.get('adapter')!=VERSION:raise ValueError('Anchor source identity')
                result.update({row['locator']:row for row in value['anchors']})
        if checksum(source)!=digest:raise ValueError('DOC changed during anchor lookup')
        return result
    def export(self,source,target,format):
        from nc5.conversion import checksum,inspect_legacy
        from nc5.documents import inspect_file
        source=Path(source);target=Path(target);suffix=source.suffix.lower()
        if suffix=='.doc':inspect_legacy(source)
        elif suffix=='.docx':inspect_file(source)
        else:raise ValueError('Word source format')
        digest=checksum(source);temporary=target.with_name(target.stem+'.bridge.tmp'+target.suffix)
        target.parent.mkdir(parents=True,exist_ok=True)
        try:
            with self.transport.open('/word/'+format,source.read_bytes(),{'Content-Type':'application/octet-stream','X-Source-Format':suffix[1:],'X-Source-SHA256':digest},timeout=240) as response:
                expected=response.headers.get('X-Artifact-SHA256');origin=response.headers.get('X-Source-SHA256')
                actual=hashlib.sha256();size=0
                with temporary.open('wb') as out:
                    while chunk:=response.read(65536):
                        size+=len(chunk)
                        if size>64*1024**2:raise ValueError('Word output exceeds limit')
                        actual.update(chunk);out.write(chunk)
                if origin!=digest or actual.hexdigest()!=expected or checksum(source)!=digest:raise ValueError('Word bridge checksum mismatch')
            if format=='docx':inspect_file(temporary)
            elif format=='pdf':
                with temporary.open('rb') as file:
                    if file.read(5)!=b'%PDF-':raise ValueError('Word PDF signature')
            else:raise ValueError('Word output format')
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
        return target

def install(transport):
    from nc5 import conversion,formatting
    from nc5.common import DATA
    from nc5.documents import inspect_file
    import word_source,types
    from nc5 import documents
    original_inspect=documents.inspect_file
    client=WordClient(transport);original_prepare=conversion.prepare_word;original_check=formatting.check
    def prepare(path,cfg=None):
        if Path(path).suffix.lower()=='.docx':return original_prepare(path,cfg)
        conversion.inspect_legacy(path,(cfg or {}).get('max_file_bytes',50*1024**2))
        client.read(path)
        return Path(path).resolve()
    def check(doc,cat,mode):
        if mode=='render':
            target=DATA/'renders'/(doc['sha256']+'.pdf')
            if not target.exists():
                try:client.export(doc['path'],target,'pdf')
                except Exception as error:
                    findings,coverage=original_check(doc,cat,'xml')
                    coverage.append({'state':'unverified','check':'render','reason':type(error).__name__,'remedy':'Проверить Windows-мост Microsoft Word'})
                    return findings,coverage
        return original_check(doc,cat,mode)
    word_source.reader=client.read
    def inspect(path,max_file=50*1024**2,max_unpacked=200*1024**2):
        if word_source.is_doc(path):
            conversion.inspect_legacy(path,max_file);client.read(path);return Path(path).resolve()
        return original_inspect(path,max_file,max_unpacked)
    documents.inspect_file=inspect
    formatting.ZipFile=word_source.open_archive
    from nc5 import vision
    vision.zipfile=types.SimpleNamespace(ZipFile=word_source.open_archive)
    conversion.prepare_word=prepare;formatting.check=check
