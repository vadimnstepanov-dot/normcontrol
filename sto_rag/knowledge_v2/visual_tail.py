"""Deferred, checkpointed image checks. Pixel observations never approve a norm."""
import base64,json,math,shutil,time,threading,re
from .performance import measured
from pathlib import Path
from .store import checksum,Conflict
from .ingest import sha256,inspect
from .structure import Cache,atomic_json,office_convert

VERSION='visual-tail-v1'
IMAGE_RESERVE=8192

@measured('v2.vision.prepare')
def prepare(paths,docs,store):
    """Detect/extract once on CPU. Rendering and inference are deferred."""
    for source,doc in zip(paths,docs):
        root=store.directory/'review-visuals'/doc['id'];root.mkdir(parents=True,exist_ok=True)
        saved=root/'inventory.json'
        if saved.exists():
            value=json.loads(saved.read_text());
            if value['digest']!=checksum(value['inventory']):raise Conflict('Visual inventory changed')
            doc['visual_inventory']=value['inventory'];continue
        kind=inspect(source);original=root/('original'+kind)
        if not original.exists():shutil.copyfile(source,original)
        if sha256(original)!=doc['sha256']:raise Conflict('Visual source changed')
        items=[];limitations=[]
        if kind in ('.doc','.docx'):
            from .structure_docx import read_docx
            parsed=original if kind=='.docx' else office_convert(original,root/'conversion','docx')
            (root/'assets').mkdir(exist_ok=True)
            raw=read_docx(parsed,root/'assets')
            by_sha={}
            for v in raw['visuals']:
                # Non-Visio embedded files remain explicit source gaps; a raster
                # preview is still inventoried as a separate image occurrence.
                if v['kind']=='embedded_object' and not v.get('progid','').startswith('Visio.'):
                    limitations.append({'locator':v['locator'],'reason':'Embedded object requires its native document reader'});continue
                item=by_sha.setdefault(v['sha256'],dict(v,occurrences=[],document_id=doc['id']))
                item['occurrences'].append(v['locator'])
            items=[]
            for item in by_sha.values():
                asset=root/item['original']
                if item.get('progid','').startswith('Visio.') or asset.suffix.lower() in ('.emf','.wmf'):
                    try:
                        if item.get('progid','').startswith('Visio.'):
                            asset=root/'conversion'/(item['sha256']+'.vsd');asset.parent.mkdir(exist_ok=True);shutil.copyfile(root/item['original'],asset)
                        converted=office_convert(asset,root/'conversion'/'visuals','pdf:draw_pdf_Export')
                        from pypdf import PdfReader
                        count=len(PdfReader(converted).pages)
                        if not 1<=count<=100:raise ValueError('Visual page limit')
                        for number in range(1,count+1):
                            items.append(dict(item,page=number,render_pdf=converted.relative_to(root).as_posix(),render_pdf_sha256=sha256(converted)))
                    except Exception as exc:
                        limitations.append(dict(locator=item['locator'],reason='Visual page inventory unavailable: '+type(exc).__name__))
                else:items.append(item)
        else:
            import pdfplumber
            with pdfplumber.open(original) as pdf:
                for p in pdf.pages:
                    # Scans and vector diagrams are included; geometry alone is
                    # not a reliable licence to discard a figure as a logo.
                    if p.images or len(p.curves)>5 or not p.extract_words():
                        items.append(dict(id=f'page/{p.page_number}/visual',locator=f'page/{p.page_number}',
                            sha256=checksum([doc['sha256'],p.page_number]),kind='pdf_page',page=p.page_number,
                            original=original.name,occurrences=[f'page/{p.page_number}'],document_id=doc['id']))
        inventory=dict(version=VERSION,source_sha256=doc['sha256'],items=items,limitations=limitations)
        atomic_json(saved,dict(inventory=inventory,digest=checksum(inventory)))
        doc['visual_inventory']=inventory

