"""Source-derived navigation and context. No evaluation/reference corpus is used here."""
import re
from .documents import compact_block
from .search import Index, terms

REFERENCE = re.compile(r'(?P<kind>приложени[еяию]|рисунк[аеуы]?|таблиц[аеуы]?|пункт[аеуы]?)\s*(?:№\s*)?(?P<number>(?:\d+|[А-ЯA-Z]\d*)(?:\.\d+)*)(?!\w|\.\d)', re.I)

def outline(doc):
    return [{k:h[k] for k in ('locator','title','address') if k in h} for h in doc['headings']]

def reference_inventory(doc):
    result=[]
    for b in doc['blocks']:
        if b.get('toc'):continue
        text=b['text'].strip()
        appendix=re.search(r'пункт\s+ПРИЛОЖЕНИЕ\s+(\d+|[А-ЯA-Z])\b',b.get('address',''),re.I) if b.get('is_heading') else None
        caption=re.match(r'^(Таблица|Рисунок)\s+((?:\d+|[А-ЯA-Z]\d*)(?:\.\d+)*)\s*[-–—.:]',text,re.I)
        if appendix or caption:
            result.append({'document':doc['id'],'locator':b['locator'],'kind':'приложение' if appendix else caption[1].lower(),
                           'number':appendix[1] if appendix else caption[2],'title':text,'address':b.get('address','')})
    return result

def reference_payloads(doc,group_size=3):
    inventory=reference_inventory(doc);by={b['locator']:b for b in doc['blocks']}
    refs=[b for b in doc['blocks'] if not b.get('toc') and not b.get('is_heading') and REFERENCE.search(b['text'])
          and not re.match(r'^\s*(?:Рисунок|Таблица)\s+[\dА-ЯA-Z.]+\s*[-–—]',b['text'],re.I)]
    for start in range(0,len(refs),group_size):
        group=refs[start:start+group_size];chosen={b['locator']:b for b in group};links=[]
        for b in group:
            mentions=list(REFERENCE.finditer(b['text']));numbers={m['number'].casefold() for m in mentions}
            kinds={'приложение' if m['kind'].lower().startswith('прилож') else 'таблица' if m['kind'].lower().startswith('таблиц') else 'рисунок' if m['kind'].lower().startswith('рисун') else 'пункт' for m in mentions}
            candidates=[x for x in inventory if x['kind'] in kinds]
            relevant=[x for x in candidates if x['number'].casefold() in numbers]
            wanted=set(terms(b['text']))
            relevant+=sorted(candidates,key=lambda x:len(wanted.intersection(terms(x['title']))),reverse=True)[:3]
            for target in relevant:chosen[target['locator']]=by[target['locator']]
            links.append({'source_locator':b['locator'],'mentions':[{'kind':m['kind'],'number':m['number']} for m in mentions],
                          'targets_with_same_number':[x for x in candidates if x['number'].casefold() in numbers]})
        included=inventory if len(inventory)<=48 else [x for x in inventory if x['locator'] in chosen]
        yield {'stage':'cross','directions':'Проверь КАЖДУЮ ссылку из links, включая №. Сверь номер И смысл целевого приложения/таблицы/рисунка. Разные формулировки одного смысла не являются ошибкой. Не требуй буквального равенства названий. inventory содержит подписи из Word, но не содержание изображений. Различай внешние ссылки на СТО/договор и ссылки внутри этого документа. Несовпадение номера доказывай цитатой ссылки и подписью правильной цели. Не считай наличие номера доказательством правильности смысла ссылки. При inventory_complete=false нельзя доказывать отсутствие подписи по выборке.',
               'reference_inventory':included,'inventory_complete':len(included)==len(inventory),'inventory_total':len(inventory),'links':links,'blocks':[compact_block(b) for b in chosen.values()],
               'requirements':[],'scope':'source_links_with_target_captions'}

