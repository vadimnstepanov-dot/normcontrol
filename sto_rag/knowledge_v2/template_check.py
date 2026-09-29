"""Source-bound template comparison. No model calls and no inferred compliance.

The template comes from originals pinned by the release, not model knowledge.
Content decisions are attached from the ordinary normative review afterwards.
"""
import re
from pathlib import Path
from .ingest import parse, sha256, PARSER_VERSION
from .store import checksum, Conflict

VERSION = 'sto-template-v1'
TITLE = 'Сопоставление структуры и содержания с шаблоном СТО'
HEADERS = ['Элемент шаблона', 'Элемент документа', 'Результат проверки структуры']
_TITLE = re.compile(r'шаблон\s+документа\s*[«“"]([^»”"]+)', re.I)
_NUMBER = re.compile(r'^(\d+(?:\.\d+)*)[.]?\s+(.+)$')
_FRONT = {'аннотация', 'обозначения и сокращения', 'перечень обозначений и сокращений',
          'лист регистрации изменений', 'составили', 'согласовано'}


def normalized(text):
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ').replace('\u00ad', '')).strip().casefold().replace('ё', 'е')


def extract_templates(parsed):
    """Keep exact source sections, including body locators for later decisions."""
    templates = []; current = None; element = None
    conditional_changes=any('лист регистрации изменений' in normalized(b['exact_text']) and
        ('при необходимости' in normalized(b['exact_text']) or 'следует включать только' in normalized(b['exact_text'])) for b in parsed['blocks'])
    for b in parsed['blocks']:
        text = b['exact_text']; match = _TITLE.search(text)
        if match and b.get('is_heading'):
            current = dict(name=match.group(1), locator=b['locator'], elements=[])
            templates.append(current); element = None
            continue
        if current is None:
            continue
        if b.get('is_heading') and re.match(r'^Приложение\s+', text, re.I):
            current = None; element = None; continue
        number = _NUMBER.match(text.strip())
        is_element = b.get('kind') == 'paragraph' and (
            (number and b.get('is_heading')) or normalized(text) in _FRONT)
        if is_element:
            element = dict(title=text, name=number.group(2) if number else text,
                number=number.group(1) if number else '', locator=b['locator'],
                locators=[b['locator']], conditional=conditional_changes and normalized(text)=='лист регистрации изменений')
            current['elements'].append(element)
        elif element is not None:
            element['locators'].append(b['locator'])
    return [t for t in templates if t['elements']]


def catalog(store, records):
    """No paths from the reviewed document; validate each normative original."""
    result = []; issues = []
    for ref, record in records.items():
        source = record['payload']
        if record['kind'] != 'source_revision' or source.get('classification', {}).get('type') != 'sto':
            continue
        key = source.get('original_key', '')
        root = store.directory.resolve(); path = (root / key).resolve()
        if not key or not path.is_relative_to(root):
            issues.append('Недоступен оригинал нормативного источника'); continue
        if not path.is_file():
            issues.append('Не загружен оригинал: '+source.get('filename', 'СТО')); continue
        if sha256(path) != source['sha256']:
            raise Conflict('Template original differs from pinned source')
        # Original parsing is cached by content and parser, never by filename.
        cache = root/'template-catalog'/ (checksum([VERSION,PARSER_VERSION,source['sha256']])+'.json')
        import json
        if cache.exists():
            value = json.loads(cache.read_text(encoding='utf-8'))
            if value.get('digest') != checksum(value.get('templates')):
                raise Conflict('Template cache checksum')
            templates = value['templates']
        else:
            templates = extract_templates(parse(path))
            from .structure import atomic_json
            atomic_json(cache, dict(templates=templates,digest=checksum(templates)))
        for template in templates:
            result.append(dict(template,source_ref=list(ref),source_sha256=source['sha256'],
                source_name=source.get('filename','СТО')))
    return result, issues


def document_outline(doc):
    outline = []
    for b in doc['blocks']:
        if b.get('table') or re.search(r'\t\s*\d+\s*$', b['text']):
            continue  # An old table of contents is not the document body.
        headings=b.get('headings',[]); text=b['text'].strip()
        if not (headings and normalized(headings[-1])==normalized(text)) and normalized(text) not in _FRONT:
            continue
        number=_NUMBER.match(text)
        outline.append(dict(title=text,name=number.group(2) if number else text,
            locator=b['locator'],location=b.get('location',b['locator']),
            parent=normalized(_NUMBER.sub(r'\2',headings[-2])) if len(headings)>1 else '',
            number=number.group(1) if number else ''))
    return outline


