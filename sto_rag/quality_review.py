"""Structured, source-driven review. All working document state remains in RAM."""
import copy
import hashlib
import json
import re
import threading
import time
import uuid
import math
import itertools
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

import batch_review as base
import rag
from document_locations import build_locations, verified_rows
from extract import Numbering, clean
from sections import plan
from word_compact import package, N, Q

POLICY = '''
Проверяй только направления из directions текущего задания.
Ты получаешь часть единого документа, а не самостоятельный документ. document_profile,
heading_path и table_context задают обязательный контекст. Оглавление не является содержанием.
Родительский заголовок может содержать только дочерние разделы: это не ошибка.
Объединённые ячейки и продолжение таблицы не означают пропуск данных.
Не создавай замечания об отсутствии сведений на основании неполной выборки.
Если наличие сведений во всём документе не установлено, укажи недостаточно данных.
Не применяй шаблоны других типов документов без прямой нормы о применимости.
Отличай обязательные указания шаблона от иллюстративных примеров внутри него.
Значения из примеров не являются обязательными значениями для проверяемой системы.
Отсутствие СТО в локальной/межраздельной проверке не является ограничением: нормативный
проход выполняется отдельно. Не перечисляй разделы, которых нет в текущем пакете.
Факты из примеров отчётов не являются параметрами эксплуатации системы.
Различие чисел не доказывает противоречие: сравни условия, объект, единицы и смысл границ.
Минимальная и максимальная границы могут одновременно выполняться.
Все цитаты копируй непрерывно, без многоточий и исправления ошибок оригинала.
Если переданы candidates, это гипотезы предыдущего прохода: независимо перепроверь их.
Верификация возвращает только подтверждённые замечания из candidates, не новые замечания.
Пустой findings означает снятие гипотез. Объясни снятие в limitations кратко.
'''

ABSENCE = re.compile(r'отсутств|не указан|не определ|не привед|не описан|не представл|не содержит', re.I)

def units_for_document(path):
    d = package(path)
    legacy = plan(path)
    rows = verified_rows(path, d['sha256'])
    addresses = build_locations(d, rows)
    with ZipFile(path) as z: numbering = Numbering(z)
    blocks = [b for g in legacy['groups'] for b in g]
    by_id = {b['locator']: b for b in blocks}
    paragraph_cells={id(p):c for c in d['root'].iter(Q+'tc') for p in c.findall('w:p',N)}
    headings = []; stack = []; body = []; table_headers = {}; table_titles = {}; caption = ''
    for p, r in zip(d['paragraphs'], d['records']):
        b = copy.deepcopy(by_id[r['id']]); b['text'] = r['text']; b['offset'] = 0
        props = numbering.properties(p); level = int(props.get('outline', '9'))
        if level < 9 and not r['table'] and clean(r['text']):
            while stack and stack[-1]['level'] >= level: stack.pop()
            stack.append({'level':level,'title':clean(r['text']),'locator':r['id'], 'address':addresses[r['id']]})
            headings.append(dict(stack[-1])); caption = ''
        b['heading_path'] = [x['title'] for x in stack]
        b['section_title'] = ' / '.join(b['heading_path']) or 'Начало документа'
        b['section'] = stack[-1]['locator'] if stack else 'front'
        b['address'] = addresses[r['id']]
        b['is_heading'] = level < 9 and not r['table']
        if re.match(r'^\s*Таблица\s+[\dА-Я]',r['text']): caption = clean(r['text'])
        fmt = b['format']
        if r['table']:
            ti = fmt['table']; table_titles.setdefault(ti, caption)
            if fmt['row'] == 1: table_headers.setdefault(ti, []).append(r['text'])
            cell = paragraph_cells.get(id(p))
            if cell is not None:
                span = cell.find('w:tcPr/w:gridSpan', N); merge = cell.find('w:tcPr/w:vMerge', N)
                fmt['column_span'] = int(span.get(Q+'val','1')) if span is not None else 1
                if merge is not None: fmt['vertical_merge'] = merge.get(Q+'val','continue')
        body.append(b)
    for b in body:
        ti = b['format'].get('table')
        if ti:
            b['table_context'] = {'title':table_titles[ti], 'headers':table_headers.get(ti,[]),
                                  'table':ti, 'row':b['format']['row'], 'cell':b['format']['cell']}
    units = []
    for b in body:
        if not b['text'].strip(): continue
        key = ('row',b['format']['table'],b['format']['row']) if 'table' in b['format'] else ('paragraph',b['locator'])
        if units and units[-1]['key'] == key: units[-1]['blocks'].append(b)
        else: units.append({'key':key,'section':b['section'],'blocks':[b]})
    # Include non-body content exactly once, outside section structure.
    for b in blocks:
        if not re.fullmatch(r'p\d+',b['locator']) and b['text'].strip():
            units.append({'key':('extra',b['locator']), 'section':b['section'], 'blocks':[b]})
    front = '\n'.join(b['text'] for b in body[:180])
    if re.search(r'частно[её]\s+техническо[её]\s+задани[её]|\bЧТЗ\b',front,re.I): kind,app='ЧТЗ','Б'
    elif re.search(r'техническо[её]\s+задани[её]',front,re.I):kind,app='ТЗ','А'
    else:kind,app='не определён',None
    profile = {'type':kind,'template_appendix':app,'title_excerpt':front[:2200],
               'heading_count':len(headings),'headings':headings}
    return d,legacy,profile,units

