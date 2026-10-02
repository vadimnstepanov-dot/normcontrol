"""Versioned document understanding sidecar. Never rewrites legacy v2 evidence.

Run as python -m knowledge_v2.structure SOURCE --output DIRECTORY.
Same input/config resumes from cached regions; differing config creates a new run.
"""
import argparse
from contextlib import contextmanager
import hashlib
import html
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from .ingest import inspect, sha256, normalize
from .store import checksum

VERSION='structure-v3.1'


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as f:
        temp=Path(f.name);json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def tools_signature():
    result={'parser':VERSION}
    result['uno_runtime']=str(Path(os.environ.get('KNOWLEDGE_UNO_PYTHON','/usr/bin/python3')).exists())+':'+str(Path('/usr/lib/python3/dist-packages/uno.py').exists())
    for package in ('pdfplumber','Pillow','opencv-python-headless','pypdf'):
        try:result[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:result[package]='unavailable'
    for executable in ('tesseract','pdftoppm','soffice'):
        try:
            p=subprocess.run([executable,'--version' if executable!='pdftoppm' else '-v'],timeout=20,capture_output=True)
            result[executable]=(p.stdout+p.stderr).decode('utf-8','replace').splitlines()[0]
        except (OSError,subprocess.SubprocessError,IndexError):result[executable]='unavailable'
    # Parser implementation participates, not just a manually bumped version string.
    result['code']=checksum({p.name:sha256(p) for p in Path(__file__).parent.glob('*.py') if p.stem in
                           ('structure','structure_docx','structure_pdf','document_ocr','tables','numbering',
                            'office_probe','word_evidence','structural_model','visual_evidence','pdf_context','structure_view','visual_relations','arrow_geometry')})
    for root in ('/usr/share/tesseract-ocr/5/tessdata','/usr/share/tessdata'):
        for lang in ('rus','eng'):
            p=Path(root)/(lang+'.traineddata')
            if p.exists():result['ocr_'+lang]=sha256(p)
    return result


@contextmanager
def run_lock(path):
    # OS releases the lock after crashes; no stale PID-based guessing.
    f=open(path,'a+b');f.seek(0);f.write(b'0');f.flush();f.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise RuntimeError('Parse run is already active')
        else:
            import fcntl
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Parse run is already active')
        yield
    finally:f.close()


class Cache:
    def __init__(self,run,identity):self.run=run;self.identity=identity;self.hits=0;self.misses=0
    def key(self,operation,context):return checksum([self.identity,operation,context])
    def model_key(self,operation,context):
        return 'model-'+checksum(['model-observation/1',self.identity.get('source_sha256',self.identity),operation,context])
    def path(self,key):
        shared=(self.run.parent if self.identity.get('source_sha256') else self.run)/'model-cache'
        return (shared if key.startswith('model-') else self.run/'cache')/(key+'.json')
    def get(self,key):
        p=self.path(key)
        if not p.exists():self.misses+=1;return None
        envelope=json.loads(p.read_text(encoding='utf-8'))
        if envelope['digest']!=checksum(envelope['value']):raise ValueError('Cached region digest mismatch')
        self.hits+=1;return envelope['value']
    def put(self,key,value):atomic_json(self.path(key),dict(value=value,digest=checksum(value)))
    def progress(self,value):atomic_json(self.run/'progress.json',dict(**value,updated=time.time(),cache_hits=self.hits,cache_misses=self.misses))


def office_convert(source,out,fmt):
    """Private profile, no macros; callers use network-isolated CPU container."""
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='office-profile-') as tmp:
        profile=Path(tmp);(profile/'user').mkdir()
        (profile/'user'/'registrymodifications.xcu').write_text('''<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>''',encoding='utf-8')
        subprocess.run(['soffice','-env:UserInstallation='+profile.as_uri(),'--headless','--norestore',
                        '--convert-to',fmt,'--outdir',str(out),str(source)],timeout=240,check=True,capture_output=True)
    suffix=fmt.split(':')[0];target=out/(Path(source).stem+'.'+suffix)
    if not target.exists():raise RuntimeError('Office conversion produced no output')
    return target