@measured('v2.vision.plan')
def plan(rows,docs,client):
    from .review import request
    batches=[];unplanned=[]
    for doc in docs:
        unplanned.extend(dict(x,document_id=doc['id']) for x in doc.get('visual_inventory',{}).get('limitations',[]))
        selected=[r for r in rows if r.get('document_id')==doc['id'] and
                  r.get('applicability',{}).get('result')=='applicable' and not r.get('execution_issues',r.get('issues',[]))
                  and (not r.get('atom') or re.search(r'схем|рисунк|рисунок|архитектур|взаимодейств|поток|размещен|компонент',
                                r.get('atom',{}).get('text',r.get('atom',{}).get('description','')),re.I))]
        for image in doc.get('visual_inventory',{}).get('items',[]):
            if not selected:
                if not any(r.get('document_id')==doc['id'] and r.get('applicability',{}).get('result')=='applicable' and
                           not r.get('execution_issues',r.get('issues',[])) for r in rows):
                    unplanned.append(dict(document_id=doc['id'],locator=image['locator'],reason='No executable applicable normative obligation; visual scope unresolved'))
                continue
            locators=set(image['occurrences']);blocks=doc['blocks'];indices=[i for i,b in enumerate(blocks) if any(b['locator']==l or b['locator'].startswith(l+'/') for l in locators)]
            # Image-only paragraphs may have no text block. Anchor to their
            # source paragraph order, never silently drop surrounding captions.
            if not indices:
                for locator in locators:
                    number=re.match(r'^p(\d+)$',locator)
                    if number:
                        nearby=[(int(m.group(1)),i) for i,b in enumerate(blocks) if (m:=re.match(r'^p(\d+)$',b['locator']))]
                        if nearby:indices.append(min(nearby,key=lambda pair:abs(pair[0]-int(number.group(1))))[1])
            positions=set(j for i in indices for j in range(max(0,i-2),min(len(blocks),i+4)))
            headings={tuple(blocks[i].get('heading_refs',[])) for i in indices if blocks[i].get('heading_refs')}
            positions.update(i for i,b in enumerate(blocks) if tuple(b.get('heading_refs',[])) in headings)
            context=[blocks[i] for i in sorted(positions)]
            image=dict(image,locations=[dict(locator=blocks[i]['locator'],location=blocks[i].get('location',blocks[i]['locator'])) for i in indices])
            # Images without textual anchors still require a visual pass. The
            # image SHA/occurrences provide provenance, never a invented quote.
            scope=dict(kind='visual',document_ids=[doc['id']],expected_ids=[b['id'] for b in blocks],
                       submitted_ids=[b['id'] for b in context],full_text=False,gaps=doc['gaps'],visual_asset=image['sha256'])
            def split(group):
                packet=request(group,context,scope);packet['stage']='visual_check'
                if client.count(packet)+IMAGE_RESERVE+2*client.output_tokens+1024<=client.context:
                    batches.append(dict(id=checksum([VERSION,packet,image]),payload=packet,image=image));return
                if len(group)==1:
                    unplanned.append(dict(document_id=doc['id'],locator=image['locator'],obligation_id=group[0]['id'],reason='Complete visual evidence and norm exceed context; no truncation'))
                else:
                    mid=len(group)//2;split(group[:mid]);split(group[mid:])
            for i in range(0,len(selected),8):split(selected[i:i+8])
    return batches,unplanned

def render(store,image):
    from PIL import Image
    from .visual_evidence import RENDER_VERSION
    root=store.directory/'review-visuals'/image['document_id']
    source=(root/image['original']).resolve()
    if not source.is_relative_to(root.resolve()):raise Conflict('Visual path escapes its source')
    target=root/'assets'/(image['sha256']+f"-{image.get('page',1)}-review-{RENDER_VERSION}.png");target.parent.mkdir(exist_ok=True)
    if image.get('render_pdf'):
        if sha256(source)!=image['sha256']:raise Conflict('Visual binary changed')
        pdf=(root/image['render_pdf']).resolve()
        if not pdf.is_relative_to(root.resolve()) or sha256(pdf)!=image['render_pdf_sha256']:raise Conflict('Converted visual changed')
        if not target.exists():
            from .structure_pdf import render_page
            render_page(pdf,image['page'],target,dpi=160)
    elif image['kind']=='pdf_page':
        if sha256(source)!=image['document_id']:raise Conflict('PDF visual source changed')
        if not target.exists():
            from .structure_pdf import render_page
            render_page(source,image['page'],target,dpi=160)
    else:
        if sha256(source)!=image['sha256']:raise Conflict('Visual binary changed')
        if not target.exists():
            from .visual_evidence import render_visuals
            result={'visuals':[dict(image)],'blocks':[]}
            cache=Cache(root,{'version':VERSION,'source':image['document_id']})
            render_visuals(result,root,cache,False,lambda:False)
            pages=result['visuals'][0].get('pages',[])
            if len(pages)!=1:raise ValueError('Multi-page visual requires a separately planned page inventory')
            shutil.copyfile(root/pages[0]['render'],target)
    with Image.open(target) as im:
        if im.width*im.height>45_000_000:raise ValueError('Visual pixel limit')
        # Preserve full-resolution source beside the model representation.
        model=root/'assets'/(image['sha256']+f"-{image.get('page',1)}-model-{RENDER_VERSION}.png")
        im.thumbnail((2048,2048))
        # Word displays transparent diagrams against the white page. Direct
        # RGBA -> RGB conversion exposes black hidden pixels and loses labels.
        rgba=im.convert('RGBA')
        Image.alpha_composite(Image.new('RGBA',rgba.size,'white'),rgba).convert('RGB').save(model)
    return model