def rule_evidence(doc,rules,index=None):
    """Prefer the actual template section; retain a full-index search for counterevidence."""
    index=index or Index([doc]);anchors=[];selected=[]
    if any(r.get('applicability_contract',{}).get('requires_project_facts') for r in rules):
        from .normative_contract import applicability_facts
        anchors.extend({'document':doc['id'],'locator':x['locator']} for x in applicability_facts(doc))
    for rule in rules:
        expected=rule.get('expected_evidence','').strip()
        number=re.search(r',\s*(\d+(?:\.\d+)*)\s*$',rule.get('clause','')) if rule.get('document_scope')=='template' else None
        target_number=number[1] if number else None
        expected_words=set(terms(re.sub(r'^\d+(?:\.\d+)*\s*','',expected)))
        for h in doc['headings']:
            label=re.search(r'пункт\s+(\d+(?:\.\d+)*)\s',h.get('address',''))
            same_number=target_number and label and label[1]==target_number
            heading_words=set(terms(h['title']))
            same_title=expected_words and len(expected_words & heading_words)/max(1,len(expected_words))>=.7
            if same_number or same_title:
                selected.append(h['locator'])
                heading=next((b for b in doc['blocks'] if b['locator']==h['locator']),{})
                path=heading.get('heading_path',[])
                anchors.extend({'document':doc['id'],'locator':b['locator']} for b in doc['blocks'] if not b.get('toc') and (b.get('section')==h['locator'] or (path and b.get('heading_path',[])[:len(path)]==path)))
        # Front matter is not represented by a numbered heading in many Word files.
        if not target_number and re.search(r'аннотаци|титульн|обозначени|децимальн',expected+' '+rule.get('compact_text',''),re.I):
            for b in doc['blocks'][:120]:
                if b.get('toc') or re.fullmatch(r'\s*СОДЕРЖАНИЕ\s*',b['text'],re.I):break
                anchors.append({'document':doc['id'],'locator':b['locator']})
    query=' '.join(r.get('expected_evidence','')+' '+r.get('compact_text',r['source_quote']) for r in rules)
    return index.retrieve(query,anchors,limit=8),{'method':'template_section_plus_full_index','sections':list(dict.fromkeys(selected)),
        'complete_text_index':True,'does_not_prove_semantic_absence':True,'query':query}

def verification_context(docs,findings,limit=8):
    anchors=[e for f in findings for e in f['evidence']]
    query=' '.join(f.get('issue','')+' '+f.get('search_query','')+' '+f.get('explanation','') for f in findings)
    # Include the definition of an acronym or a term if the source actually contains it.
    quoted=' '.join(e['quote'] for e in anchors)
    tokens=set(re.findall(r'\b[А-ЯЁA-Z][А-ЯЁA-Z0-9_-]{1,15}\b',quoted))
    for doc in docs:
        for b in doc['blocks']:
            if b.get('toc'):continue
            if re.search(r'сокращени|термин|определени', ' '.join(b.get('heading_path',[])),re.I) and any(re.search(r'\b'+re.escape(t)+r'\b',b['text']) for t in tokens):
                anchors.append({'document':doc['id'],'locator':b['locator']})
    return Index(docs).retrieve(query,anchors,limit=limit,neighbors=2)

def restore_evidence_addresses(item,blocks):
    """Only a unique verbatim quote in supplied blocks may repair an incorrect locator."""
    repaired=[]
    for evidence in item.get('evidence',[]):
        e=dict(evidence);quote=e.get('quote','')
        valid=any(b['document']==e.get('document') and b['locator']==e.get('locator') and quote and quote in b['text'] for b in blocks)
        if not valid and quote.strip() and quote!=quote.strip():
            exact=any(b['document']==e.get('document') and b['locator']==e.get('locator') and quote.strip() in b['text'] for b in blocks)
            if exact:
                e['original_quote']=quote;quote=quote.strip();e['quote']=quote;e['quote_boundary_trimmed']=True;valid=True
        if not valid and len(quote.strip())>=12:
            hits={(b['document'],b['locator']) for b in blocks if b['document']==e.get('document') and quote in b['text']}
            if len(hits)==1:
                e['original_locator']=e.get('locator');e['locator']=next(iter(hits))[1];e['locator_restored']=True
        repaired.append(e)
    return {**item,'evidence':repaired}

def symbol_context(doc,blocks):
    """Include definitions referenced by conversion expressions in mapping tables."""
    symbols=set()
    for b in blocks:
        if re.search(r'комментар|преобразован|алгоритм|правило',b.get('table',{}).get('column_name',''),re.I):
            symbols.update(re.findall(r'\b[A-Za-z_][A-Za-z_0-9]{3,}\b',b['text']))
    present={(b['document'],b['locator']) for b in blocks};anchors=[]
    for b in doc['blocks']:
        if b['text'].strip() in symbols and re.search(r'атрибут.*(?:АПИ|API)|имя поля',b.get('table_context',{}).get('column_name',''),re.I):
            if (b['document'],b['locator']) not in present:anchors.append({'document':doc['id'],'locator':b['locator']})
    # Four extra rows at most; unselected definitions are not treated as absent.
    extra=Index([doc]).retrieve('',anchors[:4],limit=0,neighbors=0) if anchors else []
    return blocks+[{**b,'role':'referenced_definition'} for b in extra if (b['document'],b['locator']) not in present]