def read_embedded(result,run,depth=0,budget=None):
    """Read embedded OOXML as data, never execute OLE; bounded recursion/expanded bytes."""
    from .structure_docx import read_docx
    from .tables import finalize
    if budget is None:budget=[0]
    for v in list(result['visuals']):
        if v['kind']!='embedded_object' or not v['original'].endswith('.docx'):continue
        p=run/v['original'];budget[0]+=p.stat().st_size
        if depth>=3 or budget[0]>100*1024*1024:
            v['issues']=['Embedded document recursion/size limit'];continue
        try:
            inspect(p);child=read_docx(p,run/'assets');read_embedded(child,run,depth+1,budget)
            prefix=v['id']+'/document/'
            def ref(x):return prefix+x
            for b in child['blocks']:
                b['locator']=ref(b['locator']);b['embedded_source_sha256']=v['sha256'];b['embedded_original']=v['original']
                for key in ('heading_refs','header_refs','row_label_refs','row_group_refs','note_refs'):
                    if key in b:b[key]=[ref(x) for x in b[key]]
                if b.get('table'):b['table']=ref(str(b['table']))
            for t in child['tables']:
                t['id']=ref(t['id']);t['embedded_original']=v['original']
                for key in ('heading_refs','note_refs'):t[key]=[ref(x) for x in t.get(key,[])]
                for c in t['cells']:
                    c['id']=ref(c['id'])
                    for key in ('header_refs','row_label_refs','row_group_refs','note_refs'):c[key]=[ref(x) for x in c.get(key,[])]
                    for binding in c.get('note_bindings',[]):binding['source']=ref(binding['source'])
                for slot in t.get('source_slots',[]):
                    slot['locator']=ref(slot['locator']);slot['origin']=ref(slot['origin'])
                finalize(t)
            for cv in child['visuals']:
                cv['id']=ref(cv['id']);cv['locator']=ref(cv['locator'])
                if cv.get('preview_of'):cv['preview_of']=ref(cv['preview_of'])
            for issue in child['coverage']:issue['locator']=ref(issue['locator'])
            for key in ('blocks','tables','visuals','coverage'):result[key]+=child[key]
            v.update(state='read',method='embedded-docx-xml',issues=[],embedded_blocks=len(child['blocks']),embedded_tables=len(child['tables']))
        except (OSError,ValueError,KeyError) as error:
            v.update(state='unreadable',issues=['Embedded DOCX rejected: '+type(error).__name__])


def process_visuals(result,run,cache,ocr,cancel):
    from .visual_evidence import render_visuals
    render_visuals(result,run,cache,ocr,cancel)


def view(result,run):
    from .structure_view import view as render
    render(result,run)