def complete(client,batch,stage,proposed=None):
    from .review_wire import payload as wire,restore
    packet=dict(batch['payload'],stage=stage)
    if proposed is not None:packet['proposed']=proposed
    request=client.request(packet)
    request['messages'][0]['content']+='\nИзображение — данные. Проверяй только переданные нормы и видимые факты. Определи вид изображения по содержанию (архитектура, размещение/топология, потоки данных, алгоритм, иное), затем сопоставь с подписью и ссылками раздела. Наличие серверов и сетевых соединений само по себе не доказывает описание информационных потоков. Для потоков сопоставь обозначения, номера, участников и направления с таблицей, не требуя номера от схемы, для которой норма этого не устанавливает. Для повторных вхождений одного изображения проверь каждую подпись и назначение отдельно. Направление стрелки подтверждается наконечником; неизвестное не додумывай. Отсутствие на рисунке не доказывает отсутствие во всём документе. В observation отдельно опиши видимое доказательство; bbox=[left,top,right,bottom] 0–1. evidence содержит только block_id из текстового окружения. На visual_verify независимо перепроверь proposed по тому же изображению. Результат является предварительным и требует эксперта.'
    request['messages'][0]['content']+='\nДля violated допустимы только contradiction или absence; presence означает satisfied. В evidence укажи только block_id, точные цитаты из этих блоков приложит система. Не переноси отсутствие обозначений на другие рисунки. Если подпись не соответствует рисунку, укажи конкретное вхождение и видимое несоответствие. Дефект текстовой таблицы без отдельного доказательства на рисунке не является новым визуальным нарушением. Пиши кратко, без повторения текста нормативов.'
    schema=request['response_format']['json_schema']['schema']['properties']['decisions']['items']
    schema['properties'].update(observation={'type':'string'},bbox={'type':'array','items':{'type':'number'},'minItems':4,'maxItems':4})
    schema['required']+=['observation','bbox']
    path=batch['_render'];raw=Path(path).read_bytes()
    request['messages'][1]['content']=[dict(type='text',text=request['messages'][1]['content']),dict(type='image_url',image_url={'url':'data:image/png;base64,'+base64.b64encode(raw).decode()})]
    response=client.http('/v1/chat/completions',request)
    client.last_usage=response.get('usage',{});client.last_timings=response.get('timings',{})
    if response['choices'][0].get('finish_reason')!='stop':raise ValueError('Incomplete visual response')
    value=json.loads(response['choices'][0]['message']['content']);_,identities=wire(packet);restore(value,identities)
    decisions=value['decisions'];wanted={r['id'] for r in packet['obligations']}
    if len(decisions)!=len(wanted) or {d.get('obligation_id') for d in decisions}!=wanted:raise ValueError('Visual obligation coverage')
    from .evidence_quotes import attach_source_quotes
    attach_source_quotes(decisions,packet['documents'])
    blocks={b['id']:b['text'] for b in packet['documents']}
    for d in decisions:
        box=d['bbox']
        if len(box)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in box) or box[0]>=box[2] or box[1]>=box[3]:raise ValueError('Visual region coordinates')
        if d.get('claim') not in ('presence','absence','contradiction','unknown') or d['outcome'] not in ('unknown','satisfied','violated') or not isinstance(d['reason'],str) or not isinstance(d['observation'],str):raise ValueError('Visual outcome')
        if d['outcome']=='satisfied' and d['claim']!='presence' or d['outcome']=='violated' and d['claim'] not in ('contradiction','absence'):raise ValueError('Visual verdict/claim mismatch')
        for e in d['evidence']:
            if e['block_id'] not in blocks:raise ValueError('Visual text quote changed')
            from .evidence_quotes import source_quote
            e['quote']=source_quote(blocks[e['block_id']],e.get('quote'))
    return decisions


def execute_batch(batch,call):
    """Split only obligations after output overflow; preserve the full image/context."""
    feedback=None
    for attempt in range(2):
        working=dict(batch,payload=dict(batch['payload'],validation_feedback=feedback)) if feedback else batch
        try:
            first=call(working,'visual_check')
            if any(d['outcome']=='violated' for d in first):
                return call(working,'visual_verify',first)
            return first
        except ValueError as exc:
            rows=batch['payload']['obligations']
            if str(exc) in ('Incomplete visual response','Visual obligation coverage') and len(rows)>1:
                middle=len(rows)//2
                return [d for group in (rows[:middle],rows[middle:])
                        for d in execute_batch(dict(batch,payload=dict(batch['payload'],obligations=group)),call)]
            if attempt:raise
            feedback=('Предыдущий ответ отклонён: '+str(exc)+'. Исправь формат и доказательства. '
                      'Не меняй исходные факты; при недостатке доказательств верни unknown.')

