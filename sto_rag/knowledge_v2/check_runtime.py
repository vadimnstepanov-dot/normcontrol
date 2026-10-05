"""Execute a portal-selected v2 check locally with an immutable knowledge snapshot."""
import hashlib
from .performance import profiled, span, milestone
import json
import tempfile
import time
from pathlib import Path

from .review import ReviewRunner,corpus,release_records
from .review_client import LlamaClient
from .store import checksum,Conflict,NotReady
from .lease_heartbeat import check_review_lease
from pipeline import QueuePaused


from .check_log import logged,emit

class PreparationPaused(Exception):pass

def review_wire_version(store,job_id):
    from .review_wire import COMPACT_NORMS_VERSION,VERSION
    with store.connection() as db:
        prior=db.execute("SELECT json_extract(payload,'$.snapshot.versions.transport') FROM tasks WHERE dedupe_key=?",('review.run:'+job_id,)).fetchone()
    return (prior[0] or VERSION) if prior else __import__('os').environ.get('NORMCONTROL_REVIEW_WIRE',COMPACT_NORMS_VERSION)

@profiled('v2')
@logged
def execute(bridge,claim,download,client=None,experience_index=None,*,lease_cancel=None):
    try:return _execute(bridge,claim,download,client,experience_index,lease_cancel=lease_cancel)
    except (PreparationPaused,QueuePaused) as interrupted:
        payload=claim['payload'];store=bridge.store
        with store.connection() as db:
            saved=db.execute("SELECT id FROM tasks WHERE operation='review.run' AND dedupe_key=?",('review.run:'+payload['job_id'],)).fetchone()
            if saved and isinstance(interrupted,PreparationPaused):raise
            if saved:
                db.execute("UPDATE tasks SET state='pending',attempts=MAX(0,attempts-1),lease=NULL,lease_until=NULL WHERE id=? AND state='running'",(saved[0],))
                db.execute('INSERT INTO review_controls VALUES(?,1) ON CONFLICT(task_id) DO UPDATE SET paused=1',(saved[0],))
        result=dict(kind='review.execute.done',set_id=payload['set_id'],job_id=payload['job_id'],
            task_id=saved[0] if saved else None,state='paused',finding_count=0,findings_digest=checksum([]),
            limitations=['Ожидание ресурсов остановлено. Сохранённые ответы и контрольная точка сохранены.'])
        return store.remember_result(claim['command_id'],'review.execute',payload,result)