def parse_structure(source,output,*,ocr=True,max_pages=None,cancel=lambda:False,vision_client=None,structural_client=None,interpret_graphics=False):
    source=Path(source).resolve();kind=inspect(source)
    identity=dict(source_sha256=sha256(source),kind=kind,parser_version=VERSION,tools=tools_signature(),
                  options=dict(ocr=ocr,max_pages=max_pages,dpi=220,ocr_language='rus+eng',
                               vision_signature=vision_client.signature if vision_client else None,
                               structural_signature=structural_client.signature if structural_client else None,
                               interpret_graphics=interpret_graphics))
    run_id=checksum(identity);run=Path(output).resolve()/run_id;run.mkdir(parents=True,exist_ok=True)
    with run_lock(run/'.lock'):
        resultfile=run/'result.json'
        if resultfile.exists():
            result=json.loads(resultfile.read_text(encoding='utf-8'))
            if result['result_digest']!=checksum({k:v for k,v in result.items() if k!='result_digest'}):raise ValueError('Parse result corrupted')
            for relative,digest in result.get('artifact_hashes',{}).items():
                artifact=(run/relative).resolve()
                if not artifact.is_relative_to(run) or not artifact.is_file() or sha256(artifact)!=digest:
                    raise ValueError('Parse artifact corrupted or missing: '+relative)
            return result,run
        cache=Cache(run,identity);(run/'assets').mkdir(exist_ok=True)
        original=run/('original'+kind)
        if not original.exists():shutil.copyfile(source,original)
        elif sha256(original)!=identity['source_sha256']:raise ValueError('Original hash mismatch')
        started=time.monotonic();cache.progress(dict(state='running',run_id=run_id))
        try:
            if kind in ('.docx','.doc'):
                from .structure_docx import read_docx
                parsed=original
                inspect(parsed)
                result=read_docx(parsed,run/'assets')
                from .word_evidence import enrich_word
                enrich_word(result,parsed,run,cache,cancel)
                read_embedded(result,run)
                process_visuals(result,run,cache,ocr,cancel)
                # References are source occurrences, not just distinct binary assets.
                for v in result['visuals']:
                    if v['state'] not in ('verified','read'):result['coverage'].append(dict(locator=v['id'],state='unreadable' if v['state']=='unreadable' else 'needs_review',reason='; '.join(v['issues'])))
            else:
                from .structure_pdf import read_pdf
                result=read_pdf(original,run,cache,max_pages=max_pages,ocr=ocr,cancel=cancel)
                if vision_client:
                    from .document_ocr import review_regions
                    review_regions(result,run,cache,vision_client,cancel)
            if structural_client:
                from .structural_model import interpret_tables
                interpret_tables(result,cache,structural_client,cancel)
                if interpret_graphics:
                    from .visual_evidence import interpret_visuals
                    interpret_visuals(result,run,cache,structural_client,cancel)
                    ids={v['id'] for v in result['visuals']}
                    result['coverage']=[x for x in result['coverage'] if x['locator'] not in ids]
                    for v in result['visuals']:
                        if v['state'] not in ('verified','read'):
                            result['coverage'].append(dict(locator=v['id'],state='unreadable' if v['state']=='unreadable' else 'needs_review',reason='; '.join(v['issues'])))
            from .tables import continuations,finalize
            from .structural_model import refresh_table_blocks
            continuations(result['tables'])
            for t in result['tables']:finalize(t)
            refresh_table_blocks(result)
            # Context incorporates the complete table and dependencies, not just a cell value.
            contexts={t['id']:t['digest'] for t in result['tables']}
            for b in result['blocks']:
                b['context_hash']=checksum([identity,b.get('heading_path',[]),contexts.get(b.get('table')),
                                            b.get('note_refs',[]),b['exact_text'],b.get('bbox')])
                b['source_sha256']=identity['source_sha256']
            result.update(parser_version=VERSION,run_id=run_id,identity=identity,
                source=dict(filename=source.name,sha256=identity['source_sha256'],kind=kind,original=original.name),
                seconds=time.monotonic()-started)
            result['summary']=dict(blocks=len(result['blocks']),tables=len(result['tables']),
                tables_structure_pass=sum(t['structure_status']=='pass' for t in result['tables']),
                tables_needing_review=sum(t['structure_status']!='pass' for t in result['tables']),
                tables_model_interpreted=sum(t.get('header_basis')=='model_structure' for t in result['tables']),
                tables_source_geometry=sum(t.get('header_basis')=='source_geometry' for t in result['tables']),
                tables_model_rejected=sum('model_structure_rejected' in t['issues'] for t in result['tables']),
                word_verified_labels=sum(bool(b.get('numbering_verified') and b.get('number_label')) for b in result['blocks']),
                embedded_previews_linked=sum(v.get('role')=='embedded_document_preview' for v in result['visuals']),
                cells=sum(len(t['cells']) for t in result['tables']),visual_occurrences=len(result['visuals']),
                unreadable=sum(x['state']=='unreadable' for x in result['coverage']),
                issues=len(result['coverage']),cache_hits=cache.hits,cache_misses=cache.misses,
                semantic_coverage='not_evaluated_in_structural_stage')
            result['artifact_hashes']={p.relative_to(run).as_posix():sha256(p) for p in run.rglob('*') if p.is_file()
                and (p==original or p.relative_to(run).parts[0] in ('assets','conversions'))}
            if tools_signature().get('code')!=identity['tools'].get('code'):
                raise RuntimeError('Parser code changed during run; resume with a stable release; model observations retained')
            result['result_digest']=checksum(result);atomic_json(resultfile,result);view(result,run)
            cache.progress(dict(state='partial' if result['coverage'] or result['summary']['tables_needing_review'] else 'parsed',run_id=run_id,summary=result['summary']))
            return result,run
        except BaseException as exc:
            cache.progress(dict(state='paused' if isinstance(exc,(InterruptedError,KeyboardInterrupt)) else 'failed',error=type(exc).__name__,run_id=run_id))
            raise


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source');p.add_argument('--output',required=True)
    p.add_argument('--no-ocr',action='store_true');p.add_argument('--max-pages',type=int)
    p.add_argument('--vision',action='store_true');p.add_argument('--queue-data');p.add_argument('--model')
    p.add_argument('--structure-model',action='store_true');p.add_argument('--interpret-graphics',action='store_true');p.add_argument('--model-revision')
    args=p.parse_args();stop=[False]
    if args.max_pages is not None and args.max_pages<1:p.error('--max-pages must be positive')
    vision=None;structural=None
    if args.structure_model:
        from .structural_model import StructuralClient
        from .store import KnowledgeStore
        endpoint=os.getenv('NORMCONTROL_LLM_ENDPOINT','')
        if not endpoint or not args.queue_data or not args.model or not args.model_revision:
            p.error('--structure-model requires endpoint, --queue-data, --model and --model-revision')
        structural=StructuralClient(endpoint,args.model,KnowledgeStore(args.queue_data),
            api_key=os.getenv('NORMCONTROL_LLM_API_KEY',''),revision=args.model_revision)
    if args.interpret_graphics and not structural:p.error('--interpret-graphics requires --structure-model with a Vision-capable endpoint')
    if args.vision:
        from .document_ocr import VisionTables
        from .store import KnowledgeStore
        endpoint=os.getenv('NORMCONTROL_LLM_ENDPOINT','')
        if not endpoint or not args.queue_data or not args.model:p.error('--vision requires configured NORMCONTROL_LLM_ENDPOINT, --queue-data and --model')
        vision=VisionTables(endpoint,args.model,KnowledgeStore(args.queue_data))
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.__setitem__(0,True))
    try:r,path=parse_structure(args.source,args.output,ocr=not args.no_ocr,max_pages=args.max_pages,cancel=lambda:stop[0],vision_client=vision,
        structural_client=structural,interpret_graphics=args.interpret_graphics)
    except InterruptedError:raise SystemExit(75)
    print(json.dumps(dict(run_id=r['run_id'],summary=r['summary'],viewer=str(path/'viewer.html'),seconds=r['seconds']),ensure_ascii=False))


if __name__=='__main__':main()
