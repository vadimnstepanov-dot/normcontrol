"""Execute a portal-selected v2 check locally with an immutable knowledge snapshot."""
import hashlib
import json
import tempfile
import time
from pathlib import Path

from .review import ReviewRunner,corpus,release_records
from .review_client import LlamaClient
from .store import checksum,Conflict,NotReady


def execute(bridge,claim,download,client=None,experience_index=None):
    payload=claim['payload'];store=bridge.store
    if checksum(payload['snapshot'])!=payload['snapshot_digest']:raise Conflict('Portal snapshot digest')
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
    model=client or LlamaClient(__import__('os').environ['NORMCONTROL_LLM_ENDPOINT'],context=24576,output_tokens=2048,timeout=300,store=store)
    with tempfile.TemporaryDirectory(prefix='knowledge-check-') as temporary:
        paths=[]
        for index,declared in enumerate(payload['documents']):
            name=Path(declared['name']).name
            if not name or name!=declared['name'] or Path(name).suffix.casefold() not in ('.doc','.docx','.pdf'):
                raise ValueError('Document filename')
            path=Path(temporary)/(str(index)+'-'+name)
            digest=hashlib.sha256();size=0
            with download(claim['command_id'],payload['job_id'],declared['id'],claim['lease']) as source,path.open('wb') as target:
                while True:
                    data=source.read(1024*1024)
                    if not data:break
                    size+=len(data)
                    if size>declared['size'] or size>50*1024*1024:raise ValueError('Document length changed')
                    digest.update(data);target.write(data)
            if size!=declared['size'] or digest.hexdigest()!=declared['sha256']:raise Conflict('Document revision changed')
            paths.append(path)
        docs=corpus(paths)
        visual_enabled=payload.get('visual_version')=='visual-tail-v1'
        if visual_enabled:
            from .visual_tail import prepare
            prepare(paths,docs,store)
        if len(docs)!=len(paths):raise ValueError('Corpus count changed')
        for doc,declared in zip(docs,payload['documents']):doc['name']=declared['name']
        trace_enabled=payload.get('trace_version')=='package-trace-9.1.6'
        if trace_enabled:
            from .document_types import classify,declared_types,declared_stages
            types=set();stages=set()
            for item in selected:
                _,records=release_records(store,item['release_id'],authorize);types.update(declared_types(records));stages.update(declared_stages(records))
            classify(store,docs,sorted(types),model,stages)
        facts={};profiles={}
        for doc in docs:
            by_release={};source_ids=[]
            for item in selected:
                _,records=release_records(store,item['release_id'],authorize)
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
        def verify_fact(name,value,evidence):
            doc=next((d for d in docs if d['id']==evidence.get('source')),None)
            if not doc:return False
            if name=='selected_sources':return value==evidence.get('selected_sources')
            if name=='stage':return doc['classification'].get('stage')==value and any(
                b['locator']==evidence.get('locator') and evidence.get('quote') in b['text'] for b in doc['blocks'][:35])
            return name=='document_type' and doc['classification'].get('types',doc['classification']['type'])==value and any(
                b['locator']==evidence.get('locator') and b['text']==evidence.get('quote')
                for b in doc['blocks'][:35])
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
        trace_links=links_for(store,[r['release_id'] for r in selected],authorize) if trace_enabled else []
        from .model_queue import model_turn
        from .model_profile import ensure
        with model_turn(store,model):
            ensure(model,'text')
            trace_batches,_=trace_plan(trace_links,docs,facts,verify_fact,model) if trace_enabled else ([],[])
        trace_total=len(trace_batches)
        visual_total=0;visual_result=None;visual_batches=[];visual_unplanned=[]
        phase='text'
        progress_floor=0
        def progress(task_id,completed,total,decisions=None):
            completed=max(progress_floor,completed)
            preview=[dict(state='candidate',reason=str(d.get('reason',''))[:500],obligation_id=d.get('obligation_id'),
                          evidence=[{'quote':str(e.get('quote',''))[:500],'location':e.get('location','')}
                                    for e in d.get('evidence',[])[:2]])
                     for d in (decisions or []) if d.get('outcome')=='violated'][:5]
            response=bridge.transport('/worker/checks/progress/',dict(command_id=claim['command_id'],lease=claim['lease'],
                job_id=payload['job_id'],progress=dict(task_id=task_id,completed=completed,total=total+trace_total+visual_total,
                    percent=round(completed/max(1,total+trace_total+visual_total)*100,1),
                    eta_seconds=(round((time.monotonic()-started)/(completed-previous_completed)*max(0,total+trace_total+visual_total-completed)) if completed>previous_completed else previous_eta),
                    preview=preview,**({'stage':phase,'visual_total':visual_total} if visual_enabled else {}))))
            return response.get('pause_requested') is True or bridge.stop_event.is_set()
        runner=ReviewRunner(store,model,authorize,owner=payload['actor_id'],experience_selector=selector,on_checkpoint=progress)
        from .model_queue import model_turn
        with model_turn(store,model):
            from .model_profile import ensure
            ensure(model,'text')
            task_id=runner.create(paths,profiles,facts,verify_fact,job_id=payload['job_id'],prepared_docs=docs,
                experience_releases=[payload['experience_release_id']] if payload.get('experience_release_id') else [])
            if visual_enabled:
                from .visual_tail import plan
                with store.connection() as db:review_payload=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
                visual_batches,visual_unplanned=plan(review_payload['rows'],docs,model)
                visual_total=len(visual_batches)
                if visual_batches:
                    visual_task=store.enqueue('visual.run','visual.run:'+payload['job_id'],dict(job_id=payload['job_id'],batches=visual_batches,
                        snapshot_id=review_payload['snapshot_id'],model_signature=model.signature))
                    with store.connection() as db:old_visual=json.loads(db.execute('SELECT cursor FROM tasks WHERE id=?',(visual_task,)).fetchone()[0])
                    visual_done=len(old_visual.get('results',{}))+len(old_visual.get('failures',{}))
                    if visual_done:progress_floor=len(review_payload['batches'])+trace_total+visual_done
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
        if trace_enabled and state!='paused':
            phase='trace'
            n=report['progress']['completed'];norm_total=report['progress']['total']
            def trace_progress(done,total):return progress(task_id,n+done,norm_total)
            trace_report=trace_run(store,payload['job_id'],trace_links,docs,facts,verify_fact,model,authorize,trace_progress)
            if trace_report['state']=='paused':state='paused'
            elif trace_report['counts'].get('unknown') or trace_report['errors']:state='partial'
            if state!='paused':progress(task_id,n+trace_total,norm_total)
        if visual_enabled and state!='paused' and visual_batches:
            phase='vision'
            from .visual_tail import run
            base_done=report['progress']['completed']+trace_total
            norm_total=report['progress']['total']
            # The text/trace stages are durable before projector activation.
            visual_result=run(store,visual_task,model,authorize,
                lambda done,total,result:progress(task_id,base_done+done,norm_total,(result or {}).get('decisions',[])),cancel=bridge.stop_event.is_set)
            if visual_result['state']=='paused':state='paused'
            elif visual_result.get('failures') or visual_unplanned:state='partial'
        if visual_enabled and visual_unplanned and state!='paused':state='partial'
        findings=[] if state=='paused' else [d for d in report['decisions'] if d['state'] in ('violated','unknown')]
        if visual_result and state!='paused':
            from .visual_tail import findings as visual_findings
            visual_rows=visual_findings(visual_batches,visual_result);findings.extend(visual_rows)
            if visual_rows:state='partial' if state=='completed' else state
        if trace_report and state!='paused':findings.extend(dict(r,category='traceability') for r in trace_report['rows'])
        hashes=[]
        for i in range(0,len(findings),10):
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
            traceability={k:v for k,v in (trace_report or {}).items() if k!='rows'},
            snapshot=report['snapshot'],experience_used=report['experience_used'],
            planning=dict(report.get('planning',{}),**({'visual_tasks':visual_total,'order':['text','trace','vision'],
                'visual_assets':sum(len(d.get('visual_inventory',{}).get('items',[])) for d in docs),'visual_unplanned':visual_unplanned} if visual_enabled else {})),
            visuals=dict(enabled=visual_enabled,total=visual_total,completed=len((visual_result or {}).get('results',{})),
                errors=len(visual_errors),unplanned=visual_unplanned,model_calls=[c for v in (visual_result or {}).get('results',{}).values() for c in v.get('model_calls',[])]),performance=report.get('performance',{}))
        return store.remember_result(claim['command_id'],'review.execute',payload,result)