def link_candidates(doc):
    """Lexical mismatch is a hypothesis for independent review, never a finding by itself."""
    inventory=[x for x in reference_inventory(doc) if x['kind']=='приложение'];by={b['locator']:b for b in doc['blocks']}
    stop={'приложени','документ','настоящ','системы','система','рисунок','таблица','приведе','данных','должен','частном','техниче'}
    def meaningful(text):
        result={t for t in terms(text) if t not in stop and not t.isdigit()}
        if re.search(r'маппинг|сопоставлен',text,re.I):result.add('mapping')
        return result
    for b in doc['blocks']:
        if b.get('toc') or b.get('is_heading'):continue
        for m in REFERENCE.finditer(b['text']):
            if not m['kind'].lower().startswith('прилож') or not m['number'].isdigit():continue
            if re.search(r'к\s+(?:договор|СТО|ГОСТ)',b['text'],re.I):continue
            current=[x for x in inventory if x['number']==m['number']]
            if len(current)!=1:continue  # Missing targets require separate completeness evidence.
            boundaries=list(re.finditer(r'\w{4,}[.!?]\s+(?=[А-Я])',b['text'][:m.start()]))
            fragment=b['text'][boundaries[-1].end() if boundaries else 0:m.end()]
            words=meaningful(fragment);overlap=lambda x:len(words & meaningful(x['title']))
            score=lambda x:overlap(x)/(1+len(meaningful(x['title'])))
            others=sorted([x for x in inventory if x['number']!=m['number']],key=score,reverse=True)
            if not others or overlap(others[0])<2 or score(others[0])<=score(current[0]):continue
            alternatives=[x for x in others[:3] if overlap(x)>=2]
            relevant=[b,by[current[0]['locator']]]+[by[x['locator']] for x in alternatives]
            yield {'category':'межраздельная логика','kind':'question','severity':'major','issue':'Возможное несоответствие назначения ссылки и приложения '+m['number'],
                   'explanation':'Текст ссылки и название указанного приложения описывают потенциально разные объекты; в документе есть другое приложение с более близким названием. Лексическое сходство само по себе не доказывает ошибку, необходима смысловая перепроверка.',
                   'suggestion':'Сопоставить назначение ссылки с обеими приведёнными подписями и исправить номер только при подтверждении несовпадения.',
                   'evidence':[{'document':doc['id'],'locator':x['locator'],'quote':x['text']} for x in relevant],
                   'requirement_id':'','search_query':b['text'][:300],
                   'target_numbers':{'referenced':m['number'],'actual_caption':current[0]['title'],'alternatives':[{'number':x['number'],'caption':x['title']} for x in alternatives]}}

def local_candidates(doc):
    rows={};definitions={}
    for b in doc['blocks']:
        tc=b.get('table_context',{})
        if tc:rows.setdefault((tc['table'],tc['row']),[]).append(b)
        if re.search(r'атрибут.*(?:АПИ|API)|имя поля',tc.get('column_name',''),re.I) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',b['text'].strip()):
            definitions.setdefault(b['text'].strip(),[]).append(b)
    for b in doc['blocks']:
        if b.get('toc'):continue
        if b.get('table_context') and re.search(r'уточнить\s+у\b',b['text'],re.I) and re.search(r'неправильн[а-я]*\s+описани',b['text'],re.I):
            yield {'category':'техническая логика','kind':'violation','severity':'major','issue':'В таблице оставлено неразрешённое указание об ошибочном описании',
                   'explanation':'Сам текст отмечает неправильное описание и необходимость уточнения; окончательное правило в этой ячейке не задано.',
                   'suggestion':'Заменить рабочую пометку согласованным описанием. Значение должен подтвердить владелец интерфейса.',
                   'evidence':[{'document':doc['id'],'locator':b['locator'],'quote':b['text']}],'requirement_id':'','search_query':b['text'],'evidence_scope':'quoted_cell'}
        tc=b.get('table_context',{})
        lookup=re.search(r'Поиск по\s+([A-Za-z_][A-Za-z_0-9]*)\s*=',b['text']) if re.search('комментар|алгоритм|преобразован',tc.get('column_name',''),re.I) else None
        if lookup:
            own=rows.get((tc['table'],tc['row']),[])
            fields=[x for x in own if re.search(r'атрибут.*(?:АПИ|API)|имя поля',x.get('table_context',{}).get('column_name',''),re.I)]
            others=[x for x in definitions.get(lookup[1],[]) if x.get('table_context',{}).get('row')!=tc['row'] and x.get('table_context',{}).get('table')==tc['table']]
            if len(fields)==1 and len(others)==1 and fields[0]['text'].strip()!=lookup[1]:
                selected=[fields[0],b,others[0]]
                selected += [x for x in own+rows[(tc['table'],others[0]['table_context']['row'])] if x.get('table_context',{}).get('column_name','').strip().lower()=='наименование']
                yield {'category':'техническая логика','kind':'question','severity':'major','issue':'Правило поиска использует поле из другой строки маппинга',
                       'explanation':f'Строка описывает поле {fields[0]["text"].strip()}, а выражение поиска использует {lookup[1]}, определённое в другой строке. Следует проверить совместимость смыслов и правильность правила поиска.',
                       'suggestion':'Сверить определение входного поля, целевой справочник и контракт API. Не переименовывать поля автоматически.',
                       'evidence':[{'document':doc['id'],'locator':x['locator'],'quote':x['text']} for x in selected],'requirement_id':'','search_query':lookup[1]}