def _execute(bridge,claim,download,client=None,experience_index=None,*,lease_cancel=None):
    check_review_lease(lease_cancel)
    payload=claim['payload'];store=bridge.store
    last_progress=None;last_wait_notice=-float('inf')
    def send_progress(value):
        nonlocal last_progress
        check_review_lease(lease_cancel)
        last_progress=dict(value)
        return bridge.transport('/worker/checks/progress/',dict(command_id=claim['command_id'],lease=claim['lease'],job_id=payload['job_id'],progress=value))
    def queue_wait(ticket):
        nonlocal last_wait_notice
        check_review_lease(lease_cancel)
        if bridge.stop_event.is_set():return True
        if time.monotonic()-last_wait_notice<5:return False
        last_wait_notice=time.monotonic()
        if not last_progress:return False
        waiting='Ожидание памяти' if ticket.get('waiting_reason')=='ram' else 'Ожидание свободного процессора/модели'
        if ticket.get('waiting_reason')=='ram':waiting+=f": доступно {ticket['available_mb']} МиБ; резерв {ticket['reserve_mb']} МиБ; задача {ticket['ram_mb']} МиБ"
        return send_progress(dict(last_progress,operation=waiting)).get('pause_requested') is True
    if checksum(payload['snapshot'])!=payload['snapshot_digest']:raise Conflict('Portal snapshot digest')
    def preparing(stage,operation,**details):
        nonlocal last_progress
        check_review_lease(lease_cancel)
        with store.connection() as db:
            saved=db.execute("SELECT id,cursor,json_array_length(payload,'$.batches') FROM tasks WHERE operation='review.run' AND dedupe_key=?",('review.run:'+payload['job_id'],)).fetchone()
            if saved:
                if last_progress is None:
                    cursor=json.loads(saved[1]);done=len(cursor.get('results',{}))+len(cursor.get('failures',{}));total=saved[2] or 0
                    last_progress=dict(task_id=saved[0],completed=done,total=total,percent=round(100*done/max(1,total),1),eta_seconds=None,preview=[],stage='text')
                return False
        response=send_progress(dict(task_id=None,completed=0,total=None,percent=0,eta_seconds=None,
                preview=[],stage=stage,operation=operation,**details))
        emit('preparation',dict(stage=stage,operation=operation))
        # Resume must preserve the durable plan; preparation updates apply only before its creation.
        return response.get('pause_requested') is True
    selected=payload['snapshot']['releases']
    if sorted(payload['set_ids'])!=sorted(x['set_id'] for x in selected):raise Conflict('Set selection changed')
    authorize=bridge.authorization_for(payload['actor_id'])
    for item in selected:
        manifest,_=release_records(store,item['release_id'],authorize)
        if manifest['set_id']!=item['set_id'] or checksum(manifest)!=item['manifest_hash']:
            raise Conflict('Normative release changed')
    if payload.get('experience_release_id'):
        manifest,_=release_records(store,payload['experience_release_id'],authorize)
        if manifest['set_id']!=payload['experience_set_id']:raise Conflict('Experience release changed')
    pipeline=payload.get('batch_id') if payload.get('pipeline_version')=='pipeline-v1' else None
    with tempfile.TemporaryDirectory(prefix='knowledge-check-') as temporary:
        preparing('download','Получение документов и проверка контрольных сумм')
        paths=[]
        for index,declared in enumerate(payload['documents']):
            name=Path(declared['name']).name
            if not name or name!=declared['name'] or Path(name).suffix.casefold() not in ('.doc','.docx','.pdf'):
                raise ValueError('Document filename')
            path=Path(temporary)/(str(index)+'-'+name)
            digest=hashlib.sha256();size=0
            with span('v2.download'), download(claim['command_id'],payload['job_id'],declared['id'],claim['lease']) as source,path.open('wb') as target:
                while True:
                    data=source.read(1024*1024)
                    if not data:break
                    size+=len(data)
                    if size>declared['size'] or size>50*1024*1024:raise ValueError('Document length changed')
                    digest.update(data);target.write(data)
            if size!=declared['size'] or digest.hexdigest()!=declared['sha256']:raise Conflict('Document revision changed')
            paths.append(path)
        preparing('parse','Чтение структуры, текста и таблиц')
        if pipeline:
            import os,urllib.request
            endpoint=os.environ['NORMCONTROL_LLM_ENDPOINT'].rstrip('/')
            headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ.get('NORMCONTROL_LLM_API_KEY','')}
            def shared_request(route,value):
                req=urllib.request.Request(endpoint+route,data=json.dumps({'pipeline':pipeline,**value}).encode(),headers=headers)
                with urllib.request.urlopen(req,timeout=15) as response:return json.load(response)
            from .preparation_state import PreparationState
            preparation_state=PreparationState()
            while True:
                artifact=shared_request('/pipeline/artifact',{'metadata':True})
                if artifact.get('ready'):break
                status=shared_request('/pipeline/status',{})
                if preparation_state.paused(status):raise PreparationPaused()
                if preparing('parse','Общая подготовка документов на CPU') or bridge.stop_event.is_set():raise PreparationPaused()
                time.sleep(2)
            from types import SimpleNamespace
            from pipeline import remote_turn
            admission=SimpleNamespace(pipeline_id=pipeline,pipeline_endpoint=endpoint,timeout=300,_queue_wait=queue_wait)
            reserve=max(512,256+int(artifact['bytes'])*6//1048576)
            with remote_turn(admission,'normative_material','cpu',ram_mb=reserve):
                artifact=shared_request('/pipeline/artifact',{})
                if artifact.get('version')!='pipeline-v1' or artifact.get('pipeline')!=pipeline:raise Conflict('Shared preparation version differs')
                lookup={d['sha256']:d for d in artifact['documents']}
                if set(lookup)!={d['sha256'] for d in payload['documents']}:raise Conflict('Shared corpus source identity differs')
                docs=[lookup[d['sha256']] for d in payload['documents']]
            shared_request('/pipeline/phase',{'stage':'normative','state':'running'})
        else:docs=corpus(paths)
        # Existing cursors retain their pinned transport; compact gaps apply to
        # new plans only, so a resume never silently changes model input.
        wire_version=review_wire_version(store,payload['job_id'])
        model=client or LlamaClient(__import__('os').environ['NORMCONTROL_LLM_ENDPOINT'],context=49152,output_tokens=4096,timeout=300,store=store,queue_wait=queue_wait,wire_version=wire_version,**({'pipeline_id':pipeline} if pipeline else {}))
        with store.connection() as db:
            prior=db.execute("SELECT json_extract(payload,'$.snapshot.versions.shared_evidence') FROM tasks WHERE dedupe_key=?",('review.run:'+payload['job_id'],)).fetchone()
        model.shared_evidence=(bool(prior[0]) if prior else __import__('os').environ.get('NORMCONTROL_SHARED_EVIDENCE','1')=='1') if client is None else getattr(client,'shared_evidence',False)
        model._queue_wait=queue_wait
        visual_enabled=payload.get('visual_version')=='visual-tail-v1'
        if visual_enabled:
            from .visual_tail import prepare
            selected_visual=[(p,d) for p,d,declared in zip(paths,docs,payload['documents']) if declared.get('review_role')!='approved_reference']
            prepare([p for p,d in selected_visual],[d for p,d in selected_visual],store)
        if len(docs)!=len(paths):raise ValueError('Corpus count changed')
        for doc,declared in zip(docs,payload['documents']):
            doc['name']=declared['name'];doc['review_role']=declared.get('review_role','unassigned')
        trace_enabled=payload.get('trace_version')=='package-trace-9.1.6'
        if trace_enabled:
            from .document_types import classify,declared_types,declared_stages
            types=set();stages=set()
            for item in selected:
                _,records=release_records(store,item['release_id'],authorize);types.update(declared_types(records));stages.update(declared_stages(records))
            preparing('classify','Определение вида документа и стадии')
            classify(store,docs,sorted(types),model,stages)
        preparing('facts','Извлечение фактов для определения применимости нормативов')
        facts={};profiles={}
        from .document_facts import requested,extract,verified,definitions
        fact_names={};fact_basis=[]
        for doc in docs:
            by_release={};source_ids=[]
            for item in selected:
                _,records=release_records(store,item['release_id'],authorize)
                for name,values in requested(records).items():fact_names.setdefault(name,set()).update(values)
                fact_basis.extend(definitions(records,requested(records)))
                profile_ids=[rid for (rid,version),r in records.items() if r['kind']=='profile']
                policy=next((r['payload'] for r in records.values() if r['kind']=='publication_policy'),None)
                if policy and policy['profiles']:profile_ids=list(policy['profiles'].values())
                if not profile_ids:raise NotReady('Release contains no profiles')
                by_release[item['release_id']]=profile_ids
                source_ids.extend(rid for (rid,version),r in records.items() if r['kind']=='source_revision')
            profiles[doc['id']]=by_release
            facts[doc['id']]={'selected_sources':dict(value=sorted(set(source_ids)),complete=True,
                evidence=[dict(source=doc['id'],locator='selected-releases',selected_sources=sorted(set(source_ids)))])}
            title=doc.get('classification',{}).get('type')
            if isinstance(title,str) and title!='unknown':
                classification=doc['classification']
                if classification.get('method')=='evidenced_title':
                    e=classification['evidence'][0]
                    matching=next((b for b in doc['blocks'][:35] if b['id']==e['block_id'] and e['quote'] in b['text']),None)
                else:matching=next((b for b in doc['blocks'][:35] if title.casefold() in b['text'].casefold()),None)
                if matching:facts[doc['id']]['document_type']=dict(value=classification.get('types',title),complete=True,evidence=[dict(source=doc['id'],
                    locator=matching['locator'],quote=matching['text'])])
            if doc['classification'].get('stage'):
                ev=[]
                for e in doc['classification']['stage_evidence']:
                    b=next(x for x in doc['blocks'] if x['id']==e['block_id'])
                    ev.append(dict(source=doc['id'],locator=b['locator'],quote=e['quote']))
                facts[doc['id']]['stage']=dict(value=doc['classification']['stage'],evidence=ev)
            facts[doc['id']].update(extract(store,doc,{k:sorted(v) for k,v in fact_names.items()},model,fact_basis))
        def verify_fact(name,value,evidence):
            doc=next((d for d in docs if d['id']==evidence.get('source')),None)
            if not doc:return False
            if verified(facts[doc['id']],name,value,evidence):return any(
                b['locator']==evidence['locator'] and evidence['quote'] in b['text'] for b in doc['blocks'])
            if name=='selected_sources':return value==evidence.get('selected_sources')
            if name=='stage':return doc['classification'].get('stage')==value and any(
                b['locator']==evidence.get('locator') and evidence.get('quote') in b['text'] for b in doc['blocks'][:35])
            return name=='document_type' and doc['classification'].get('types',doc['classification']['type'])==value and any(
                b['locator']==evidence.get('locator') and b['text']==evidence.get('quote')
                for b in doc['blocks'][:35])
        template_comparison=None
        if payload.get('template_version')=='sto-template-v1':
            # Existing jobs retain their pinned plan. New jobs compare templates
            # before any normative task, without an extra inference pass.
            with store.connection() as db:
                old_template=db.execute("SELECT json_extract(payload,'$.template_comparison') FROM tasks WHERE operation='review.run' AND dedupe_key=?",('review.run:'+payload['job_id'],)).fetchone()
            if old_template:
                template_comparison=json.loads(old_template[0]) if old_template[0] else None
            else:
                from .template_check import build,TITLE
                preparing('template',TITLE)
                template_comparison=build(store,[d for d in docs if d.get('review_role')!='approved_reference'],[release_records(store,x['release_id'],authorize)[1] for x in selected],facts)
                preparing('template','Структура сопоставлена; содержание будет дополнено результатами нормативной проверки',template_comparison=template_comparison)
                emit('template_comparison',template_comparison)
        selector=None
        if payload.get('experience_release_id'):
            from .embedding import RemoteEncoder
            from .search import QdrantIndex,HybridSearch
            from .experience import ExperienceSelector
            import os
            encoder,vector=experience_index or (RemoteEncoder(os.getenv('KNOWLEDGE_EMBEDDING_URL','http://embeddings:8109')),
                QdrantIndex(os.getenv('KNOWLEDGE_QDRANT_URL','http://qdrant:6333')))
            selector=ExperienceSelector(HybridSearch(store,encoder,vector,authorize),payload['experience_scope_id'],verify_fact)
        started=time.monotonic()
        previous_completed=0;previous_eta=None
        trace_report=None
        trace_enabled=payload.get('trace_version')=='package-trace-9.1.6'
        from .trace import links_for,plan as trace_plan,run as trace_run
        # Resuming a saved plan never inherits a later deployment's opt-in.
        with store.connection() as db:
            old_plan=db.execute("SELECT json_extract(payload,'$.include_candidates') FROM tasks WHERE operation='review.run' AND dedupe_key=?",('review.run:'+payload['job_id'],)).fetchone()
        include_candidates=(bool(old_plan[0]) if old_plan else
            __import__('os').getenv('KNOWLEDGE_INCLUDE_NORMATIVE_CANDIDATES','0')=='1')
        trace_links=links_for(store,[r['release_id'] for r in selected],authorize,include_candidates=include_candidates) if trace_enabled else []
        from .model_queue import planning_turn as model_turn
        from .model_profile import ensure
        with model_turn(store,model):
            if not pipeline:ensure(model,'text')
            trace_batches,_=trace_plan(trace_links,docs,facts,verify_fact,model) if trace_enabled else ([],[])
        trace_total=len(trace_batches)
        visual_total=0;visual_result=None;visual_batches=[];visual_unplanned=[]
        phase='text'
        progress_floor=0
        adaptive_operation=''
        def progress(task_id,completed,total,decisions=None):
            if completed:milestone('first_completed_batch_seconds')
            if any(d.get('outcome')=='violated' for d in (decisions or [])):milestone('first_violation_preview_seconds')
            from .check_log import active
            journal=active.get()
            if journal:
                emit('checkpoint',dict(task_id=task_id,completed=completed,total=total,decisions=decisions))
            completed=max(progress_floor,completed)
            preview=[dict(state='candidate',reason=str(d.get('reason',''))[:500],obligation_id=d.get('obligation_id'),
                          evidence=[{'quote':str(e.get('quote',''))[:500],'location':e.get('location','')}
                                    for e in d.get('evidence',[])[:2]])
                     for d in (decisions or []) if d.get('outcome')=='violated'][:5]
            response=send_progress(dict(task_id=task_id,completed=completed,total=total+trace_total+visual_total,
                    percent=round(completed/max(1,total+trace_total+visual_total)*100,1),
                    eta_seconds=(round((time.monotonic()-started)/(completed-previous_completed)*max(0,total+trace_total+visual_total-completed)) if completed>previous_completed else previous_eta),
                    preview=preview,**({'stage':'vision' if phase=='waiting_native' else phase,'visual_total':visual_total} if visual_enabled else {}),
                    **({'operation':'Ожидание завершения текстовых проверок'} if phase=='waiting_native' else {})))
            paused=response.get('pause_requested') is True or bridge.stop_event.is_set()
            if journal and not paused:
                try:journal.deliver(bridge,claim,max_chunks=1)
                except Exception:journal.append('log_delivery_error',dict(message='Будет повторено из сохранённого журнала'))
            return paused
        runner=ReviewRunner(store,model,authorize,owner=payload['actor_id'],experience_selector=selector,on_checkpoint=progress)
        def adaptive_progress(done,total):
            nonlocal adaptive_operation
            adaptive_operation=f'Дополнительная полная проверка: фрагмент {done+1} из {total}'
            current=getattr(runner,'_current_progress',None)
            if current:
                response=send_progress(dict(task_id=current[0],completed=current[1],total=current[2]+trace_total+visual_total,
                    percent=round(current[1]/max(1,current[2]+trace_total+visual_total)*100,1),eta_seconds=None,
                    preview=[],stage='text',operation=adaptive_operation))
                return response.get('pause_requested') is True or bridge.stop_event.is_set()
            return False
        runner.on_adaptive=adaptive_progress
        from .model_queue import planning_turn as model_turn
        with model_turn(store,model):
            from .model_profile import ensure
            if not pipeline:ensure(model,'text')
            preparing('plan','Подбор требований и точный расчёт пакетов по контекстному окну')
            probes=0;last_probe=0
            def plan_probe():
                nonlocal probes,last_probe
                probes+=1
                check_review_lease(lease_cancel)
                if bridge.stop_event.is_set():raise PreparationPaused()
                if time.monotonic()-last_probe<10:return
                last_probe=time.monotonic()
                if preparing('plan',f'Подбор пакетов: проверено вариантов — {probes}'):
                    raise PreparationPaused()
            previous_probe=getattr(model,'on_plan_probe',None)
            model.on_plan_probe=plan_probe
            try:
                task_id=runner.create(paths,profiles,facts,verify_fact,job_id=payload['job_id'],prepared_docs=docs,
                    experience_releases=[payload['experience_release_id']] if payload.get('experience_release_id') else [],template_comparison=template_comparison,include_candidates=include_candidates)
            finally:model.on_plan_probe=previous_probe
            with store.connection() as db:
                from .plan_storage import unpack
                planned=unpack(json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()[0]))
            from .check_log import compact_plan
            emit('plan',compact_plan(planned))
            if visual_enabled:
                from .visual_tail import plan
                review_payload=planned
                visual_batches,visual_unplanned=plan(review_payload['rows'],docs,model)
                visual_total=len(visual_batches)
                if visual_batches:
                    visual_task=store.enqueue('visual.run','visual.run:'+payload['job_id'],dict(job_id=payload['job_id'],batches=visual_batches,
                        snapshot_id=review_payload['snapshot_id'],model_signature=model.signature))
                    with store.connection() as db:old_visual=json.loads(db.execute('SELECT cursor FROM tasks WHERE id=?',(visual_task,)).fetchone()[0])
                    visual_done=len(old_visual.get('results',{}))+len(old_visual.get('failures',{}))
                    if visual_done:progress_floor=len(review_payload['batches'])+trace_total+visual_done
        # Both visual preparation and the journal refer to this same plan.
        # Release it before report()/run_once() load their own durable task.
        if visual_enabled:del review_payload
        del planned
        previous=runner.report(task_id)
        # Initial token planning is a finished preparation stage, not recurring
        # per-task latency. A resume estimates only newly completed work.
        started=time.monotonic();previous_completed=previous['progress']['completed'];previous_eta=previous['progress'].get('eta_seconds')
        if trace_enabled:
            with store.connection() as db:
                old=db.execute("SELECT cursor FROM tasks WHERE operation='trace.run' AND json_extract(payload,'$.job_id')=? ORDER BY created DESC LIMIT 1",(payload['job_id'],)).fetchone()
            if old:
                cursor=json.loads(old['cursor']);progress_floor=max(progress_floor,previous['progress']['completed']+len(cursor.get('results',{}))+len(cursor.get('failures',{})))
        # A resumed command must report the durable cursor, not rewind portal progress.
        runner.pause(task_id,False)
        if progress(task_id,previous['progress']['completed'],previous['progress']['total']):
            runner.pause(task_id,True)
        check_review_lease(lease_cancel)
        runner.run_once(task_id)
        report=runner.report(task_id)
        if report['state']=='failed':
            raise NotReady('Local review failed; retry from its saved cursor after resolving the cause: '+report['task_error'][:300])
        if report['state'] in ('pending','running'):
            state='paused'
        elif report['state']=='done' and report['normative_coverage']['resolved_percent']==100 and not report['errors']:
            state='completed'
        else:state='partial'
        if state=='completed' and report.get('normative_selection'):state='partial'
        if pipeline:
            for stage in ('normative','verify_normative'):shared_request('/pipeline/phase',{'stage':stage,'state':'paused' if state=='paused' else 'done'})
        if trace_enabled and state!='paused':
            phase='trace'
            if pipeline:model.pipeline_stage='trace';shared_request('/pipeline/phase',{'stage':'trace','state':'running'})
            n=report['progress']['completed'];norm_total=report['progress']['total']
            def trace_progress(done,total):return progress(task_id,n+done,norm_total)
            trace_report=trace_run(store,payload['job_id'],trace_links,docs,facts,verify_fact,model,authorize,trace_progress)
            if trace_report['state']=='paused':state='paused'
            elif trace_report['counts'].get('unknown') or trace_report['errors']:state='partial'
            if state!='paused':progress(task_id,n+trace_total,norm_total)
        if pipeline:shared_request('/pipeline/phase',{'stage':'trace','state':'paused' if state=='paused' else 'done'})
        if pipeline and visual_enabled and visual_batches and state!='paused':
            # The native job pins the text profile. Wait without holding RAM/GPU
            # tickets before loading Vision; text work and CPU prep still overlap.
            phase='waiting_native'
            while True:
                phases={p['stage']:p['state'] for p in shared_request('/pipeline/status',{})['phases']}
                if phases.get('verify_native')=='done':break
                if phases.get('verify_native') in ('failed','paused') or progress(task_id,report['progress']['completed']+trace_total,report['progress']['total']):
                    state='paused';break
                time.sleep(5)
        if visual_enabled and state!='paused' and visual_batches:
            phase='vision'
            if pipeline:model.pipeline_stage='vision';shared_request('/pipeline/phase',{'stage':'vision','state':'running'})
            from .visual_tail import run
            base_done=report['progress']['completed']+trace_total
            norm_total=report['progress']['total']
            # The text/trace stages are durable before projector activation.
            visual_result=run(store,visual_task,model,authorize,
                lambda done,total,result:progress(task_id,base_done+done,norm_total,(result or {}).get('decisions',[])),cancel=bridge.stop_event.is_set)
            if visual_result['state']=='paused':state='paused'
            elif visual_result.get('failures') or visual_unplanned:state='partial'
        if visual_enabled and visual_unplanned and state!='paused':state='partial'
        if pipeline:shared_request('/pipeline/phase',{'stage':'vision','state':'paused' if state=='paused' else 'done'})
        findings=[] if state=='paused' else [d for d in report['decisions'] if d['state'] in ('violated','unknown')]
        if state!='paused':findings.extend(report.get('scope_findings',[]))
        if visual_result and state!='paused':
            from .visual_tail import findings as visual_findings
            visual_rows=visual_findings(visual_batches,visual_result);findings.extend(visual_rows)
            if visual_rows:state='partial' if state=='completed' else state
        if trace_report and state!='paused':findings.extend(dict(r,category='traceability') for r in trace_report['rows'])
        hashes=[]
        for i in range(0,len(findings),10):
            check_review_lease(lease_cancel)
            entries=findings[i:i+10];entry_hash=checksum(entries)
            answer=bridge.transport('/worker/checks/findings/',dict(command_id=claim['command_id'],lease=claim['lease'],
                job_id=payload['job_id'],sequence=i//10,entries=entries,digest=entry_hash))
            if answer.get('accepted') is not True:raise NotReady('Finding chunk unacknowledged')
            hashes.append(entry_hash)
        final_progress=dict(report['progress'])
        if trace_report:
            with store.connection() as db:
                trace_cursor=json.loads(db.execute('SELECT cursor FROM tasks WHERE id=?',(trace_report['task_id'],)).fetchone()[0])
            done=report['progress']['completed']+len(trace_cursor.get('results',{}))+len(trace_cursor.get('failures',{}))
            total=report['progress']['total']+trace_total
            final_progress.update(completed=done,total=total,percent=round(100*done/max(1,total),1),eta_seconds=0 if state!='paused' else None)
        if visual_enabled:
            done=final_progress['completed']+len((visual_result or {}).get('results',{}))+len((visual_result or {}).get('failures',{}))
            total=report['progress']['total']+trace_total+visual_total
            final_progress.update(completed=done,total=total,percent=round(100*done/max(1,total),1),eta_seconds=0 if state!='paused' else None,
                stage='vision' if state=='paused' and phase=='vision' else phase,visual_total=visual_total)
        visual_errors={'visual:'+k:v for k,v in (visual_result or {}).get('failures',{}).items()}
        result=dict(kind='review.execute.done',set_id=payload['set_id'],job_id=payload['job_id'],task_id=task_id,
            state=state,finding_count=len(findings),findings_digest=checksum(hashes),
            coverage=report['normative_coverage'],violation_count=report['violation_count']+(trace_report or {}).get('counts',{}).get('violated',0),
            progress=final_progress,errors=dict(report['errors'],**{'trace:'+k:v for k,v in (trace_report or {}).get('errors',{}).items()},**visual_errors),
            limitations=report['limitations']+(trace_report or {}).get('limitations',[])+
                ([str(x['reason']) for x in visual_unplanned]+['Visual findings are preliminary source-linked observations, not expert-approved violations.'] if visual_enabled and (visual_batches or visual_unplanned) else []),
            normative_selection=report.get('normative_selection',[]),
            candidate_analysis=report.get('candidate_analysis',{}),
            template_comparison=report.get('template_comparison'),
            traceability={k:v for k,v in (trace_report or {}).items() if k!='rows'},
            snapshot=report['snapshot'],experience_used=report['experience_used'],
            planning=dict(report.get('planning',{}),**({'visual_tasks':visual_total,'order':['text','trace','vision'],
                'visual_assets':sum(len(d.get('visual_inventory',{}).get('items',[])) for d in docs),'visual_unplanned':visual_unplanned} if visual_enabled else {})),
            visuals=dict(enabled=visual_enabled,total=visual_total,completed=len((visual_result or {}).get('results',{})),
                errors=len(visual_errors),unplanned=visual_unplanned,model_calls=[c for v in (visual_result or {}).get('results',{}).values() for c in v.get('model_calls',[])]),performance=report.get('performance',{}))
        with span('v2.persist_report'):
            return store.remember_result(claim['command_id'],'review.execute',payload,result)
