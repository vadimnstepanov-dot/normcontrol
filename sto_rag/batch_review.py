"""Bounded, stateless normcontrol. Source snapshots and task state live in RAM."""
import copy
import hashlib
import itertools
import json
import re
import math
import statistics
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import rag
from sections import plan, model_metadata
from context_guard.guard import Backend

CATEGORIES = ['СТО', 'общая логика', 'техническая логика', 'грамотность']
TOPICS = ['идентификация', 'архитектура', 'данные', 'интерфейсы', 'безопасность',
          'отказоустойчивость', 'производительность', 'эксплуатация', 'сроки', 'терминология']
SYSTEM = rag.SYSTEM + '''
Выполни комплексный нормоконтроль по четырём направлениям: СТО, общая логика,
техническая логика и грамотность. Проверяй согласованность терминов, чисел, версий,
ролей, процессов, архитектуры, интерфейсов, условий испытаний и восстановления.
Не подменяй техническую оценку стилистическими предпочтениями. Для каждого
нарушения СТО обязательно приведи source_id и дословную quote из sources,
объясни применимость. Для остальных категорий не приписывай своё мнение СТО.
У каждого замечания evidence: список {locator, offset, quote} с точными цитатами
из blocks. offset — смещение начала переданного блока, не самой цитаты.
Для противоречия между фрагментами нужны обе цитаты и оба места.
Пропуски вне переданной части и сомнительные требования — только требует уточнения.
Оформление оценивай только в пределах переданных свойств OOXML, не выдумывай страницы.
Составь facts: факты с topic из заданного списка, entity, value и evidence.
Извлекай существенные факты для межраздельной проверки; гипотезы не являются фактами.
checklist должен содержать все заданные directions со значением проверено или
неприменимо; последнее объясни в limitations. Пустые findings допустимы.
Не вызывай инструменты. Не продолжай чтение самостоятельно. Верни один JSON.
Не создавай замечания с пустой цитатой. Отсутствующие сведения и пределы охвата
опиши в limitations. Для каждой цитаты копируй locator и offset её блока.
Пиши кратко. В findings включай только конкретные проблемы, не описания правильных
элементов и не «нарушений не выявлено». Не считай перенос строки или пробел,
обусловленный извлечением Word, доказательством ошибки оформления.
facts — только существенные технические параметры, версии и определения терминов.
Не включай подписи, согласования, пустые даты, заголовки и повтор одинакового факта.
entity называй устойчиво: объект / параметр / условия; RTO и RPO называй именно так.
Цитируй краткий достаточный фрагмент, не переписывай абзац целиком.
'''
FORMAT = {'checklist': {c: 'проверено' for c in CATEGORIES}, 'limitations': [],
          'findings': [{'category': 'СТО', 'status': 'требует уточнения', 'issue': 'проблема',
                        'reason': 'обоснование и применимость', 'recommendation': 'правка или вопрос',
                        'evidence': [{'locator': 'p1', 'offset': 0, 'quote': 'точная цитата'}],
                        'source_id': 'только для СТО', 'quote': 'точная цитата СТО'}],
          'facts': [{'topic': TOPICS[0], 'entity': 'объект/параметр', 'value': 'значение',
                     'evidence': [{'locator': 'p1', 'offset': 0, 'quote': 'точная цитата'}]}]}


def response_schema(directions):
    def obj(fields): return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}
    text = {'type': 'string'}
    nonempty = {'type': 'string', 'minLength': 1}
    ev = {'type': 'array', 'minItems': 1, 'items': obj({'locator': nonempty, 'offset': {'type': 'integer', 'minimum': 0}, 'quote': nonempty})}
    return obj({'checklist': obj({c: {'type': 'string', 'enum': ['проверено', 'неприменимо']} for c in directions}),
                'limitations': {'type': 'array', 'items': text},
                'findings': {'type': 'array', 'items': obj({'category': {'type': 'string', 'enum': directions},
                    'issue': nonempty, 'reason': nonempty, 'recommendation': nonempty, 'evidence': ev,
                    'source_id': text, 'quote': text})},
                'facts': {'type': 'array', 'items': obj({'topic': {'type': 'string', 'enum': TOPICS},
                    'entity': nonempty, 'value': nonempty, 'evidence': ev})}})


