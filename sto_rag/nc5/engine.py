import copy
import json
import re
import threading
import time
import shutil
from collections import Counter,defaultdict
from pathlib import Path
from . import VERSION
from .common import DATA,ROOT,config,digest,read,write
from .catalog import load_catalog
from .documents import parse,relationships,semantic_groups,coalesce_groups,split_blocks,compact_block
from .checks import validate_finding,validate_evidence,deterministic,ABSENCE,restore_non_normative_id,route_finding
from .model import Client,BudgetError,OutputError,POLICY,SCHEMA,output_budget
from .search import Index,terms
from .store import Store,LRU
from .runtime import Lease,BusyError,SleepInhibitor,implementation_hash
from .evidence_context import rule_evidence,reference_payloads,reference_inventory,verification_context,restore_evidence_addresses,symbol_context,local_candidates,link_candidates

STAGES=['language','logic','cross','inter','sto','verify']

def response_cache_key(client,payload):
    # Cache the model response by its actual request, not by unrelated UI/report
    # code. Findings, quotations, math and coverage are validated again on replay.
    return digest([VERSION,client.signature,client.request(payload)])

class Engine:
    def __init__(self,cfg=None,store=None):
        self.config=cfg or config();self.store=store or Store();self.client=Client(self.config);self.lock=threading.Lock();self.cache=LRU(self.config['ram_bytes']);self.thread=None
    def create(self,paths,options=None,owner='local'):
        if not 1<=len(paths)<=20:raise ValueError('Нужен комплект из 1–20 документов')
        cat=load_catalog();data={'paths':[str(Path(p).resolve()) for p in paths],'options':{**self.config,**(options or {})},'owner':owner,'version':VERSION,'implementation':implementation_hash(),'catalog':cat['version'],'derived':[], 'limitations':[]}
        jid=self.store.create(data)
        return jid
    def material(self,jid):
        value=self.cache.get(jid)
        if value is None:value=read(DATA/'jobs'/jid/'documents.json');self.cache.put(jid,value)
        return value
    def prepare(self,jid):
        job=self.store.job(jid);data=job['data'];cat=load_catalog(data['catalog']);probe=self.client.probe();docs=[]
        from .conversion import prepare_word,checksum
        for path in data['paths']:
            source=Path(path).resolve(strict=True);prepared=prepare_word(source,data['options']);doc=parse(prepared,cat)
            doc['source_path']=str(source);doc['source_sha256']=checksum(source);doc['storage_name']=prepared.name
            if source.suffix.casefold()=='.doc':
                converted_name=doc['name'];doc['name']=source.name;doc['source_format']='doc'
                for block in doc['blocks']:
                    block['file']=source.name
                    if block.get('address','').startswith(converted_name+' → '):block['address']=source.name+block['address'][len(converted_name):]
                for heading in doc['headings']:
                    if heading.get('address','').startswith(converted_name+' → '):heading['address']=source.name+heading['address'][len(converted_name):]
            docs.append(doc)
        for doc in docs:
            snapshot=DATA/'jobs'/jid/'sources'/doc.get('storage_name',doc['name']);snapshot.parent.mkdir(parents=True,exist_ok=True)
            if snapshot.exists() and digest(snapshot.read_bytes())!=doc['sha256']:snapshot=snapshot.with_name(doc['id']+'-'+doc['name'])
            if not snapshot.exists():shutil.copyfile(doc['path'],snapshot)
            if digest(snapshot.read_bytes())!=doc['sha256']:raise ValueError('Документ изменился во время подготовки: '+doc['name'])
            doc['original_path']=doc['path'];doc['path']=str(snapshot)
        write(DATA/'jobs'/jid/'documents.json',docs);self.cache.put(jid,docs)
        from .planning import document_registry
        registries={doc['id']:document_registry(doc) for doc in docs}
        write(DATA/'jobs'/jid/'analysis-registry.json',registries)
        data.update({'model':probe,'documents':[{k:d[k] for k in ('id','name','sha256','profile','structure_hash','sections','coverage','changes')} for d in docs], 'relationships':relationships(docs),'prepared':time.time(),'policy_hash':digest(POLICY)})
        data['analysis_registry']={key:{'sha256':value['sha256'],'structure_sha256':value['structure_sha256'],'headings':len(value['structure']),'numbers':len(value['numbers']),'definitions':len(value['definitions'])} for key,value in registries.items()}
        if data['options'].get('check_sto',True) or data['options'].get('formatting','off')!='off':data['limitations'].extend(cat['limitations'])
        cards={c['requirement_id']:c for c in cat['cards']}
        index=Index(docs)
        for doc in docs:
            if self.store.job(jid)['state'] in ('paused','cancelled'):return
            from .formatting import check as check_formatting
            format_findings,format_coverage=check_formatting(doc,cat,data['options']['formatting'])
            write(DATA/'jobs'/jid/(doc['id']+'-formatting.json'),format_coverage)
            data.setdefault('formatting_coverage',[]).extend({'document':doc['id'],**c} for c in format_coverage)
            for item in format_findings:
                f=validate_finding(item,docs,cards);f['deterministic']=True;self.store.finding(jid,f,'question' if f.get('kind')=='question' else 'confirmed')
            for item in deterministic(doc) if data['options'].get('check_logic',True) else []:
                f=validate_finding(item,docs,cards);f['deterministic']=True;self.store.finding(jid,f,'confirmed')
            if data['options'].get('check_logic',True):
                if data['options'].get('check_images',True) and doc['coverage'].get('images'):
                    if getattr(self.client,'props',{}).get('modalities',{}).get('vision'):
                        from .vision import extract
                        assets,issues=extract(doc,DATA/'jobs'/jid/'images'/doc['id'])
                        data.setdefault('visual_coverage',[]).append({'document':doc['id'],'unique_images':len(assets),'limitations':issues})
                        for asset in assets:
                            self.enqueue_bounded(jid,'logic',{'stage':'logic','images':[asset],'blocks':[asset['caption']],'requirements':[],'scope':'visual_image'},data)
                    else:data['limitations'].append('Визуальная проверка недоступна: сервер не поддерживает изображения')
                for item in list(local_candidates(doc))+list(link_candidates(doc)):self.store.finding(jid,validate_finding(item,docs,cards),'candidate')
            if data['options'].get('check_language',True):
                from .language_candidates import candidates as language_candidates
                extra=language_candidates(doc);limit=max(1,int(data['options'].get('language_screen_limit',48)))
                data.setdefault('language_screen',[]).append({'document':doc['id'],'candidates':len(extra),'queued':min(limit,len(extra))})
                if len(extra)>limit:data['limitations'].append('Локальная языковая проверка: '+doc['name']+'; '+str(len(extra)-limit)+' дополнительных гипотез не поставлено на перепроверку из-за лимита. Основной языковой проход сохраняется.')
                for item in extra[:limit]:self.store.finding(jid,validate_finding(item,docs,cards),'candidate')
            if data['options'].get('check_sto',True):
                from .structure import inspect as inspect_structure
                structural,structural_coverage=inspect_structure(doc,cat)
                data.setdefault('structure_coverage',[]).extend(structural_coverage)
                for item in structural:
                    f=validate_finding(item,docs,cards);self.store.finding(jid,f,'candidate')
            base={'documents':[{'id':doc['id'],'name':doc['name'],'profile':doc['profile']}],'scope':'section_subset','requirements':[],'lessons':self.lessons(doc,data['owner'])}
            for stage in ('language','logic'):
                if not data['options'].get('check_'+stage,True):continue
                groups=coalesce_groups(doc,target_chars=data['options'].get('language_chunk_chars',6500) if stage=='language' else data['options'].get('logic_chunk_chars',6500))
                for group in groups:
                    if self.store.job(jid)['state'] in ('paused','cancelled'):return
                    source=next(b for b in doc['blocks'] if b['locator']==group[0]['locator'])
                    payload={**base,'stage':stage,'heading_path':source.get('heading_path',[]),'blocks':symbol_context(doc,group) if stage=='logic' else group}
                    self.enqueue_bounded(jid,stage,payload,data)
            if data['options'].get('check_sto',True):
                selected=[c for c in cat['cards'] if c['document_types']==['general'] or doc['profile']['profile_id'] in c['document_types']]
                for c in selected:
                    if c['check_stage']=='formatting':
                        data.setdefault('coverage_skipped',[]).append({'document':doc['id'],'requirement_id':c['requirement_id'],'state':'skipped_setting' if data['options']['formatting']=='off' else 'separate_formatting','reason':'Оформление выключено пользователем' if data['options']['formatting']=='off' else 'См. результаты измерения свойств Word; текстовый ответ не доказывает оформление'})
                    elif c['check_stage']=='inter':
                        data.setdefault('coverage_skipped',[]).append({'document':doc['id'],'requirement_id':c['requirement_id'],'state':'separate_interdocument' if len(docs)>1 else 'unknown','reason':'Состав комплекта зависит от вида работ и стадии; проверяется по реестру документов'})
                    elif c['check_stage']=='structure':
                        data.setdefault('coverage_skipped',[]).append({'document':doc['id'],'requirement_id':c['requirement_id'],'state':'separate_structure','reason':'Заголовок проверяется по полному локальному дереву, см. structure_coverage; отдельная генерация по одному заголовку не требуется'})
                selected=[c for c in selected if c['check_stage']=='sto']
                # Retrieve each obligation once, then group obligations by exact shared
                # evidence. Coverage remains one row per requirement ID.
                from .planning import group_sto_evidence,focused_outline,completeness,column_findings,deterministic_rule_check
                sto_index=Index([doc]);entries=[]
                for rule in selected:
                    if self.store.job(jid)['state'] in ('paused','cancelled'):return
                    blocks,search=rule_evidence(doc,[rule],sto_index)
                    for item in column_findings(doc,rule,blocks):self.store.finding(jid,validate_finding(item,docs,cards),'candidate')
                    closed=deterministic_rule_check(doc,rule,blocks)
                    if closed:data.setdefault('deterministic_coverage',[]).append(closed)
                    else:entries.append({'rule':rule,'blocks':blocks,'search':search})
                grouped=group_sto_evidence(entries,data['options'].get('sto_group_size',10),data['options'].get('sto_group_chars',52000))
                data.setdefault('sto_planning',[]).append({'document':doc['id'],'semantic_requirements':len(entries),'requests':len(grouped),
                    'source_characters':sum(len(b.get('text','')) for x in grouped for b in x['blocks']),
                    'unique_source_characters':sum(len(b.get('text','')) for b in {k:b for x in grouped for b in x['blocks'] for k in [digest([b.get('document'),b.get('locator'),b.get('offset',0),b.get('text')])]}.values())})
                for group in grouped:
                    rules,blocks=group['rules'],group['blocks'];searches=group['searches']
                    search={'method':'evidence_first_group','sections':list(dict.fromkeys(s for value in searches for s in value.get('sections',[]))),
                        'complete_text_index':all(value.get('complete_text_index',False) for value in searches),
                        'does_not_prove_semantic_absence':True,
                        'by_requirement':{rule['requirement_id']:{'query':value.get('query',''),'sections':value.get('sections',[])} for rule,value in zip(rules,searches)}}
                    focused=focused_outline(doc,blocks)
                    payload={**base,'source_inventory':completeness(doc,blocks),'stage':'sto','requirements':[self.compact_rule(c) for c in rules],
                        'outline':focused,'outline_inventory':{'total':len(doc.get('headings',[])),'sha256':registries[doc['id']]['structure_sha256'],'included':len(focused)},
                        'blocks':blocks,'scope':'shared_evidence_group','search':search,
                        'batch_contract':'Верни отдельный coverage для каждого requirement_id; не объединяй решения.',
                        '_evidence_map':group['evidence_map'],'_document_id':doc['id']}
                    self.enqueue_bounded(jid,'sto',payload,data)
        data['planning_finished']=time.time();self.store.update(jid,'running',data)
    @staticmethod
    def compact_rule(c):
        return {k:c[k] for k in ('requirement_id','document_name','clause','appendix','normative_kind','applicability','source_quote','parent_context_refs','validation_status')}
    def lessons(self,doc,owner):
        with self.store.connect() as c: rows=c.execute('SELECT data FROM lessons WHERE active=1 ORDER BY created DESC LIMIT 100').fetchall()
        lessons=[json.loads(r[0]) for r in rows]
        return [x for x in lessons if x.get('owner')==owner and x.get('type') in (doc['profile']['type'],'general')][:4]
    def rule_subset(self,jid,payload,rules):
        """Narrow a split rule batch to its own source fragments."""
        evidence_map=payload.get('_evidence_map') or {}
        if not evidence_map:return {**payload,'requirements':rules}
        from .planning import block_key,completeness,focused_outline
        keys={key for rule in rules for key in evidence_map.get(rule['requirement_id'],[])}
        blocks=[block for block in payload.get('blocks',[]) if block_key(block) in keys]
        result={**payload,'requirements':rules,'blocks':blocks,
            '_evidence_map':{rule['requirement_id']:evidence_map.get(rule['requirement_id'],[]) for rule in rules}}
        if isinstance(payload.get('search'),dict) and payload['search'].get('by_requirement'):
            ids={rule['requirement_id'] for rule in rules};search=dict(payload['search'])
            search['by_requirement']={key:value for key,value in search['by_requirement'].items() if key in ids}
            search['sections']=list(dict.fromkeys(section for value in search['by_requirement'].values() for section in value.get('sections',[])))
            result['search']=search
        doc=next((d for d in self.material(jid) if d['id']==payload.get('_document_id')),None)
        if doc:
            result['source_inventory']=completeness(doc,blocks);result['outline']=focused_outline(doc,blocks)
            result['outline_inventory']={'total':len(doc.get('headings',[])),'sha256':digest(doc.get('headings',[])),'included':len(result['outline'])}
        return result
    def enqueue_bounded(self,jid,stage,payload,data,depth=0):
        tokens=self.client.count(payload)
        if tokens+output_budget(self.config,payload)+self.config['margin']<=self.client.context:
            key=response_cache_key(self.client,payload) if isinstance(self.client,Client) else digest([VERSION,self.client.signature,stage,payload])
            if not data['options'].get('reuse_cache',True):key=digest(['fresh_review',jid,key])
            tid=self.store.add(jid,stage,payload,key)
            self.store.event(jid,'task_budget',{'task':tid,'stage':stage,'input_tokens':tokens,'output_tokens':output_budget(self.config,payload)})
            return
        if depth>=20:raise BudgetError('Достигнут предел безопасного разбиения')
        if len(payload.get('outline',[]))>24:
            # The full tree stays on disk and in the search index. A tenfold document
            # must not turn its navigation tree into an unbounded model prompt.
            outline=payload['outline'];query=' '.join(b['text'] for b in payload.get('blocks',[]))+' '+str(payload.get('requirements',[]))
            wanted=set(terms(query));ranked=sorted(enumerate(outline),key=lambda item:len(wanted.intersection(terms(item[1]['title']))),reverse=True)[:24]
            subset=[h for _,h in sorted(ranked)]
            smaller={**payload,'outline':subset,'outline_inventory':{'total':len(outline),'sha256':digest(outline),'included':len(subset)},'scope':'retrieved_outline_and_evidence','directions':payload.get('directions','')+' В payload только выбранные заголовки из полного локального дерева. По этой выборке нельзя доказывать отсутствие раздела или объекта.'}
            self.enqueue_bounded(jid,stage,smaller,data,depth+1)
        elif payload.get('candidates'):
            if len(payload['candidates'])>1:
                for f in payload['candidates']:
                    self.enqueue_bounded(jid,stage,{**payload,'candidates':[f]},data,depth+1)
            else:
                f=payload['candidates'][0];required={(e['document'],e['locator']) for e in f['evidence']}
                essential=[b for b in payload['blocks'] if (b['document'],b['locator']) in required]
                if len(essential)==len(payload['blocks']):
                    self.store.verdict(f['id'],'question','Полный набор доказательств не помещается в контекст; автоматическое подтверждение запрещено')
                    return
                self.enqueue_bounded(jid,stage,{**payload,'blocks':essential,'scope':'evidence_only','source_inventory':{'document_complete':False,'complete_sections':[]},'section_inventories':[]},data,depth+1)
        elif len(payload.get('requirements',[]))>1:
            # Preserve the complete evidence set and split decisions before splitting
            # source text; this avoids checking several obligations on partial evidence.
            rules=payload['requirements'];middle=(len(rules)+1)//2
            for part in (rules[:middle],rules[middle:]):self.enqueue_bounded(jid,stage,self.rule_subset(jid,payload,part),data,depth+1)
        elif payload.get('images'):
            raise BudgetError('Изображение с подписью не помещается в контекст; уменьшите размер или выделите область вручную')
        elif payload.get('blocks'):
            for part in split_blocks(payload['blocks']):self.enqueue_bounded(jid,stage,{**payload,'blocks':part,'scope':'split_subset','source_inventory':{'document_complete':False,'complete_sections':[]},'split_depth':depth+1},data,depth+1)
        else:raise BudgetError('Служебная часть не помещается')
    def run(self,jid):
        if not self.lock.acquire(False):raise ValueError('GPU уже занят другим заданием')
        previous_config,previous_client=self.config,self.client
        lease=Lease(self.store.path+'.worker.lock');sleep=SleepInhibitor();stop=threading.Event();heartbeat=None
        try:
            lease.__enter__()
            sleep.__enter__()
            job=self.store.job(jid)
            self.config=copy.deepcopy(job['data']['options'])
            if isinstance(self.client,Client):self.client=Client(self.config)
            self.cache=LRU(self.config['ram_bytes'])
            self.store.recover(jid)
            def pulse():
                while not stop.is_set():
                    self.store.heartbeat(jid,self.cache.size);stop.wait(5)
            heartbeat=threading.Thread(target=pulse,daemon=True);heartbeat.start()
            job=self.store.job(jid)
            if job['state']=='preparing' or not job['data'].get('planning_finished'):
                self.store.update(jid,'preparing');self.prepare(jid)
            else:
                self.validate_resume(jid)
                self.client.probe()
                if self.client.signature!=job['data']['model']['signature']:raise ValueError('Изменились модель или шаблон; создайте новую проверку вместо смешивания результатов')
                self.store.update(jid,'running')
            for stage in STAGES:
                if self.store.job(jid)['state']!='running':return
                if stage in ('cross','inter','verify'):self.derive(jid,stage)
                while self.store.job(jid)['state']=='running':
                    task=self.store.claim(jid,stage)
                    if not task:break
                    self.execute(task)
            job=self.store.job(jid);tasks=self.store.tasks(jid)
            if job['state']=='running':
                coverage_unknown=any(x.get('state') in ('unknown','insufficient','unverified') for t in tasks for x in (t.get('result') or {}).get('coverage',[]))
                format_unknown=any(x.get('state') in ('unverified','source_conflict') for x in job['data'].get('formatting_coverage',[]))
                visual_unknown=job['data']['options'].get('check_logic',True) and any(d['coverage'].get('images') or d['coverage'].get('objects') for d in job['data'].get('documents',[]))
                incomplete=coverage_unknown or format_unknown or visual_unknown or any(t['state'] in ('failed','pending','running') or (t.get('result') or {}).get('invalid') for t in tasks) or bool(job['data']['limitations']) or any(f['status'] in ('candidate','verifying','question') for f in self.store.findings(jid))
                self.store.update(jid,'partial' if incomplete else 'completed')
        except BusyError:
            # A second caller must not pause the job owned by the first process.
            self.store.event(jid,'worker_busy',{})
        except Exception as e:
            data=self.store.job(jid)['data'];data['fatal_error']=str(e);self.store.update(jid,'failed',data)
        finally:
            stop.set()
            if heartbeat:heartbeat.join(2)
            self.config,self.client=previous_config,previous_client
            sleep.__exit__()
            lease.__exit__();self.lock.release()
    def validate_resume(self,jid):
        data=self.store.job(jid)['data']
        if data.get('implementation')!=implementation_hash():raise ValueError('Алгоритм обновлён; создайте новую проверку. Совпадающие запросы смогут использовать сохранённые ответы')
        for doc in self.material(jid):
            for key,expected in (('path',doc['sha256']),('original_path',doc['sha256']),('source_path',doc.get('source_sha256',doc['sha256']))):
                path=Path(doc.get(key,doc['path']))
                if not path.exists():raise ValueError('Недоступен документ: '+doc['name'])
                if digest(path.read_bytes())!=expected:raise ValueError('Документ изменён: '+doc['name']+'. Создайте новую проверку; прежние результаты сохранены отдельно')
    def start(self,jid):
        if self.lock.locked():raise ValueError('Другое задание уже выполняется')
        self.thread=threading.Thread(target=self.run,args=(jid,),daemon=True);self.thread.start()
    def execute(self,task):
        jid=task['job'];docs=self.material(jid);job=self.store.job(jid);cat=load_catalog(job['data']['catalog']);cards={c['requirement_id']:c for c in cat['cards']}
        result=self.store.cached(task['cache_key']);cached=result is not None;error=None;failure_result=None
        if not cached:
            for attempt in range(self.config['retries']+1):
                try:
                    self.store.event(jid,'generation_attempt',{'task':task['id'],'stage':task['stage'],'attempt':attempt+1})
                    raw,metrics=self.client.generate(task['payload']);result={'raw':raw,'metrics':metrics};break
                except (OutputError,BudgetError) as e:
                    error=str(e)
                    diagnostic={'metrics':getattr(e,'metrics',{}),'finish_reason':(getattr(e,'response',{}).get('choices') or [{}])[0].get('finish_reason')}
                    failure_result=diagnostic
                    if getattr(e,'response',None):write(DATA/'jobs'/jid/'incomplete-responses'/(task['id']+'.json'),e.response)
                    # Output overflow splits the work once; no empty result is accepted.
                    if not task['payload'].get('images') and task['payload'].get('retry_split',0)<2:
                        try:
                            rules=task['payload'].get('requirements',[])
                            candidates=task['payload'].get('candidates',[])
                            if len(candidates)>1:
                                middle=(len(candidates)+1)//2;parts=[{**task['payload'],'candidates':part,'retry_split':task['payload'].get('retry_split',0)+1} for part in (candidates[:middle],candidates[middle:])]
                            elif len(rules)>1:
                                middle=(len(rules)+1)//2;parts=[{**self.rule_subset(jid,task['payload'],part),'retry_split':task['payload'].get('retry_split',0)+1} for part in (rules[:middle],rules[middle:])]
                            elif task['stage']!='verify' and task['payload'].get('blocks'):
                                parts=[{**task['payload'],'blocks':part,'scope':'split_subset','source_inventory':{'document_complete':False,'complete_sections':[]},'retry_split':task['payload'].get('retry_split',0)+1} for part in split_blocks(task['payload']['blocks'])]
                            else:parts=[]
                            if not parts:raise ValueError('Нет безопасной границы разбиения')
                            for part in parts:self.enqueue_bounded(jid,task['stage'],part,job['data'])
                            self.store.finish(task,{'split':True,**diagnostic},state='split');return
                        except ValueError:pass
                    break
                except Exception as e:error=str(e);self.store.event(jid,'attempt_error',{'task':task['id'],'attempt':attempt+1,'error':error[:300]})
            if result is None:self.store.finish(task,result=failure_result,error=error or 'Нет ответа');return
            # Persist the immutable model response before publishing any individual finding.
            # Recovery replays this same response instead of generating a second variant.
            self.store.cache(task['cache_key'],result)
        if cached:result={**result,'reused_metrics':result.get('metrics',{}),'metrics':{'usage':{},'seconds':0,'estimated_tokens':self.client.count(task['payload'])}}
        raw=result['raw'];valid=[];invalid=[]
        allowed={(b['document'],b['locator']) for b in task['payload'].get('blocks',[])}
        from .planning import coalesce_row_findings,coverage_questions
        for item in coalesce_row_findings(raw['findings'],task['payload'])+coverage_questions(raw,task['payload']):
            try:
                if task['payload'].get('images'):
                    from .vision import anchor_finding
                    item=anchor_finding(item,task['payload']['images'])
                item=restore_evidence_addresses(item,task['payload'].get('blocks',[]))
                item=restore_non_normative_id(item,task['stage'],task['payload'].get('requirements',[]))
                item=route_finding(item,task['stage'])
                if any((e['document'],e['locator']) not in allowed for e in item['evidence']):raise ValueError('Цитата вне пакета')
                f=validate_finding(item,docs,cards)
                if task['stage']=='inter' and f.get('kind')=='violation' and len({e['document'] for e in f['evidence']})<2:
                    f['kind']='question';f['explanation']+=' Противоречие между документами не подтверждено цитатами из двух файлов.'
                from .quality_gate import assess
                forced,reason=assess(f,docs)
                if task['payload'].get('images'):
                    f['visual_review']=True;f['visual_assets']=task['payload']['images'];f['verification']='Требуется визуальная перепроверка человеком';forced='question';reason='Вывод по изображению не подтверждается одной текстовой подписью'
                if forced:f['quality_gate']=reason
                fid=self.store.finding(jid,f,forced or ('question' if f.get('kind')=='question' else 'style' if f.get('kind')=='style' else 'candidate'));valid.append(fid)
            except (ValueError,KeyError,TypeError) as e:invalid.append({'kind':'finding','error':str(e),'item':item})
        facts=[]
        for f in raw['facts']:
            try:
                from .facts import validate_value
                f=restore_evidence_addresses(f,task['payload'].get('blocks',[]))
                if any((e['document'],e['locator']) not in allowed for e in f['evidence']):raise ValueError('Факт вне пакета')
                f['evidence']=validate_evidence(f['evidence'],docs);validate_value(f);facts.append(f)
            except (ValueError,KeyError,TypeError) as e:invalid.append({'kind':'fact','error':str(e),'item':f})
        if task['stage']=='verify':
            candidates={x['id']:x for x in task['payload'].get('candidates',[])};decided=set()
            occurrences=Counter(d.get('id') for d in raw['decisions'] if isinstance(d,dict) and isinstance(d.get('id'),str))
            for d in raw['decisions']:
                if not isinstance(d,dict) or not isinstance(d.get('id'),str) or d.get('verdict') not in ('confirmed','rejected','question') or not isinstance(d.get('reason'),str) or not d['reason'].strip():
                    invalid.append({'kind':'decision','error':'Неверная структура отдельного решения'});continue
                if d['id'] not in candidates or d['id'] in decided:continue
                decided.add(d['id']);status=d['verdict'];f=candidates[d['id']]
                if occurrences[d['id']]>1:
                    self.store.verdict(d['id'],'question','Для одного кандидата возвращено несколько решений; однозначный результат не установлен')
                    invalid.append({'kind':'decision','error':'Повторяющийся идентификатор решения'});continue
                from .quality_gate import assess
                # The verifier can introduce a new, incorrect explanation even when
                # the original candidate passed the gate. Validate its actual reason.
                suggestion=d.get('suggestion') if isinstance(d.get('suggestion'),str) and d['suggestion'].strip() else f.get('suggestion','')
                forced,reason=assess({**f,'explanation':d['reason'],'suggestion':suggestion},docs)
                if status=='confirmed' and forced:status=forced;d['reason']=reason
                if status=='rejected':
                    from .quality_gate import assess_rejection
                    forced,reason=assess_rejection(f,d['reason'])
                    if forced:status=forced;d['reason']=reason
                if status=='confirmed' and f.get('needs_full_scope'):status='question';d['reason']+=' Автоматический поиск не доказывает отсутствие во всём документе.'
                self.store.verdict(d['id'],status,d['reason'],verified_explanation=True,suggestion=suggestion)
            for fid in candidates.keys()-decided:self.store.verdict(fid,'question','Модель не вернула решение по этому элементу; пакет не принят как полный')
        required=set() if task['payload'].get('validation_retry') else {r['requirement_id'] for r in task['payload'].get('requirements',[])}
        coverage=[]
        for c in raw['coverage']:
            if not isinstance(c,dict) or not isinstance(c.get('requirement_id'),str) or c.get('state') not in ('checked','not_applicable','unknown','insufficient') or not isinstance(c.get('reason'),str):
                invalid.append({'kind':'coverage','error':'Неверная структура отдельного решения по требованию'});continue
            if c['requirement_id'] in required:coverage.append(c)
        duplicated={rid for rid,n in Counter(c['requirement_id'] for c in coverage).items() if n>1}
        coverage=[c for c in coverage if c['requirement_id'] not in duplicated]
        for rid in duplicated:invalid.append({'kind':'coverage','error':'Повторяющийся идентификатор требования: '+rid})
        for rid in required-{c['requirement_id'] for c in coverage}:coverage.append({'requirement_id':rid,'state':'insufficient','reason':'Решение не возвращено моделью'})
        result={**result,'facts':facts,'findings':valid,'invalid':invalid,'coverage':coverage,'cached':cached}
        self.store.finish(task,result)
        repairable=[x for x in invalid if x['kind'] in ('finding','fact') and x.get('item')]
        if repairable and task['stage']!='verify' and not task['payload'].get('validation_retry'):
            payload={**task['payload'],'validation_retry':1,'repair_items':repairable,
                     'directions':'Повторно проверь ТОЛЬКО элементы repair_items, которые не прошли проверку цитат/чисел. Не повторяй остальные находки. Бери document, locator и непрерывную цитату буквально из blocks; не исправляй исходный текст в цитате. Если гипотезу нельзя подтвердить, не выдавай её. Coverage не возвращай.'}
            self.enqueue_bounded(jid,task['stage'],payload,job['data'])
            self.store.event(jid,'validation_recheck',{'task':task['id'],'items':len(repairable),'maximum_rechecks':1})
        if task['stage']!='verify':
            waiting=[f for f in self.store.findings(jid) if f['status']=='candidate']
            if len(waiting)>=3:
                self.queue_verification(jid,waiting[:3])
            # Drain as many bounded decisions as we normally enqueue; avoid an
            # ever-growing verification tail while normative tasks are running.
            for _ in range(3):
                verification=self.store.claim(jid,'verify')
                if verification:self.execute(verification)
                else:break
    def queue_verification(self,jid,candidates):
        job=self.store.job(jid);data=job['data'];docs=self.material(jid);index=Index(docs);cards={c['requirement_id']:c for c in load_catalog(data['catalog'])['cards']}
        from .planning import duplicate_groups
        candidates=[f for f in candidates if not f.get('visual_review')]
        candidates,duplicates=duplicate_groups(candidates)
        for duplicate,original in duplicates:self.store.verdict(duplicate['id'],'rejected','Повтор замечания '+original['id']+'; отдельная генерация не требуется')
        group_size=max(1,int(data['options'].get('verification_group_size',1)))
        for i in range(0,len(candidates),group_size):
            fs=candidates[i:i+group_size];anchors=[e for f in fs for e in f['evidence']];query=' '.join(f['issue']+' '+f['explanation'] for f in fs)
            rules=[self.compact_rule(cards[f['requirement_id']]) for f in fs if f.get('requirement_id') in cards]
            for f in fs:self.store.verdict(f['id'],'verifying','Поставлено на независимую перепроверку')
            blind=[{k:v for k,v in f.items() if k not in ('explanation','verification','initial_explanation')} for f in fs]
            navigation={}
            if any(f.get('target_numbers') for f in fs):
                ids={e['document'] for e in anchors};inventory=[x for d in docs if d['id'] in ids for x in reference_inventory(d) if x['kind']=='приложение']
                if len(inventory)<=48:navigation={'appendix_inventory':inventory,'navigation_basis':'Номера и заголовки из той же неизменённой версии Word; это полный перечень распознанных приложений, а не реконструкция модели. Содержимое изображений не распознано.'}
            blocks=verification_context(docs,fs)
            # A keyword hit alone is not adequate counterevidence for a normative
            # omission. Include the source-derived target section before budgeting.
            for doc in docs:
                relevant=[cards[f['requirement_id']] for f in fs if f.get('requirement_id') in cards and any(e['document']==doc['id'] for e in f['evidence'])]
                if relevant:
                    extra,_=rule_evidence(doc,relevant)
                    known={(b['document'],b['locator'],b.get('offset',0)) for b in blocks}
                    blocks.extend(b for b in extra if (b['document'],b['locator'],b.get('offset',0)) not in known)
            from .planning import completeness
            inventories=[completeness(doc,blocks) for doc in docs]
            self.enqueue_bounded(jid,'verify',{'stage':'verify','directions':'Проверь каждый id из candidates по исходным blocks, контексту и требованиям. Используй правила этапа verify; при неполных доказательствах — question, при опровержении — rejected.','candidates':blind,'blocks':blocks,'requirements':rules,'section_inventories':inventories,'scope':'targeted_counterevidence','search':index.search_record(query),**navigation},data)
    def derive(self,jid,stage):
        job=self.store.job(jid);data=job['data']
        if stage in data['derived']:return
        docs=self.material(jid);index=Index(docs);cat=load_catalog(data['catalog']);cards={c['requirement_id']:c for c in cat['cards']}
        if stage=='cross' and data['options'].get('check_logic',True):
            facts=[]
            for t in self.store.tasks(jid,'logic'):
                if t['result']:facts.extend(t['result'].get('facts',[]))
            from .facts import normalize,contradictions
            facts=[normalize(f) for f in facts]
            write(DATA/'jobs'/jid/'facts.json',facts)
            for item in contradictions(facts):
                f=validate_finding(item,docs,cards)
                # Numeric calculation is exact; extraction and scope identity still undergo verification.
                self.store.finding(jid,f,'candidate')
            topics=defaultdict(list)
            for fact in facts:
                key=' '.join(sorted(set(terms(fact['parameter'])))) or 'other';topics[key].append(fact)
            data['fact_coverage']={'extracted':len(facts),'parameter_groups':len(topics),'groups_with_one_source':sum(len({(e['document'],e['locator']) for f in fs for e in f['evidence']})<2 for fs in topics.values()),'method':'Сопоставление извлечённых фактов и полная проверка ссылок по дереву; одиночный факт не создаёт бессодержательное сравнение с самим собой.'}
            for key,fs in topics.items():
                if len({(e['document'],e['locator']) for f in fs for e in f['evidence']})<2:
                    continue
                for start in range(0,len(fs),8):
                    group=fs[start:start+8];anchors=[e for f in group for e in f['evidence']];query=' '.join(f['entity']+' '+f['parameter']+' '+f['value'] for f in group)
                    payload={'stage':'cross','facts':group,'blocks':index.retrieve(query,anchors,limit=6),'scope':'facts_with_originals','requirements':[]}
                    self.enqueue_bounded(jid,stage,payload,data)
            # Whole-document links/appendix titles are checked against the complete tree.
            for d in docs:
                for payload in reference_payloads(d,group_size=data['options'].get('reference_group_size',1)):self.enqueue_bounded(jid,stage,payload,data)
        if stage=='inter' and len(docs)>1:
            byid={d['id']:d for d in docs}
            from .facts import normalize
            all_facts=[normalize(f) for t in self.store.tasks(jid,'logic') for f in (t['result'] or {}).get('facts',[])]
            traces=[];normative=[]
            for group in data['relationships']['groups']:
                subset=[byid[i] for i in group['documents']];ix=Index(subset)
                group_ids=set(group['documents']);pids={d['profile']['profile_id'] for d in subset}
                rules=[cards[r] for r in cat['interdocument'] if cards[r]['document_types']==['general'] or pids.intersection(cards[r]['document_types'])] if data['options'].get('check_sto',True) else []
                from .planning import comparison_payloads
                for pair_payload in comparison_payloads(subset):self.enqueue_bounded(jid,stage,pair_payload,data)
                for start in range(0,len(rules),1):
                    rs=rules[start:start+1];query=' '.join(r['compact_text'] for r in rs)
                    normative.append({'stage':'inter','relation':group,'directions':'Проверь применимость правил к данному комплекту и трассировку документов. Различай отсутствие файла в комплекте и отсутствие документа в проекте; последнее неизвестно. Противоречие требует цитат из двух документов.','documents':[{'id':d['id'],'profile':d['profile']} for d in subset],'requirements':[self.compact_rule(r) for r in rs],'blocks':ix.retrieve(query,limit=12),'scope':'related_documents_subset'})
                grouped=defaultdict(list)
                for f in all_facts:
                    if {e['document'] for e in f['evidence']}<=group_ids:grouped[(f['entity_id'],f['parameter'])].append(f)
                for key,fs in grouped.items():
                    present={e['document'] for f in fs for e in f['evidence']}
                    traces.append({'entity':key[0],'parameter':key[1],'documents':sorted(present),'facts':[f['fact_id'] for f in fs],'state':'linked_by_entity_requires_semantic_check'})
                    if len(present)<2:continue
                    # Bounded clusters avoid the Cartesian product of requirements and tests.
                    for start in range(0,len(fs),8):
                        part=fs[start:start+8];anchors=[e for f in part for e in f['evidence']]
                        self.enqueue_bounded(jid,stage,{'stage':'inter','relation':group,'facts':part,'directions':'Сопоставь требование, реализацию и критерий испытания. Различия допустимы для разных контуров и этапов; объясни условия.','blocks':ix.retrieve(' '.join(key),anchors,limit=5),'requirements':[],'scope':'traceability_cluster'},data)
                for topic in ('функции требования приемка испытания','состав системы версии интерфейсы архитектура','этапы сроки роли ответственность','производительность надежность безопасность'):
                    payload={'stage':'inter','relation':group,'directions':'Междокументная трассировка: цитаты минимум из двух разных файлов для противоречия.','documents':[{'id':d['id'],'profile':d['profile']} for d in subset],'blocks':ix.retrieve(topic,limit=20),'requirements':[],'scope':'related_documents_subset'}
                    self.enqueue_bounded(jid,stage,payload,data)
            for payload in normative:self.enqueue_bounded(jid,stage,payload,data)
            write(DATA/'jobs'/jid/'traceability.json',traces);data['traceability']=traces
        if stage=='verify':
            candidates=[f for f in self.store.findings(jid) if f['status']=='candidate' or (f['status']=='question' and not f.get('verification'))]
            self.queue_verification(jid,candidates)
        data['derived'].append(stage);self.store.update(jid,data=data)
    def status(self,jid):
        j=self.store.job(jid);tasks=self.store.tasks(jid);fs=self.store.findings(jid);counts={s:dict(Counter(t['state'] for t in tasks if t['stage']==s)) for s in STAGES};durations=defaultdict(list);remaining=0
        for t in tasks:
            if t['state']=='done' and t['started'] and t['ended'] and not (t['result'] or {}).get('cached'):durations[t['stage']].append(t['ended']-t['started'])
        for t in tasks:
            if t['state'] in ('pending','running'):
                values=durations[t['stage']];remaining+=(sum(values)/len(values)) if values else 35
        dynamic=any(s not in j['data']['derived'] for s in ('cross','inter','verify'))
        from .timing import remaining_seconds
        with self.store.connect() as conn:
            planned=[json.loads(row['data']) for row in conn.execute("SELECT data FROM events WHERE job=? AND kind='task_budget'",(jid,))]
            cached_keys={row[0] for row in conn.execute('SELECT DISTINCT t.cache_key FROM tasks t JOIN cache c ON c.key=t.cache_key WHERE t.job=?',(jid,))}
        documents=self.material(jid) if j['data'].get('documents') and 'cross' not in j['data']['derived'] else []
        remaining,_=remaining_seconds(j,tasks,fs,documents,{b['task']:b for b in planned},cached_keys,time.time())
        runtime=self.store.runtime(jid)
        return {'id':jid,'state':j['state'],'created':j['created'],'updated':j['updated'],'documents':j['data'].get('documents',[]),'stages':counts,'findings':dict(Counter(f['status'] for f in fs)),'eta_seconds':[max(1,int(remaining*.7)),max(1,int(remaining*(2 if dynamic else 1.4)))] if j['state'] in ('running','preparing') else None,'eta_provisional':dynamic,'running':[{'id':t['id'],'stage':t['stage'],'seconds':time.time()-(t['started'] or time.time())} for t in tasks if t['state']=='running'],'fatal_error':j['data'].get('fatal_error'),'ram_cache_bytes':runtime['cache_bytes'] if runtime else None,'process_ram_bytes':runtime['rss_bytes'] if runtime else None,'heartbeat':runtime['at'] if runtime else None,'stalled':bool(runtime and j['state']=='running' and time.time()-runtime['at']>90),'ram_limit':j['data']['options']['ram_bytes'],'model':j['data'].get('model'), 'limitations':j['data'].get('limitations',[])}