def pack_units(units, budget=3000):
    groups=[]; current=[]; size=0; section=None
    for u in units:
        weight=sum(len(b['text']) for b in u['blocks'])
        if current and (section != u['section'] or size+weight>budget):
            groups.append(current);current=[];size=0
        current.extend(u['blocks']);size+=weight;section=u['section']
    if current:groups.append(current)
    return groups

def applicable_sources(index, profile):
    app=profile['template_appendix']
    if not app: raise ValueError('Не удалось определить тип документа: требуется выбор профиля ТЗ/ЧТЗ')
    return [c for c in index.chunks if
            ('04.001.1' in c['document'] and (not c.get('appendix_title') or c['appendix_title'].startswith('Приложение '+app+' ')))
            or ('04.001.0' in c['document'] and not c.get('appendix_title'))]

def search_units(units, query, anchors=(), budget=6500):
    terms=set(rag.tokens(query)); ranked=[]; df=Counter()
    for u in units:
        if '_terms' not in u:
            u['_terms']=list(set(rag.tokens(' '.join(b['section_title']+' '+b['text'] for b in u['blocks']))))
        df.update(u['_terms'])
    for i,u in enumerate(units):
        score=sum(math.log(1+len(units)/(1+df[t])) for t in terms.intersection(u['_terms']))
        if any(b['locator'] in anchors for b in u['blocks']):score+=10000
        ranked.append((score,-i,i))
    chosen=[];used=0
    for score,_,i in sorted(ranked,reverse=True):
        if score<=0:continue
        size=sum(len(b['text']) for b in units[i]['blocks'])
        if used+size>budget and chosen:continue
        chosen.append(i);used+=size
        if used>=budget:break
    return [copy.deepcopy(b) for i in sorted(chosen) for b in units[i]['blocks']]

def source_packets(sources,budget=3500):
    packets=[];current=[];size=0
    for s in sources:
        if current and (size+len(s['text'])>budget or len(current)>=6 or s['document']!=current[-1]['document']):
            packets.append(current);current=[];size=0
        current.append(s);size+=len(s['text'])
    if current:packets.append(current)
    return packets