def split_blocks(blocks):
    if len(blocks) > 1:
        mid = len(blocks) // 2
        return [blocks[:mid], blocks[mid:]]
    b = blocks[0]
    if len(b['text']) < 2:
        raise ValueError('Даже минимальный блок не помещается: уменьшите инструкции/метаданные.')
    mid = len(b['text']) // 2
    boundaries = [m.end() for m in re.finditer(r'(?<=[.!?;])\s+|\s+', b['text'])
                  if len(b['text'])//4 <= m.end() <= 3*len(b['text'])//4]
    if boundaries: mid = min(boundaries, key=lambda n: abs(n-mid))
    return [[{**b, 'text': b['text'][:mid]}],
            [{**b, 'text': b['text'][mid:], 'offset': b.get('offset', 0) + mid}]]


def evidence(items, blocks):
    if not isinstance(items, list) or not items:
        raise ValueError('Нет доказательства в документе')
    verified = []
    for e in items:
        if not isinstance(e, dict) or not isinstance(e.get('quote'), str) or not e['quote'].strip():
            raise ValueError('Пустая или неверная цитата')
        b = next((b for b in blocks if b['locator'] == e.get('locator')
                  and b.get('offset', 0) == e.get('offset', 0) and e['quote'] in b['text']), None)
        if b is None:
            matches = [x for x in blocks if e['quote'] in x['text']]
            if len(matches) == 1:
                b = matches[0]
            else:
                pattern = r'\s+'.join(re.escape(s) for s in e['quote'].strip().split())
                candidates = [(x, re.search(pattern, x['text'])) for x in blocks]
                candidates = [(x,m) for x,m in candidates if m]
                located = [(x,m) for x,m in candidates if x['locator']==e.get('locator') and x.get('offset',0)==e.get('offset',0)]
                if len(located)==1: candidates=located
                if len(candidates)!=1:
                    raise ValueError('Цитата отсутствует или её место неоднозначно: '+e['quote'][:100])
                b,m = candidates[0]
                e = {**e, 'quote': m.group(0)}
        verified.append({'locator': b['locator'], 'offset': b.get('offset', 0), 'quote': e['quote']})
    return verified


def _validate(raw, payload):
    if not isinstance(raw, dict) or not isinstance(raw.get('findings'), list) or not isinstance(raw.get('facts'), list):
        raise ValueError('Неверная структура findings/facts')
    check = raw.get('checklist', {})
    if any(check.get(c) not in ('проверено', 'неприменимо') for c in payload['directions']):
        raise ValueError('Не подтверждён проход по всем направлениям')
    if not isinstance(raw.get('limitations'), list):
        raise ValueError('Нет списка ограничений')
    findings, facts = [], []
    for f in raw['findings']:
        if f.get('category') not in payload['directions']:
            raise ValueError('Неверная категория замечания')
        ev = evidence(f.get('evidence'), payload['blocks'])
        if payload['phase'] == 'cross' and len({(e['locator'], e['offset']) for e in ev}) < 2:
            raise ValueError('Межраздельное замечание требует двух мест')
        for key in ('issue', 'reason', 'recommendation'):
            if not isinstance(f.get(key), str) or not f[key].strip():
                raise ValueError('Неполное замечание: ' + key)
        ref = None
        if f['category'] == 'СТО':
            ref = rag.verified_ref(f, payload['sources'])
            if not ref:
                raise ValueError('Неподтверждённая ссылка СТО')
        findings.append({k: f[k] for k in ('category', 'issue', 'reason', 'recommendation')} |
                        {'status': 'требует оценки специалиста', 'evidence': ev, 'source': ref})
    for f in raw['facts']:
        if f.get('topic') not in TOPICS or not isinstance(f.get('entity'), str) or not isinstance(f.get('value'), str):
            raise ValueError('Неверный факт')
        facts.append({k: f[k] for k in ('topic', 'entity', 'value')} |
                     {'evidence': evidence(f.get('evidence'), payload['blocks'])})
    return {'findings': findings, 'facts': facts, 'limitations': raw['limitations'], 'checklist': check}


class EvidenceFailure(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__('; '.join(x['reason'] for x in result['rejected'])[:800])


class OutputLimit(ValueError):
    pass


def validate(raw, payload):
    # A bad citation must not erase other independently verified findings or facts.
    if not isinstance(raw, dict) or not isinstance(raw.get('findings'), list) or not isinstance(raw.get('facts'), list):
        raise ValueError('Неверная структура findings/facts')
    result = _validate({**raw, 'findings': [], 'facts': []}, payload)
    result['rejected'] = []
    for kind in ('findings', 'facts'):
        for item in raw[kind]:
            candidate = {**raw, 'findings': [], 'facts': [], kind: [item]}
            try: result[kind].extend(_validate(candidate, payload)[kind])
            except (ValueError, TypeError, AttributeError) as e:
                result['rejected'].append({'kind': kind, 'reason': str(e), 'item': item})
    if result['rejected']: raise EvidenceFailure(result)
    return result


def merge_results(previous, current):
    for key in ('findings', 'facts', 'limitations'):
        for item in previous.get(key, []):
            if item not in current[key]: current[key].append(item)
    return current


class Client:
    def __init__(self, port=8082):
        self.backend = Backend(port)
        self.url = f'http://127.0.0.1:{port}'
        slots = rag.local_request(self.url + '/slots')
        self.context = min(s['n_ctx'] for s in slots)
        self.output = 3072
        self.margin = 2048

    def request(self, payload):
        return {'model': 'local-qwen', 'messages': [
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
            'tools': [], 'tool_choice': 'none', 'temperature': 0.1, 'max_tokens': self.output,
            'stream': False, 'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'normcontrol', 'strict': True, 'schema': response_schema(payload['directions'])}},
            'chat_template_kwargs': {'enable_thinking': False, 'preserve_thinking': False}}

    def count(self, payload):
        return self.backend.count(self.request(payload))

    def generate(self, payload):
        result = rag.local_request(self.url + '/v1/chat/completions', self.request(payload), timeout=600)
        c = result['choices'][0]
        if c.get('finish_reason') == 'length':
            raise OutputLimit('Ответ не поместился в бюджет; часть нужно уменьшить')
        if c.get('finish_reason') != 'stop' or c['message'].get('tool_calls'):
            raise ValueError('Ответ не завершён или модель вызвала инструменты')
        return json.loads(c['message']['content']), {'usage': result.get('usage', {}), 'timings': result.get('timings', {})}


def pack_groups(groups, target_chars=3000, max_blocks=64):
    """Pack neighbouring headings with content; retain every source block and offset."""
    packed, current, size = [], [], 0
    for block in (b for g in groups for b in g):
        if current and (size+len(block['text'])>target_chars or len(current)>=max_blocks):
            packed.append(current);current=[];size=0
        current.append(block);size+=len(block['text'])
    if current:packed.append(current)
    return packed


def normalized(s):
    return ' '.join(str(s).casefold().split())


def unique_findings(j):
    result=[];seen=set()
    for t in j['tasks']:
        for f in t.get('result',{}).get('findings',[]):
            if re.fullmatch(r'(?:не требуется|нет замечаний|нарушений не выявлено)[.!\s]*',normalized(f.get('recommendation',''))):continue
            key=(f['category'],(f.get('source') or {}).get('id'),normalized(f['issue']),
                 tuple(sorted((e['locator'],e['offset'],normalized(e['quote'])) for e in f['evidence'])))
            if key not in seen:seen.add(key);result.append({'task':t['id'],**f})
    return result


def targeted_pairs(facts, neighbors=2):
    """At most two differing-value neighbours per fact, via an entity inverted index."""
    unique=[];seen=set();postings=defaultdict(list);tokens=[]
    for f in facts:
        key=(f['topic'],normalized(f['entity']),normalized(f['value']))
        if key in seen:continue
        seen.add(key);unique.append(f)
        words=set(rag.tokens(f['entity']))
        for special in ('rto','rpo'):
            if re.search(r'\b'+special+r'\b',f['entity'],re.I):words.add(special)
        tokens.append(words)
        for word in words:postings[(f['topic'],word)].append(len(unique)-1)
    pairs=set();candidates_considered=0
    for i,f in enumerate(unique):
        candidates=set()
        for word in tokens[i]:candidates.update(postings[(f['topic'],word)])
        ranked=[]
        for k in candidates:
            if k==i or normalized(unique[k]['value'])==normalized(f['value']):continue
            if {e['locator'] for e in unique[k]['evidence']}=={e['locator'] for e in f['evidence']}:continue
            shared=len(tokens[i]&tokens[k]);union=len(tokens[i]|tokens[k])
            score=shared/max(1,union)
            exact=normalized(f['entity'])==normalized(unique[k]['entity'])
            if exact or score>=.5:
                ranked.append((int(exact),score,-k,k));candidates_considered+=1
        for *_,k in sorted(ranked,reverse=True)[:neighbors]:pairs.add(tuple(sorted((i,k))))
    return unique,sorted(pairs),{'facts_received':len(facts),'distinct_facts':len(unique),
                               'matching_candidates':candidates_considered,'selected_pairs':len(pairs),
                               'neighbors_per_fact':neighbors}


class Engine:
    def __init__(self, client=None, index=None, memory_limit=4 * 1024**3):
        self.client = client
        self.index = index
        self.jobs = {}
        self.lock = threading.RLock()
        self.worker = threading.Lock()
        self.memory_limit = memory_limit

    def memory(self):
        # Conservative allowance for Python objects plus serialization and request buffers.
        return sum(len(json.dumps(j, ensure_ascii=False).encode()) * 4 for j in self.jobs.values())

    def start(self, path, scope=''):
        p = Path(path).resolve(strict=True)
        if p.suffix.lower() != '.docx':
            raise ValueError('Нужен DOCX')
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        with self.lock:
            for j in self.jobs.values():
                if j.get('pipeline_version')==2 and j['path'] == str(p) and j['sha256'] == digest and j['scope'] == scope:
                    return self.status(j['id'])
            if any(j['status'] in ('queued', 'running', 'cancelling') for j in self.jobs.values()):
                raise ValueError('Другая проверка выполняется. Дождитесь её завершения или приостановите.')
            d = plan(p)
            d['groups'] = pack_groups(d['groups'])
            if d['sha256'] != digest:
                raise ValueError('Документ изменился при открытии')
            key = uuid.uuid4().hex
            j = {'id': key, 'pipeline_version':2, 'started_epoch':time.time(), 'root_calls':{},
                 'initial_parts':len(d['groups']), 'path': str(p), 'sha256': digest, 'scope': scope, 'status': 'queued',
                 'cancel': False, 'phase': 'parts', 'layout': d['layout'], 'warnings': d['warnings'],
                 'tasks': [{'id': str(i), 'phase': 'parts', 'blocks': g, 'state': 'pending', 'attempts': 0}
                           for i, g in enumerate(d['groups'], 1)], 'cross_built': False,
                 'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'report': None}
            estimate = len(json.dumps(j, ensure_ascii=False).encode()) * 4
            if self.memory() + estimate > self.memory_limit:
                raise MemoryError('Лимит RAM: существующие результаты сохранены, новая задача не принята')
            self.jobs[key] = j
        threading.Thread(target=self.run, args=(key,), daemon=True).start()
        return self.status(key)

    def payload(self, job, task):
        blocks = task['blocks']
        sources = [] if task['phase'] == 'cross' else self.index.search(
            job['scope'] + ' ' + blocks[0].get('section_title', '') + ' ' + '\n'.join(b['text'] for b in blocks), 6)
        directions = CATEGORIES if task['phase'] == 'parts' else ['общая логика', 'техническая логика']
        return {'phase': task['phase'], 'task': 'Проверь часть документа' if task['phase'] == 'parts' else
                'Проверь только заданные пары сравнения. Учитывай разные объекты и условия. Не придумывай противоречий. Верни facts=[]; новые факты извлекать не нужно.',
                'scope': job['scope'], 'directions': directions, 'topics': TOPICS,
                'blocks': [{'locator': b['locator'], 'offset': b.get('offset', 0), 'text': b['text'],
                            'section': b.get('section_title', '')} for b in blocks],
                'format_metadata': model_metadata(blocks) if task['phase']=='parts' else {}, 'page_layout': job['layout'] if task['phase']=='parts' else [],
                'sources': sources, 'facts_to_compare': task.get('facts', []), 'pairs_to_compare':task.get('pairs',[]), 'format': FORMAT}

    def build_cross(self, j):
        facts=[f for t in j['tasks'] if t['phase']=='parts' for f in t.get('result',{}).get('facts',[])]
        unique,pairs,stats=targeted_pairs(facts)
        j['cross_facts']=unique;j['cross_pairs']=pairs;j['cross_stats']=stats;j['cross_position']=0
        j['cross_lookup']={b['locator']+'@'+str(b.get('offset',0)):b for t in j['tasks'] if t['phase']=='parts' for b in t['blocks']}
        j['cross_built']=True
        j['warnings'].append('Межраздельная проверка адресная: до двух соседних фактов с другим значением и совпадающим/близким названием параметра. Все пары не перебираются; факты с разными названиями могут остаться несопоставленными.')

    def next_cross(self, j):
        at=j['cross_position'];pairs=j['cross_pairs'][at:at+8]
        if not pairs:return None
        j['cross_position']+=len(pairs)
        facts=[j['cross_facts'][i] for i in sorted({i for pair in pairs for i in pair})]
        refs={(e['locator'],e['offset']) for f in facts for e in f['evidence']}
        blocks=[j['cross_lookup'][loc+'@'+str(off)] for loc,off in sorted(refs)]
        return {'id':'cross-'+str(at),'phase':'cross','blocks':blocks,'facts':facts,
                'state':'pending','attempts':0,'pairs':[[j['cross_facts'][a],j['cross_facts'][b]] for a,b in pairs]}

    def split_task(self,j,t,cursor,reason):
        if t['phase']=='cross':
            pairs=t.get('pairs',[])
            if len(pairs)<2:raise ValueError('Минимальная пара не помещается; требуется отдельная проверка')
            mid=len(pairs)//2;children=[]
            for subset in (pairs[:mid],pairs[mid:]):
                facts=[]
                for pair in subset:
                    for f in pair:
                        if f not in facts:facts.append(f)
                refs={(e['locator'],e['offset']) for f in facts for e in f['evidence']}
                children.append({'pairs':subset,'facts':facts,'blocks':[b for b in t['blocks'] if (b['locator'],b.get('offset',0)) in refs]})
        else:children=[{'blocks':g} for g in split_blocks(t['blocks'])]
        for k,fields in enumerate(children):
            j['tasks'].insert(cursor+k,{'id':t['id']+'.'+str(k),'phase':t['phase'],'state':'pending','attempts':0,**fields})
        t.update(state='split',split_reason=reason)

    def run(self, key):
        with self.worker:
            j=self.jobs[key];j['status']='running';j.setdefault('root_calls',{})
            try:
                self.client=self.client or Client();self.index=self.index or rag.Index();cursor=0
                while True:
                    if j['cancel']:j['status']='paused';break
                    if cursor%32==0 and self.memory()+16*1024**2>self.memory_limit:
                        raise MemoryError('Лимит RAM. Состояние сохранено; обработка приостановлена.')
                    if cursor==len(j['tasks']):
                        if not j['cross_built']:self.build_cross(j);continue
                        cross=self.next_cross(j)
                        if cross:j['tasks'].append(cross);continue
                        j['status']='partial' if any(t['state'] in ('error','review') for t in j['tasks']) else 'done'
                        break
                    t=j['tasks'][cursor];cursor+=1
                    if t['state'] in ('done','split','error','review'):continue
                    j['phase']=t['phase'];t['state']='running';t.setdefault('seconds',0)
                    payload=self.payload(j,t);n=self.client.count(payload);t['input_tokens']=n
                    if n+self.client.output+self.client.margin>self.client.context:
                        try:self.split_task(j,t,cursor,'input_budget')
                        except ValueError as e:t.update(state='error',error=str(e))
                        continue
                    for attempt in range(2):
                        if j['cancel']:break
                        root=t['id'].split('.')[0]
                        if j['root_calls'].get(root,0)>=4:
                            t.update(state='error',error='Лимит 4 запросов на исходную часть; требуется отдельная проверка');break
                        j['root_calls'][root]=j['root_calls'].get(root,0)+1
                        t['attempts']+=1;t['active_since']=time.time();started=time.monotonic()
                        runtime={}
                        try:
                            raw,runtime=self.client.generate(payload)
                            t['result']=merge_results(t.get('result',{}),validate(raw,payload))
                            t.update(state='done',runtime=runtime);t.pop('error',None);break
                        except EvidenceFailure as e:
                            # Citation defects are quarantined, never a reason to reread/split a part.
                            t['result']=merge_results(t.get('result',{}),e.result)
                            t.update(state='review',error='Неподтверждённых элементов: '+str(len(e.result['rejected'])),runtime=runtime)
                            break
                        except OutputLimit as e:
                            # Only one generation-driven split level, bounded by root call budget.
                            if '.' not in t['id'] and (len(t['blocks'])>1 or len(t['blocks'][0]['text'])>256):
                                try:self.split_task(j,t,cursor,'output_budget')
                                except ValueError as exc:t.update(state='error',error=str(exc))
                            else:t.update(state='error',error=str(e))
                            break
                        except Exception as e:
                            t.update(state='running' if attempt==0 else 'error',error=str(e)[:800])
                            correction={**payload,'validation_feedback':'Исправь JSON/структуру. Кратко. Ошибка: '+str(e)[:300]}
                            if self.client.count(correction)+self.client.output+self.client.margin<=self.client.context:payload=correction
                        finally:
                            t['seconds']+=round(time.monotonic()-started,3);t.pop('active_since',None)
                    if j['cancel'] and t['state']=='running':t['state']='pending'
                self.report(j)
            except Exception as e:
                j.update(status='paused',error=str(e)[:1000])
                for t in j['tasks']:
                    if t['state']=='running':t['state']='pending'
                self.report(j)

    def report(self, j):
        findings = unique_findings(j)
        errors = [{'task': t['id'], 'state': t['state'], 'error': t.get('error', 'не завершено')}
                  for t in j['tasks'] if t['state'] not in ('done', 'split')]
        warnings = list(j['warnings'])
        try:
            if hashlib.sha256(Path(j['path']).read_bytes()).hexdigest() != j['sha256']:
                warnings.append('Исходный файл изменился; отчёт относится к снимку при запуске.')
        except OSError:
            warnings.append('Исходный файл больше недоступен; отчёт относится к снимку при запуске.')
        for t in j['tasks']:
            warnings.extend(f"Часть {t['id']}: {w}" for w in t.get('result', {}).get('limitations', []))
        warnings += ['Найденные выдержки СТО не гарантируют охват всех требований. Отсутствие замечаний не подтверждает соответствие.',
                     'Проверка цитат подтверждает их наличие, но не правильность интерпретации.']
        result = {'document': j['path'], 'sha256': j['sha256'], 'status': j['status'], 'findings': findings,
                  'errors': errors, 'warnings': warnings, 'coverage': self.coverage(j),
                  'unverified':[{'task':t['id'],**r} for t in j['tasks'] for r in t.get('result',{}).get('rejected',[])],
                  'cross_stats':j.get('cross_stats',{}), 'estimate':self.estimate(j),
                  'parts': [{k: t[k] for k in ('id', 'phase', 'state', 'attempts', 'input_tokens', 'seconds') if k in t}
                            for t in j['tasks']]}
        dest = rag.HERE/'reports'/('batch-'+j['id'])
        from document_locations import attach_locations, render_report
        attach_locations(result)
        rag.dump(dest.with_suffix('.json'), result)
        dest.with_suffix('.md').write_text(render_report(result), encoding='utf-8')
        j['report'] = str(dest.with_suffix('.md'))
        j['report_json'] = str(dest.with_suffix('.json'))

    def coverage(self, j):
        return {phase: dict(Counter(t['state'] for t in j['tasks'] if t['phase'] == phase)) for phase in ('parts', 'cross')}

    def estimate(self,j):
        if j.get('pipeline_version')!=2:return None
        roots=defaultdict(list)
        for t in j['tasks']:
            if t['phase']=='parts':roots[t['id'].split('.')[0]].append(t)
        costs=[sum(t.get('seconds',0) for t in group) for group in roots.values()
               if all(t['state'] not in ('pending','running') for t in group)]
        if not costs:return {'sample_roots':0,'initial_parts':j.get('initial_parts',len(roots))}
        mean=statistics.mean(costs);remaining_roots=len(roots)-len(costs)
        if j['cross_built']:pairs=j.get('cross_stats',{}).get('selected_pairs',0)
        else:
            facts=[f for t in j['tasks'] for f in t.get('result',{}).get('facts',[])]
            _,sample_pairs,_=targeted_pairs(facts)
            pairs=math.ceil(len(sample_pairs)*len(roots)/max(1,len(costs)))
        cross_tasks=[t for t in j['tasks'] if t['phase']=='cross']
        measured_cross=[t['seconds'] for t in cross_tasks if t.get('seconds') and t['state'] not in ('pending','running')]
        cross_mean=statistics.mean(measured_cross) if measured_cross else mean
        cross_left=max(0,math.ceil(pairs/8)-sum(t['state'] not in ('pending','running','split') for t in cross_tasks))
        midpoint=remaining_roots*mean+cross_left*cross_mean
        return {'sample_roots':len(costs),'initial_parts':len(roots),'mean_root_seconds':round(mean,1),
                'estimated_cross_pairs':pairs,'cross_measured':len(measured_cross),
                'remaining_minutes_low':round(midpoint*.65/60),'remaining_minutes_high':round(midpoint*1.6/60),
                'note':'Предварительная экстраполяция; межраздельное время уточняется после извлечения фактов.'}

    def status(self, key, offset=0):
        with self.lock:
            j = self.jobs[key]
            findings = unique_findings(j)
            return {k: j.get(k) for k in ('id', 'path', 'status', 'phase', 'report', 'report_json', 'error')} | {
                'job_id': key, 'pipeline_version':j.get('pipeline_version',1), 'coverage': self.coverage(j), 'findings': copy.deepcopy(findings[offset:offset+5]),
                'estimate':self.estimate(j), 'unverified_count':sum(len(t.get('result',{}).get('rejected',[])) for t in j['tasks']),
                'total_findings': len(findings), 'next_offset': offset+5 if offset+5 < len(findings) else None,
                'errors': [{'task': t['id'], 'error': t.get('error')} for t in j['tasks'] if t['state'] == 'error'][:10],
                'notice': 'Очередью управляет программа. Заверши ответ; не опрашивай статус в цикле. Ход проверки: http://127.0.0.1:8095/'}

    def action(self, key, action):
        with self.lock:
            j = self.jobs[key]
            if action == 'pause':
                j['cancel'] = True
                if j['status'] == 'running': j['status'] = 'cancelling'
            elif action == 'resume':
                if j.get('pipeline_version')!=2:raise ValueError('Это архив старого обработчика. Запустите новую проверку по тому же пути.')
                if j['status'] not in ('paused', 'partial'):
                    raise ValueError('Продолжение возможно только для остановленной или частичной проверки')
                if any(x['status'] in ('running', 'queued', 'cancelling') for x in self.jobs.values()):
                    raise ValueError('Другая проверка выполняется')
                if hashlib.sha256(Path(j['path']).read_bytes()).hexdigest() != j['sha256']:
                    raise ValueError('Файл изменился; нужна новая проверка')
                if not any(t['state'] in ('pending','error') for t in j['tasks']):
                    raise ValueError('Все части обработаны. Неподтверждённые элементы требуют проверки; автоматическое перечитывание отключено.')
                # Rebuild cross comparisons after failed local parts have been repaired.
                if any(t['phase'] == 'parts' and t['state'] not in ('done', 'split') for t in j['tasks']):
                    j['tasks'] = [t for t in j['tasks'] if t['phase'] == 'parts']; j['cross_built'] = False
                for t in j['tasks']:
                    if t['state'] == 'error':
                        t['state'] = 'pending'
                        j.setdefault('root_calls',{})[t['id'].split('.')[0]]=0
                j.update(cancel=False, status='queued'); j.pop('error', None)
                threading.Thread(target=self.run, args=(key,), daemon=True).start()
            else: raise ValueError('Неизвестное действие')
        return self.status(key)
