"""Native DOC I/O around the shared, frozen Word review plan."""
import os
from pathlib import Path
from django.conf import settings
from . import word_review as shared
from word_source import VERSION, parts, anchor_map, is_doc

def client():
    from .models import LLMConfig
    from .llm_connection import settings_payload
    from native_core.transport import Transport
    from native_core.word_client import WordClient
    config=LLMConfig.objects.first()
    if not config:raise shared.ReviewError('Не настроено подключение обработчика DOC')
    connection=settings_payload(config,True)
    return WordClient(Transport(connection['endpoint'],connection.get('api_key',''),os.environ['NORMCONTROL_LLM_CA_FILE']),Path(settings.DATA_DIR)/'direct-doc')

def plan(source,working,document,*args,**kwargs):
    if not is_doc(source):return shared.plan(source,working,document,*args,**kwargs)
    snapshot=client().read(source)
    root=shared.xml(parts(snapshot)['word/document.xml']);ps=shared.paragraphs(root)
    description=dict(document)
    legacy=description.pop('legacy_working',None)
    if legacy:
        old=shared.paragraphs(shared.load(legacy))
        if [shared.text(p) for p in old]==[shared.text(p) for p in ps]:
            description['aliases']=sorted(set(description['aliases'])|{shared.sha(legacy),shared.sha(legacy)[:20]})
    saved=shared.plan(source,source,description,*args,**kwargs,root=root)
    if snapshot.get('paragraphs'):
        mapping=anchor_map(snapshot,ps,shared.text)
    else:
        main=[(i,p) for i,p in enumerate(ps,1) if not any(a.tag==shared.Q+'txbxContent' for a in p.iterancestors())]
        selected={op['locator'] for op in saved['operations']}
        requests=[{'locator':'p'+str(i),'paragraph':j,'text':shared.text(p)} for j,(i,p) in enumerate(main,1) if 'p'+str(i) in selected]
        mapping=client().anchors(source,requests,snapshot['paragraph_count'])
    kept=[]
    for op in saved['operations']:
        row=mapping.get(op['locator'])
        if row is None:
            saved['outcomes'].append({'finding_id':op['finding_ids'][0],'kind':'skip','reason':'Диапазон DOC не подтверждён: иной поток текста или неоднозначное соответствие'})
            continue
        value=shared.text(ps[int(op['locator'][1:])-1])
        if not row['editable'] or op.get('anchor_scope')=='paragraph':
            if op['type']!='comment':
                op.update(type='comment',validation='comment_only',reason='DOC содержит прежние правки, поле или объект: требуется решение специалиста')
                op['comment']+='\nОграничение: '+op['reason']
            start=row['start'];end=row['end']
        else:
            # Word character positions use UTF-16 code units, not Python indices.
            start=row['start']+len(value[:op['start']].encode('utf-16-le'))//2
            end=row['start']+len(value[:op['end']].encode('utf-16-le'))//2
        op.update(native_start=start,native_end=end,paragraph_start=row['start'],paragraph_end=row['end'],paragraph_raw=row['raw'])
        kept.append(op)
    saved.update(adapter=VERSION,output_format='doc',operations=kept)
    return saved

def generate(source,working,saved,destination):
    if saved.get('adapter')!=VERSION:
        if is_doc(working):raise shared.ReviewError('Нужно заново сформировать план экспорта DOC')
        return shared.generate(source,working,saved,destination)
    if saved['source_sha256']!=shared.sha(source) or saved['working_sha256']!=shared.sha(working):raise shared.ReviewError('Исходник DOC изменился')
    report=client().review(source,saved,destination)
    report.update(skips=len(saved['outcomes']),outcomes=saved['outcomes'],format='doc',adapter=VERSION)
    return report