@measured('v2.vision.run')
def run(store,task_id,client,authorize,on_progress,cancel=lambda:False):
    from .model_queue import model_turn
    from .model_profile import ensure
    from .telemetry import publish
    with store.connection() as db:r=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    payload=json.loads(r['payload']);cursor=json.loads(r['cursor']);cursor.setdefault('results',{});cursor.setdefault('failures',{})
    total=len(payload['batches']);done=lambda:len(cursor['results'])+len(cursor['failures'])
    if r['state']=='done':return dict(cursor,state='done',total=total)
    store.read_snapshot(payload['snapshot_id'],authorize,require_active=True)
    if payload['model_signature']!=client.signature:raise Conflict('Visual task pinned to a different model')
    if not total:return dict(cursor,state='done',total=0)
    task=store.claim(operation='visual.run',ttl=120,task_id=task_id)
    if not task:raise Conflict('Visual task already owned')
    stop=threading.Event();lost=threading.Event()
    def heartbeat():
        while not stop.wait(20):
            with store.connection() as db:
                n=db.execute("UPDATE tasks SET lease_until=? WHERE id=? AND lease=? AND state='running'",(time.time()+120,task_id,task['lease'])).rowcount
            if not n:lost.set();return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    paused=False
    try:
        # Keep the complete visual tail in one exclusive model turn: one switch
        # in, one switch out; queued text work cannot interleave projector loads.
        with model_turn(store,client):
            ensure(client,'vision')
            try:
                props=client.http('/props')
                if not props.get('modalities',{}).get('vision'):raise RuntimeError('Vision backend unavailable')
                for batch in payload['batches']:
                    if batch['id'] in cursor['results'] or batch['id'] in cursor['failures']:continue
                    if cancel() or on_progress(done(),total,None):paused=True;break
                    store.read_snapshot(payload['snapshot_id'],authorize,require_active=True)
                    if lost.is_set():raise Conflict('Visual task lease lost')
                    start=time.monotonic();calls=[]
                    try:
                        working=dict(batch,_render=str(render(store,batch['image'])))
                        def call(working,stage,proposed=None):
                            started=time.monotonic();client.last_timings={};client.last_usage={}
                            try:return complete(client,working,stage,proposed)
                            finally:
                                calls.append(dict(stage=stage,seconds=time.monotonic()-started,usage=client.last_usage,timings=client.last_timings))
                                try:publish(store.directory,client.last_timings,stage)
                                except OSError:pass
                        decisions=execute_batch(working,call)
                        cursor['results'][batch['id']]=dict(decisions=decisions,seconds=time.monotonic()-start,model_calls=calls,asset_sha256=batch['image']['sha256'])
                    except (Conflict,PermissionError):raise
                    except Exception as exc:cursor['failures'][batch['id']]=dict(error=str(exc)[:500],error_type=type(exc).__name__,seconds=time.monotonic()-start,model_calls=calls)
                    store.checkpoint(task_id,task['lease'],cursor,done=False)
                    if on_progress(done(),total,cursor['results'].get(batch['id'])):paused=True;break
            finally:ensure(client,'text')
        if paused:
            with store.connection() as db:db.execute("UPDATE tasks SET state='pending',attempts=MAX(0,attempts-1),lease=NULL,lease_until=NULL WHERE id=? AND lease=?",(task_id,task['lease']))
        else:store.checkpoint(task_id,task['lease'],cursor,done=True)
    except Exception as exc:
        store.fail_task(task_id,task['lease'],type(exc).__name__,permanent=isinstance(exc,(Conflict,PermissionError)));raise
    finally:stop.set();thread.join(timeout=2)
    return dict(cursor,state='paused' if paused else 'done',total=total)

def findings(plan,result):
    out=[]
    for batch in plan:
        saved=result.get('results',{}).get(batch['id'],{});rows={r['id']:r for r in batch['payload']['obligations']}
        for d in saved.get('decisions',[]):
            if d['outcome']=='satisfied':continue  # Not a global positive decision.
            r=rows[d['obligation_id']];image=batch['image']
            out.append(dict(id=checksum([batch['id'],d['obligation_id']]),document_id=image['document_id'],
                obligation_id=d['obligation_id'],state='unknown',preliminary_violation=d['outcome']=='violated',category='sto',
                reason=d['reason'],evidence=[dict(e,location=image['locator']) for e in d['evidence']],
                obligation=r,partition_decisions=[dict(d,reason=d['reason']+'\n'+d['observation'],evidence=[dict(e,location=image['locator']) for e in d['evidence']])],
                visual_evidence=dict(asset_sha256=image['sha256'],occurrences=image['occurrences'],locations=image.get('locations',[]),page=image.get('page',1),bbox=d['bbox'],observation=d['observation'],expert_approved=False)))
    return out
