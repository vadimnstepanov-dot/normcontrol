"""Bounded, serialized native DOC operations in the Windows bridge."""
import base64,hashlib,json,subprocess,os
from pathlib import Path
from word_source import VERSION,OLE,parts,checksum

def operation(route,raw,headers,root):
    plan=None
    anchors=False
    if route=='/word/anchors':
        value=json.loads(raw)
        if set(value)!={'source','source_sha256','paragraph_count','anchors'} or not isinstance(value['anchors'],list) or len(value['anchors'])>100:raise ValueError('DOC anchor request')
        raw=base64.b64decode(value.pop('source'),validate=True);plan=value;expected=plan['source_sha256'];anchors=True
    elif route=='/word/review':
        value=json.loads(raw)
        if set(value)!={'source','plan'}:raise ValueError('DOC review request')
        raw=base64.b64decode(value['source'],validate=True);plan=value['plan']
        expected=plan.get('source_sha256')
    else:expected=headers.get('X-Source-SHA256')
    digest=hashlib.sha256(raw).hexdigest()
    if not 0<len(raw)<=50*1024**2 or raw[:8]!=OLE or digest!=expected:raise ValueError('DOC identity')
    folder=root/digest;folder.mkdir(exist_ok=True);source=folder/'source.doc'
    if not source.exists():source.write_bytes(raw)
    if checksum(source)!=digest:raise ValueError('Stored DOC changed')
    script=Path(__file__).with_name('native_doc.ps1')
    if plan:
        key=hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        target=folder/(key+'.anchors.json' if anchors else key+'.doc');plan_file=folder/(key+'.plan.json')
        if not anchors and (plan.get('adapter')!=VERSION or plan.get('working_sha256')!=digest):raise ValueError('Review adapter identity')
        plan_file.write_text(json.dumps(plan,ensure_ascii=False),encoding='utf-8')
        args=['-Action','anchors' if anchors else 'review','-Plan',str(plan_file)]
    else:target=folder/(VERSION+'.read.json');args=['-Action','read']
    report_path=Path(str(target)+'.report.json')
    if not target.exists() or plan and not anchors and not report_path.exists():
        target.unlink(missing_ok=True);report_path.unlink(missing_ok=True)
        try:
            environment={key:value for key,value in os.environ.items() if key.upper()!='PSMODULEPATH'}
            process=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-Source',str(source),'-Destination',str(target),*args],timeout=270,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW,env=environment)
            if process.returncode:
                target.with_suffix('.error.txt').write_bytes(process.stderr[:12000])
                raise RuntimeError('Native Word operation failed; private diagnostic saved')
        except Exception:
            target.unlink(missing_ok=True);report_path.unlink(missing_ok=True);raise
    if checksum(source)!=digest:raise ValueError('Word changed DOC source')
    if plan and not anchors:
        if target.stat().st_size>64*1024**2 or target.read_bytes()[:8]!=OLE:raise ValueError('DOC result format/limit')
        report=json.loads(report_path.read_text(encoding='utf-8'))
        if report['output_sha256']!=checksum(target) or report['source_sha256']!=digest:raise ValueError('Review report identity')
        return target,digest,report
    if target.stat().st_size>220*1024**2:raise ValueError('DOC read limit')
    snapshot=json.loads(target.read_text(encoding='utf-8'))
    if snapshot.get('source_sha256')!=digest:raise ValueError('Read source identity')
    if not anchors:parts(snapshot)
    return target,digest,None
