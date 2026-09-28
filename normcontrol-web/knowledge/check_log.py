"""Authenticated, bounded journal delivery and streaming XLSX projection."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape
from django.db import transaction
from django.http import JsonResponse, FileResponse
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from .models import Command, KnowledgeCheck, CheckLogChunk
from .views import boundary, fields
from .access import require
from .checks import visible
from .services import Conflict, digest
from knowledge_v2.check_log import clean


def validate_chunk(d):
    if type(d.get('sequence')) is not int or d['sequence']<0 or not isinstance(d.get('entries'),list) or not 1<=len(d['entries'])<=20 or digest(d['entries'])!=d.get('digest'):raise ValueError('Journal checksum')
    for e in d['entries']:
        if set(e)!={'event_id','kind','part','total','text','sha256'} or not isinstance(e['text'],str) or len(e['text'])>24000:raise ValueError('Journal event bounds')
        if type(e['part']) is not int or type(e['total']) is not int or not 0<=e['part']<e['total']<=100000:raise ValueError('Journal part bounds')


@boundary({'POST'}, worker=True)
def receive(request):
    from django.conf import settings
    d=fields(request,{'command_id','lease','job_id','sequence','entries','digest'})
    with transaction.atomic():
        c=Command.objects.select_for_update().select_related('normative_set__scope','actor').get(pk=d['command_id'],kind='review.execute')
        if c.state!='delivering' or str(c.lease)!=d['lease'] or c.worker_id!=settings.KNOWLEDGE_WORKER_ID or c.lease_until<=timezone.now() or c.payload['job_id']!=d['job_id']:raise Conflict('Stale journal lease')
        require(c.actor,c.normative_set.scope,'read')
        if not c.payload.get('logging',{}).get('enabled'):raise ValueError('Logging disabled')
        validate_chunk(d)
        row,new=CheckLogChunk.objects.get_or_create(job_id=d['job_id'],sequence=d['sequence'],defaults=dict(entries=d['entries'],digest=d['digest']))
        if not new and row.digest!=d['digest']:raise Conflict('Journal replay changed')
    return JsonResponse({'accepted':True})


def stream_events(chunks):
    current=None;parts=[];expected=None;sha=None
    last=-1
    for chunk in chunks.order_by('sequence').iterator(chunk_size=1):
        if chunk.sequence!=last+1:raise ValueError('Journal delivery has missing chunks')
        last=chunk.sequence
        for row in chunk.entries:
            if current!=row['event_id']:
                if current is not None and len(parts)!=expected:raise ValueError('Incomplete journal event')
                current=row['event_id'];parts=[];expected=row['total'];sha=row['sha256']
            if row['part']!=len(parts) or row['total']!=expected or row['sha256']!=sha:raise ValueError('Journal event sequence')
            parts.append(row['text'])
            if len(parts)==expected:
                event=json.loads(''.join(parts))
                if digest(event)!=sha:raise ValueError('Journal event changed')
                yield clean(event)
    if current is not None and len(parts)!=expected:raise ValueError('Incomplete journal event')


def events(job):
    for chunks in (job.log_chunks,job.batch.log_chunks):
        seen=False;final=False
        try:
            for event in stream_events(chunks):
                seen=True;final=final or event['kind']=='result';yield event
            if seen and job.state in ('completed','partial','failed','cancelled') and not final:
                raise ValueError('Terminal check has no final journal marker; delivery may be incomplete')
        except ValueError as e:
            yield dict(id='integrity',kind='error',at=timezone.now().isoformat(),value={'journal_incomplete':True,'reason':str(e)})


def flatten(value,path=''):
    if isinstance(value,dict):
        if not value:yield path,'{}'
        for key,v in value.items():yield from flatten(v,path+'.'+str(key) if path else str(key))
    elif isinstance(value,list):
        if not value:yield path,'[]'
        for n,v in enumerate(value):yield from flatten(v,f'{path}[{n}]')
    else:yield path,value if value is not None else 'Не сообщено'


SHEETS=['00_Паспорт','01_Параметры','02_Документы','03_Нормативная_база','04_Применимость','05_План','06_RAG','07_Вызовы_LLM','08_Содержимое','09_Решения','10_Доказательства','11_Время_и_ресурсы','12_Ошибки_и_события','13_Файлы_и_целостность']


def rows(job):
    yield 0,['ID проверки',str(job.pk),'Состояние',job.state,'Срез',timezone.now().isoformat()]
    yield 0,['Примечание','Незавершённая проверка: журнал является промежуточным срезом' if job.state not in ('completed','partial') else 'Завершённый срез']
    yield 0,['Метрики','Не сообщённые сервером значения не считаются измеренными. Дополнительный аппаратный мониторинг не выполняется.']
    yield 6,['Политика','Нормативы выбираются по закреплённым профилям; поиск RAG может не вызываться. См. фактический план.']
    yield 11,['Ресурсы','Используются только timings реальных ответов. Дополнительные замеры CPU/GPU не выполнялись.']
    for k,v in flatten(job.snapshot.data):yield 3,['Снимок',k,v]
    for d in job.batch.documents.order_by('id'):yield 2,[str(d.pk),d.name,d.size,d.sha256]
    for command in Command.objects.filter(kind='review.execute',payload__job_id=str(job.pk)).order_by('created'):
        for k,v in flatten(clean(command.payload)):yield 1,['Зафиксировано при запуске',str(command.pk),k,v]
    for e in events(job):
        identity=e['id'];kind=e['kind'];value=e['value'];at=e['at']
        sheet={'passport':1,'plan':5,'task':5,'request':7,'response':7,'cached_response':7,'checkpoint':9,'rag':6,'model_configuration':1,'error':12,'result':9}.get(kind,12)
        for k,v in flatten(value):
            # Rich contents: split losslessly below Excel's UTF-16 cell limit.
            if isinstance(v,str) and len(v.encode('utf-16-le'))//2>30000:
                sha=__import__('hashlib').sha256(v.encode('utf8')).hexdigest()
                yield sheet,[identity,kind,at,k,'Содержимое: '+sha]
                for n in range(0,len(v),14000):yield 8,[sha,identity,k,n//14000+1,v[n:n+14000]]
            else:yield sheet,[identity,kind,at,k,v]
            if k.endswith(('prompt_per_second','predicted_per_second','prompt_ms','predicted_ms','seconds','prompt_tokens','completion_tokens')):yield 11,[identity,at,k,v]
            if 'evidence' in k or 'citations' in k:yield 10,[identity,k,v]
            if 'applicability' in k or 'not_applicable' in k:yield 4,[identity,k,v]
            if 'sha256' in k or 'digest' in k or 'signature' in k:yield 13,[identity,k,v]
    for chunk in job.finding_chunks.order_by('sequence').iterator(chunk_size=1):
        for d in chunk.entries:
            for k,v in flatten(d):yield 9,[d.get('id',''),k,v]
    for k,v in flatten(job.summary):yield 0,[k,v]


def xml_cell(value,style=2):
    text=str(value)
    text=''.join(c for c in text if ord(c)>=32 or c in '\t\n\r')
    # Inline strings cannot execute formulas and preserve IDs/leading zeroes.
    return f'<c t="inlineStr" s="{style}"><is><t xml:space="preserve">'+escape(text)+'</t></is></c>'


MAX_ROWS=1048576


def workbook(job):
    target=tempfile.TemporaryFile()
    styles='<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="3"><xf/><xf fontId="1" applyFont="1"><alignment wrapText="1" vertical="top"/></xf><xf><alignment wrapText="1" vertical="top"/></xf></cellXfs></styleSheet>'
    with tempfile.TemporaryDirectory(prefix='check-log-export-') as tmp:
        paths=[];files=[];counts=[];names=[];volumes={};current={}
        def opened(n):
            volume=volumes.get(n,0)+1;volumes[n]=volume
            name=SHEETS[n] if volume==1 else SHEETS[n][:26]+'_'+str(volume)
            index=len(files);path=Path(tmp)/f'{index}.xml';f=path.open('w',encoding='utf8')
            paths.append(path);files.append(f);counts.append(1);names.append(name);current[n]=index
            f.write('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols>'+''.join(f'<col min="{k}" max="{k}" width="{w}" customWidth="1"/>' for k,w in enumerate([30,26,26,50,90,25],1))+'</cols><sheetData><row r="1">'+''.join(xml_cell(x,1) for x in ['ID / источник','Тип / поле','Время / значение','Параметр / путь','Значение','Дополнительно'])+'</row>')
            return index
        for n in range(len(SHEETS)):opened(n)
        try:
            for n,row in rows(job):
                index=current[n]
                if counts[index]>=MAX_ROWS:index=opened(n)
                counts[index]+=1
                files[index].write(f'<row r="{counts[index]}">'+''.join(xml_cell(v) for v in row)+'</row>')
            for n,f in enumerate(files):f.write(f'</sheetData><autoFilter ref="A1:F{counts[n]}"/></worksheet>');f.close()
            with ZipFile(target,'w',ZIP_DEFLATED) as z:
                z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{n+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for n in range(len(files)))+'</Types>')
                z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
                z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{name}" sheetId="{n+1}" r:id="rId{n+1}"/>' for n,name in enumerate(names))+'</sheets></workbook>')
                z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'+''.join(f'<Relationship Id="rId{n+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{n+1}.xml"/>' for n in range(len(files)))+'</Relationships>')
                z.writestr('xl/styles.xml',styles)
                for n,p in enumerate(paths):z.write(p,f'xl/worksheets/sheet{n+1}.xml')
        finally:
            for f in files:
                if not f.closed:f.close()
    target.seek(0);return target


@login_required
def download(request,job_id):
    job=KnowledgeCheck.objects.select_related('batch','snapshot').get(pk=job_id);visible(request.user,job)
    # Recheck each original source ACL; normative visibility alone is insufficient.
    from .models import SourceUpload
    from .curation import release_catalog
    for r in job.snapshot.data['releases']:
        from .models import Release
        for item in release_catalog(Release.objects.get(pk=r['release_id'])):
            source=SourceUpload.objects.get(pk=item['source_id']);require(request.user,source.normative_set.scope,'read')
    if not job.batch.logging_enabled or not (job.log_chunks.exists() or job.batch.log_chunks.exists()):return JsonResponse({'error':'log_not_available'},status=404)
    return FileResponse(workbook(job),as_attachment=True,filename=f'Лог-проверки-{job.pk}.xlsx',content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