class Client(base.Client):
    def __init__(self):
        super().__init__(); self.output=min(6144,self.context//3)
    def request(self,payload):
        request=super().request(payload)
        request['messages'][0]['content'] += POLICY
        if payload['phase']=='requirements':
            schema=request['response_format']['json_schema']['schema']
            schema['properties']['requirement_checks']={'type':'array','items':{'type':'object',
                'properties':{'source_id':{'type':'string'},'status':{'type':'string','enum':['проверено','неприменимо','недостаточно данных']},
                              'reason':{'type':'string'},'evidence':schema['properties']['findings']['items']['properties']['evidence']},
                'required':['source_id','status','reason','evidence'],'additionalProperties':False}}
            schema['properties']['requirement_checks']['items']['properties']['evidence']={**schema['properties']['requirement_checks']['items']['properties']['evidence'],'minItems':0}
            schema['required'].append('requirement_checks')
        if payload['phase']=='verify':
            schema=request['response_format']['json_schema']['schema']
            ev={**schema['properties']['findings']['items']['properties']['evidence'],'minItems':0}
            schema['properties']['verification']={'type':'object','properties':{
                'decision':{'type':'string','enum':['подтверждено','снято','недостаточно данных']},
                'reason':{'type':'string'},'evidence':ev},'required':['decision','reason','evidence'],'additionalProperties':False}
            schema['required'].append('verification')
        return request

class Engine(base.Engine):
    def start(self,path,scope=''):
        p=Path(path).resolve(strict=True);digest=hashlib.sha256(p.read_bytes()).hexdigest()
        with self.lock:
            for j in self.jobs.values():
                if j.get('pipeline_version')==3 and j['path']==str(p) and j['sha256']==digest and j['scope']==scope:return self.status(j['id'])
            if any(j['status'] in ('running','queued','cancelling') for j in self.jobs.values()):raise ValueError('Другая проверка выполняется')
            self.index=self.index or rag.Index()
            d,legacy,profile,units=units_for_document(p)
            if d['sha256']!=digest:raise ValueError('Документ изменился во время открытия')
            sources=applicable_sources(self.index,profile)
            groups=pack_units(units)
            tasks=[{'id':'local-'+str(i),'phase':'parts','blocks':g,'state':'pending','attempts':0} for i,g in enumerate(groups)]
            for i,packet in enumerate(source_packets(sources)):
                blocks=search_units(units,' '.join(s['heading']+' '+s['text'] for s in packet),budget=4500)
                tasks.append({'id':'norm-'+str(i),'phase':'requirements','sources':packet,'blocks':blocks,'state':'pending','attempts':0})
            key=uuid.uuid4().hex
            j={'id':key,'pipeline_version':3,'path':str(p),'sha256':digest,'scope':scope,'status':'queued','cancel':False,
               'phase':'parts','layout':legacy['layout'],'warnings':[legacy['warnings'][0],legacy['warnings'][1],
                   'Профиль нормативного прохода: '+profile['type']+'. Покрытие учитывается по фрагментам СТО; это не доказательство проверки каждого атомарного требования.',
                   'Актуальность предоставленных редакций СТО вне локальной базы не подтверждена.'],
               'profile':profile,'units':units,'tasks':tasks,'cross_built':False,'verification_built':False,
               'created':time.strftime('%Y-%m-%d %H:%M:%S'),'report':None,'started_epoch':time.time(),'root_calls':{},
               'initial_parts':len(groups),'source_catalog':[{'id':s['id'],'document':s['document'],'clause':s['clause']} for s in sources]}
            if self.memory()+len(json.dumps(j,ensure_ascii=False).encode())*4>self.memory_limit:raise MemoryError('Лимит RAM')
            self.jobs[key]=j
        threading.Thread(target=self.run,args=(key,),daemon=True).start()
        return self.status(key)

    def payload(self,j,t):
        phase=t['phase'];sources=t.get('sources',[])
        directions=['СТО'] if phase=='requirements' else (list(dict.fromkeys(c['category'] for c in t['candidates'])) if phase=='verify' else ['общая логика','техническая логика','грамотность'])
        task={'parts':'Проверь локальную логику и грамотность. Извлеки существенные параметры с условиями; не извлекай значения примеров.',
              'requirements':'Проверь применимость и выполнение каждого переданного фрагмента СТО. Для каждого source_id верни requirement_checks: status, reason, evidence. Для проверено нужны цитаты из документа. Для неприменимо объясни причину. Отсутствие в выборке означает недостаточно данных. Верни facts=[].',
              'cross':'Проверь заданные пары фактов. Учитывай условия и контекст строк таблиц. Верни facts=[].',
              'verify':'Перепроверь кандидата по найденным местам всего документа и применимым СТО. Верни verification: decision (подтверждено/снято/недостаточно данных), reason, evidence. Для подтверждения или снятия нужны точные доказательства. Недостаточность данных не означает снятие. Отсутствие во всём документе нельзя подтвердить одной выборкой. Верни facts=[].'}[phase]
        profile={k:v for k,v in j['profile'].items() if k!='headings'}
        tables={};sections={}
        for block in t['blocks']:
            context=block.get('table_context')
            if context:tables[str(context['table'])]={k:v for k,v in context.items() if k not in ('row','cell')}
            sections[str(block.get('section',''))]={'path':block.get('heading_path',[]),'title':block.get('section_title',''),
                                                  'address':block.get('address','').split(';')[0]}
        compact=[{'locator':b['locator'],'offset':b.get('offset',0),'text':b['text'],'section':b.get('section',''),'is_heading':b.get('is_heading',False),
                  **{k:b.get('format',{})[k] for k in ('table','row','cell','column_span','vertical_merge') if k in b.get('format',{})}} for b in t['blocks']]
        return {'phase':phase,'task':task,'scope':j['scope'],'directions':directions,'topics':base.TOPICS,
                'document_profile':profile,'blocks':compact,'sources':[{k:s.get(k,'') for k in ('id','file','document','clause','appendix_title','heading','text','locator','end_locator','numbering_verified')} for s in sources],'sections':sections,'tables':tables,
                'format_metadata':base.model_metadata(t['blocks']) if phase=='parts' else {},
                'pairs_to_compare':t.get('pairs',[]),'candidates':t.get('candidates',[]),
                'page_layout':j['layout'],'search_scope':'Поиск по всем текстовым блокам; выборка не доказывает отсутствие сведений.'}

    def build_cross(self,j):
        facts=[f for t in j['tasks'] if t['phase']=='parts' for f in t.get('result',{}).get('facts',[])]
        # Stable exact entities compare all differing values, not two arbitrary neighbours.
        groups=defaultdict(list)
        for f in facts:groups[(f['topic'],base.normalized(f['entity']))].append(f)
        pairs=[];omitted=0
        for group in groups.values():
            values={}
            for f in group:values.setdefault(base.normalized(f['value']),f)
            values=list(values.values())
            possible=len(values)*(len(values)-1)//2
            take=min(possible,5000-len(pairs));omitted+=possible-take
            pairs.extend([a,b] for a,b in itertools.islice(itertools.combinations(values,2),take))
        # Also retain fuzzy candidates to catch differently worded entity names.
        unique,near,_=base.targeted_pairs(facts)
        seen={json.dumps(p,sort_keys=True,ensure_ascii=False) for p in pairs}
        for a,b in near:
            pair=[unique[a],unique[b]];signature=json.dumps(pair,sort_keys=True,ensure_ascii=False)
            if signature not in seen:
                if len(pairs)<5000:pairs.append(pair);seen.add(signature)
                else:omitted+=1
        for i in range(0,len(pairs),4):
            subset=pairs[i:i+4];anchors=[e['locator'] for pair in subset for f in pair for e in f['evidence']]
            blocks=search_units(j['units'],' '.join(f['entity'] for p in subset for f in p),anchors,budget=7500)
            j['tasks'].append({'id':'cross-'+str(i),'phase':'cross','blocks':blocks,'pairs':subset,'state':'pending','attempts':0})
        j['cross_stats']={'facts_received':len(facts),'selected_pairs':len(pairs),'entity_groups':len(groups),'omitted_due_budget':omitted}
        if omitted:j.setdefault('warnings',[]).append(f'Межраздельный бюджет 5000 пар исчерпан; не проверены {omitted} дополнительных кандидатов сравнения.')
        j['cross_built']=True

    def build_verification(self,j):
        candidates=base.unique_findings(j)
        for i,f in enumerate(candidates):
            anchors=[e['locator'] for e in f['evidence']]
            blocks=search_units(j['units'],f['issue']+' '+f['reason'],anchors,budget=8000)
            source=self.index.by_id.get((f.get('source') or {}).get('id'))
            j['tasks'].append({'id':'verify-'+str(i),'phase':'verify','blocks':blocks,'candidates':[f],
                              'sources':[source] if source else [],'state':'pending','attempts':0})
        j['verification_built']=True;j['candidate_count']=len(candidates)

    def subdivide(self,j,t,cursor):
        if t.get('depth',0)>=3:return False
        if t['phase']=='parts':
            groups=[]
            for block in t['blocks']:
                f=block.get('format',{});key=('row',f['table'],f['row']) if 'table' in f else ('p',block['locator'])
                if groups and groups[-1][0]==key:groups[-1][1].append(block)
                else:groups.append((key,[block]))
            if len(groups)<2:return False
            mid=len(groups)//2
            changes=[{'blocks':[b for _,row in half for b in row]} for half in (groups[:mid],groups[mid:])]
        elif t['phase']=='requirements' and len(t['sources'])>1:
            mid=len(t['sources'])//2
            changes=[{'sources':half,'blocks':search_units(j['units'],' '.join(s['text'] for s in half),budget=4500)} for half in (t['sources'][:mid],t['sources'][mid:])]
        else:return False
        for k,change in enumerate(changes):
            child={**t,**change,'id':t['id']+'.'+str(k),'depth':t.get('depth',0)+1,'state':'pending','attempts':0,'seconds':0}
            child.pop('error',None);child.pop('result',None);j['tasks'].insert(cursor+k,child)
        t['state']='split';return True

    def run(self,key):
        j=self.jobs[key]
        if j.get('pipeline_version')!=3:return super().run(key)
        with self.worker:
            j['status']='running'
            try:
                self.client=self.client or Client();self.index=self.index or rag.Index();cursor=0
                while True:
                    if j['cancel']:j['status']='paused';break
                    if self.memory()+16*1024**2>self.memory_limit:raise MemoryError('Лимит RAM')
                    if cursor==len(j['tasks']):
                        if not j['cross_built']:self.build_cross(j);continue
                        if not j['verification_built']:self.build_verification(j);continue
                        incomplete=any(c['status']=='недостаточно данных' for t in j['tasks'] for c in t.get('result',{}).get('requirement_checks',[]))
                        j['status']='partial' if incomplete or any(t['state'] in ('error','review') for t in j['tasks']) else 'done';break
                    t=j['tasks'][cursor];cursor+=1
                    if t['state'] in ('done','split','error','review'):continue
                    j['phase']=t['phase'];t['state']='running';t.setdefault('seconds',0)
                    payload=self.payload(j,t);n=self.client.count(payload);t['input_tokens']=n
                    if n+self.client.output+self.client.margin>self.client.context:
                        if self.subdivide(j,t,cursor):continue
                        # Reduce retrieval context, never fragment a table row.
                        if t['phase']!='parts' and len(t['blocks'])>1:
                            shorter=self.trim_context(t['blocks'])
                            if not shorter:
                                t.update(state='error',error='Целая строка таблицы не помещается в контекст');continue
                            t['blocks']=shorter;t['state']='pending';cursor-=1;continue
                        t.update(state='error',error='Структурная единица не помещается; требуется отдельная проверка');continue
                    for attempt in range(2):
                        if j['cancel']:break
                        started=time.monotonic();t['attempts']+=1
                        try:
                            raw,runtime=self.client.generate(payload)
                            try: result=base.validate(raw,payload)
                            except base.EvidenceFailure as exc:result=exc.result
                            if t['phase']=='requirements':
                                checks=raw.get('requirement_checks',[])
                                if {c.get('source_id') for c in checks}!={s['id'] for s in t['sources']}:raise ValueError('Не заполнен реестр нормативной проверки')
                                for check in checks:
                                    if check['status']=='проверено':base.evidence(check['evidence'],payload['blocks'])
                                result['requirement_checks']=checks
                            if t['phase']=='verify':
                                verification=raw.get('verification',{})
                                decision=verification.get('decision')
                                if decision not in ('подтверждено','снято','недостаточно данных'):raise ValueError('Нет решения повторной проверки')
                                if decision!='недостаточно данных':base.evidence(verification.get('evidence'),payload['blocks'])
                                if (decision=='подтверждено') != bool(result['findings']):
                                    if decision!='недостаточно данных':raise ValueError('Решение повторной проверки противоречит списку замечаний')
                                result['verification']=verification
                                if decision=='недостаточно данных':
                                    result['findings']=[]
                                    result.setdefault('rejected',[]).append({'kind':'verification','reason':verification.get('reason','Недостаточно данных'),'item':t['candidates'][0]})
                                accepted=[]
                                original_locations={e['locator'] for c in t['candidates'] for e in c['evidence']}
                                for f in result['findings']:
                                    if ABSENCE.search(f['issue']):
                                        result.setdefault('rejected',[]).append({'kind':'findings','reason':'Отсутствие во всём документе не доказано','item':f})
                                    elif not original_locations.intersection(e['locator'] for e in f['evidence']):
                                        result.setdefault('rejected',[]).append({'kind':'findings','reason':'Повторный проход предложил замечание без связи с исходным кандидатом','item':f})
                                    else:accepted.append(f)
                                result['findings']=accepted
                            if 'response_budget_instruction' in payload:
                                result.setdefault('rejected',[]).append({'kind':'coverage','reason':'Повторный ответ ограничен по числу результатов; полнота прохода не подтверждена','item':{}})
                            t.update(result=result,runtime=runtime,state='review' if result.get('rejected') else 'done');t.pop('error',None);break
                        except base.OutputLimit:
                            if self.subdivide(j,t,cursor):break
                            if attempt==0:
                                payload={**payload,'response_budget_instruction':'Сократи пояснения до 1 предложения. Не более 4 замечаний и 6 фактов. Если существенные результаты не помещаются, явно укажи неполноту в limitations.'}
                            else:t.update(state='error',error='Ответ не поместился после краткого повторного прохода')
                        except Exception as exc:
                            t.update(state='error',error=str(exc)[:600])
                            if attempt==0:payload={**payload,'validation_feedback':str(exc)[:300]};t['state']='running'
                        finally:t['seconds']+=round(time.monotonic()-started,3)
                    if j['cancel'] and t['state']=='running':t['state']='pending'
                    if cursor%8==0:self.report(j)
                self.report(j)
            except Exception as exc:
                j.update(status='paused',error=str(exc)[:1000])
                for t in j['tasks']:
                    if t['state']=='running':t['state']='pending'
                self.report(j)

    @staticmethod
    def trim_context(blocks):
        last=blocks[-1];fmt=last.get('format',{})
        if 'table' in fmt:
            shorter=[b for b in blocks if (b.get('format',{}).get('table'),b.get('format',{}).get('row'))!=(fmt['table'],fmt['row'])]
        else:shorter=blocks[:-1]
        return shorter

    def coverage(self,j):
        return {p:dict(Counter(t['state'] for t in j['tasks'] if t['phase']==p)) for p in ('parts','requirements','cross','verify')}

    def estimate(self,j):
        if j.get('pipeline_version')!=3:return super().estimate(j)
        done=[t for t in j['tasks'] if t.get('seconds') and t['state'] in ('done','review','error')]
        if len(done)<3:return None
        mean=sum(t['seconds'] for t in done)/len(done)
        remaining=sum(t['state'] in ('pending','running') for t in j['tasks'])
        return {'sample_roots':len(done),'remaining_minutes_low':round(remaining*mean*.8/60),
                'remaining_minutes_high':round(remaining*mean*1.5/60),'note':'До формирования межраздельных задач и перепроверки оценка неполная.'}

    def report(self,j):
        if j.get('pipeline_version')!=3:return super().report(j)
        from document_locations import attach_locations,render_report
        candidates=base.unique_findings({**j,'tasks':[t for t in j['tasks'] if t['phase']!='verify']})
        findings=base.unique_findings({**j,'tasks':[t for t in j['tasks'] if t['phase']=='verify']})
        checks=[{'task':t['id'],**c} for t in j['tasks'] for c in t.get('result',{}).get('requirement_checks',[])]
        limitations=[{'task':t['id'],'text':w} for t in j['tasks'] for w in t.get('result',{}).get('limitations',[])]
        warnings=j['warnings']+['В основной список включены только замечания после повторной проверки. Кандидаты и неподтверждённые элементы сохранены отдельно.',
                 'Результаты моделей требуют оценки специалиста. Цитата не является гарантией верной интерпретации.',
                 'Нормативные фрагменты с недостаточными данными: '+str(sum(c['status']=='недостаточно данных' for c in checks))]
        result={'document':j['path'],'sha256':j['sha256'],'status':j['status'],'findings':findings,'candidates':candidates,
                'errors':[{'task':t['id'],'state':t['state'],'error':t.get('error','Неподтверждённые элементы')} for t in j['tasks'] if t['state'] in ('error','review')],
                'warnings':warnings,'coverage':self.coverage(j),'normative_catalog':j['source_catalog'],'requirement_checks':checks,
                'limitations':limitations,'cross_stats':j.get('cross_stats',{}),
                'verification_decisions':[{'task':t['id'],'candidate':t['candidates'][0]['issue'],**t['result']['verification']} for t in j['tasks'] if t['phase']=='verify' and 'verification' in t.get('result',{})],
                'unverified':[{'task':t['id'],**r} for t in j['tasks'] for r in t.get('result',{}).get('rejected',[])]}
        attach_locations(result)
        dest=rag.HERE/'reports'/('batch-'+j['id']);rag.dump(dest.with_suffix('.json'),result)
        md=render_report(result)+'\n\n## Реестр нормативного прохода\n\n'
        md+='| Источник | Результат | Обоснование |\n|---|---|---|\n'
        byid={c['id']:c for c in j['source_catalog']}
        for c in checks:
            source=byid[c['source_id']]
            cells=[source['document']+'; '+source['clause'],c['status'],c['reason']]
            md+='| '+' | '.join(str(x).replace('|','/').replace('\n',' ') for x in cells)+' |\n'
        checked={c['source_id'] for c in checks}
        for source in j['source_catalog']:
            if source['id'] not in checked:
                md+='| '+source['document']+'; '+source['clause']+' | не проверено | Проход ожидается или завершился ошибкой |\n'
        md+='\n## Пределы отдельных проверок\n\n'+'\n'.join('- '+x['task']+': '+x['text'] for x in limitations)
        md+='\n\n## Кандидаты, требующие завершения перепроверки\n\n'
        pending=[t for t in j['tasks'] if t['phase']=='verify' and t['state'] not in ('done','review')]
        md+='Всего кандидатов: '+str(len(candidates))+'. Ожидают завершения перепроверки: '+str(len(pending) if j['verification_built'] else len(candidates))+'.\n'
        md+='\n## Решения повторной проверки\n\n'
        for v in result['verification_decisions']:
            md+='- '+v['decision']+': '+v['candidate']+'. '+v['reason']+'\n'
        dest.with_suffix('.md').write_text(md,encoding='utf-8');j['report']=str(dest.with_suffix('.md'));j['report_json']=str(dest.with_suffix('.json'))

    def status(self,key,offset=0):
        result=super().status(key,offset);j=self.jobs[key]
        if j.get('pipeline_version')==3:
            fs=base.unique_findings({**j,'tasks':[t for t in j['tasks'] if t['phase']=='verify']})
            result.update(total_findings=len(fs),findings=fs[offset:offset+5],candidate_count=j.get('candidate_count',0),
                          next_offset=offset+5 if offset+5<len(fs) else None,phase=j['phase'])
        return result

    def action(self,key,action):
        j=self.jobs[key]
        if j.get('pipeline_version')!=3 or action=='pause':return super().action(key,action)
        if action!='resume':raise ValueError('Неизвестное действие')
        with self.lock:
            if j['status']!='paused':raise ValueError('Продолжение доступно только для приостановленной проверки')
            if hashlib.sha256(Path(j['path']).read_bytes()).hexdigest()!=j['sha256']:raise ValueError('Документ изменился')
            if self.worker.locked():raise ValueError('Служба занята')
            j.update(cancel=False,status='queued');j.pop('error',None)
            threading.Thread(target=self.run,args=(key,),daemon=True).start()
        return self.status(key)