def compare(doc, templates, aliases, issues=()):
    names={normalized(s) for s in aliases if isinstance(s,str)}
    selected=[t for t in templates if normalized(t['name']) in names]
    # Same source can appear in several selected releases; don't duplicate it.
    selected=list({(t['source_sha256'],t['locator']):t for t in selected}.values())
    base=dict(document_id=doc['id'],document_name=doc['name'],title=TITLE,rows=[])
    if len(selected)!=1:
        reason='Шаблон для установленного вида документа не найден в выбранных СТО.' if not selected else 'Найдено несколько шаблонов. Требуется определить приоритет нормативных версий.'
        base['rows']=[dict(template_element='Применимый шаблон СТО',document_element=doc['name'],
            structure_state='question',result=reason+(' '+'; '.join(issues) if issues else ''),content_state='unknown')]
        return base
    template=selected[0]; base['source']=dict(name=template['source_name'],sha256=template['source_sha256'],
        ref=template['source_ref'],locator=template['locator'],template_name=template['name'])
    outline=document_outline(doc); used=set(); matched={}
    # Match all exact names before considering a possible renamed heading.
    for i,el in enumerate(template['elements']):
        hits=[j for j,h in enumerate(outline) if j not in used and normalized(h['name'])==normalized(el['name'])]
        if len(hits)>1 and el['number']:
            numbered=[j for j in hits if outline[j]['number']==el['number']]
            if len(numbered)==1:hits=numbered
        if len(hits)==1:matched[i]=hits[0];used.add(hits[0])
    last=-1
    for i,el in enumerate(template['elements']):
        j=matched.get(i); status='matched'; notes=[]
        if j is None:
            # A candidate, never a definitive rename inferred from position alone.
            candidates=[k for k,h in enumerate(outline) if k not in used and
                normalized(h['name']).split()[:1]==normalized(el['name']).split()[:1]]
            if len(candidates)==1:
                j=candidates[0];used.add(j);status='difference'
                notes.append('Название отличается от шаблона; проверьте переименование.')
            else:
                status='conditional' if el['conditional'] else 'question'
                notes.append('Не найден. Включается при выполнении условий СТО.' if el['conditional'] else
                    'Соответствующий заголовок не найден; требуется проверить отсутствие или распознавание структуры.')
        else:
            notes.append('Заголовок найден.')
        if j is not None:
            if j<last:status='difference';notes.append('Порядок отличается от шаблона.')
            last=max(last,j)
            if el['number'] and outline[j]['number'] and el['number']!=outline[j]['number']:
                status='difference';notes.append('Номер отличается от шаблона.')
            if '.' in el['number']:
                parent_number=el['number'].rsplit('.',1)[0]
                parent=next((x for x in template['elements'] if x['number']==parent_number),None)
                if parent and outline[j]['parent'] and outline[j]['parent']!=normalized(parent['name']):
                    status='difference';notes.append('Родительский раздел отличается от шаблона.')
        base['rows'].append(dict(template_element=el['title'],
            document_element=(outline[j]['title']+' · '+outline[j]['location'] if j is not None else 'Не найден'),
            document_locator=outline[j]['locator'] if j is not None else None,
            source_locator=el['locator'],source_locators=el['locators'],
            structure_state=status,result=' '.join(notes),content_state='pending',
            content_result='Содержание будет оценено по применимым нормативным требованиям.'))
    return base


def build(store, docs, selected_records, facts):
    templates=[];issues=[]
    for records in selected_records:
        values,errors=catalog(store,records);templates.extend(values);issues.extend(errors)
    return dict(version=VERSION,title=TITLE,columns=HEADERS,documents=[compare(doc,templates,
        (lambda v:v if isinstance(v,list) else [v])(facts.get(doc['id'],{}).get('document_type',{}).get('value',doc.get('classification',{}).get('types',doc.get('classification',{}).get('type')))),issues) for doc in docs])


def with_content(comparison, decisions):
    """Never turn presence of a heading into a positive content verdict."""
    import copy
    value=copy.deepcopy(comparison)
    for doc in value.get('documents',[]):
        for row in doc['rows']:
            locs=set(row.get('source_locators',[]));source=doc.get('source',{})
            relevant=[d for d in decisions if d['obligation'].get('document_id')==doc['document_id'] and
                d['obligation'].get('source',{}).get('sha256')==source.get('sha256') and
                any(c.get('locator') in locs for c in d['obligation'].get('atom',{}).get('citations',[])) and
                d['state']!='not_applicable']
            row['content_obligation_ids']=[d['obligation']['id'] for d in relevant]
            if not relevant:
                row.update(content_state='unknown',content_result='Отдельное решение по содержанию не получено. Наличие заголовка не подтверждает полноту.');continue
            states=[d['state'] for d in relevant]
            state='violated' if 'violated' in states else 'checked' if all(s=='checked' for s in states) else 'unknown'
            row.update(content_state=state,content_result={'violated':'Есть нормативное расхождение; см. реестр замечаний.',
                'checked':'Содержание подтверждено по связанным требованиям.',
                'unknown':'Проверка содержания не завершена определённым решением; см. вопросы и ограничения.'}[state])
    return value
