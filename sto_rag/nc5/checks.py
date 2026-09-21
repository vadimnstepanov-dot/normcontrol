import re
from decimal import Decimal,InvalidOperation
from collections import defaultdict
from .common import digest

ABSENCE=re.compile(r'отсутств|не указан|не определ[её]н|не привед[её]н|не описан|не представлен|не содержит|не раскрыт|не задан|нет (?:сведений|описания|раздела|данных)',re.I)
LOCAL_LANGUAGE_ABSENCE=re.compile(r'(?:отсутств|пропущ)[а-яё]*\s+(?:(?:один|одна|нужн[а-я]+)\s+)?(?:пробел|букв|запят|предлог|союз|скобк|точк|окончан|дефис)',re.I)

def validate_evidence(items,documents):
    by={(d['id'],b['locator']):b for d in documents for b in d['blocks']}
    out=[]
    for e in items:
        b=by.get((e.get('document'),e.get('locator')));quote=e.get('quote','')
        if b is None or not quote.strip():raise ValueError('Цитата отсутствует по указанному адресу')
        if quote not in b['text']:
            # Recover only whitespace representation, with an exact character range in the original.
            # No letter, number, punctuation, case, or semantic substitutions are allowed.
            pattern=r'\s+'.join(re.escape(x) for x in re.split(r'\s+',quote.strip()))
            matches=list(re.finditer(pattern,b['text']))
            if len(matches)!=1:raise ValueError('Цитата отсутствует по указанному адресу')
            quote=matches[0][0];e={**e,'quote':quote,'whitespace_restored':True}
        start=b['text'].index(quote)
        out.append({**e,'address':b['address'],'start':start,'end':start+len(quote)})
    if not out:raise ValueError('Нет доказательств')
    return out

def validate_finding(item,documents,cards):
    item=dict(item);item['evidence']=validate_evidence(item.get('evidence',[]),documents)
    rid=item.get('requirement_id','')
    if rid:
        if rid not in cards:raise ValueError('Неизвестная нормативная ссылка')
        c=cards[rid];item['source']={k:c[k] for k in ('requirement_id','document_name','clause','appendix','source_locator','source_quote','source_sha256','validation_status')}
        if item.get('reference_defect') and not re.search(r'ссыл|нумерац|номер|таблиц|рисунк',c['source_quote'],re.I):
            item['unverified_normative_source']=item.pop('source');item['requirement_id']='';item['category']='межраздельная логика'
            item['source_routing_note']='Приведённая норма не устанавливает правило нумерации или ссылок. Локальная ошибка проверяется самостоятельно, без приписывания ей нарушения этого пункта СТО.'
    elif item.get('category') in ('СТО','sto','нормативное нарушение','соответствие СТО','структура СТО') and item.get('kind')=='violation':raise ValueError('У нормативного нарушения нет источника')
    claim=item.get('issue','')+' '+item.get('explanation','')
    # A local spelling/syntax defect is not a claim about a missing document section.
    local_grammar=item.get('category')=='грамотность' and not re.search(r'во вс[её]м документе|раздел|перечень сокращений|определение термин',claim,re.I)
    if ABSENCE.search(claim) and not LOCAL_LANGUAGE_ABSENCE.search(claim) and not local_grammar and item.get('evidence_scope')!='quoted_cell':item['needs_full_scope']=True
    return item

def restore_non_normative_id(item,stage,requirements):
    if stage=='language' and not requirements and item.get('requirement_id')=='language':
        return {**item,'requirement_id':'','schema_repair':'Removed language stage label from normative ID; no normative claim is added'}
    return item

def route_finding(item,stage):
    item=dict(item)
    claim=item.get('issue','')+' '+item.get('explanation','')
    reference=re.search(r'ссылк|номер[а-я]*\s+(?:таблиц|рисунк|приложени)|(?:таблиц|рисунк|приложени)[а-я]*\s+\d',claim,re.I) and re.search(r'неверн|ошибочн|несовпад|несогласован|не соответствует|несоответств',claim,re.I)
    if reference:item['reference_defect']=True
    objective=re.search(r'(?:грамматическ|орфографическ|пунктуационн).*ошибк|опечатк|нарушен[аоыие]*\s+согласовани[ея]|согласовани[ея]\s+(?:числа|подлежащ)|лишняя запятая|пропущен[ао]?\s+(?:буква|предлог|скобка|запятая)|незаверш[её]нн[а-я]+\s+(?:фраз|предложени)|оборванн[а-я]+\s+(?:фраз|предложени)',item.get('issue','')+' '+item.get('explanation',''),re.I)
    objective=objective or re.search(r'обрыв\s+фразы|(?:неверн|нарушен)[а-яё]*\s+управлени|(?:отсутствует|пропущена)\s+(?:закрывающая\s+)?(?:кавычка|скобка|точка)|неверное написание',claim,re.I)
    if reference and not item.get('requirement_id'):item['category']='межраздельная логика'
    elif stage=='language' or (objective and not item.get('requirement_id')):item['category']='грамотность'
    if (objective or reference) and item.get('kind')=='style':
        item['original_kind']='style';item['kind']='violation'
        item['routing_note']='Заявление о конкретной ошибке направлено на независимую перепроверку; это ещё не подтверждение.'
    return item

def compatible(a,b):
    keys=('entity','parameter','unit','scope','environment','conditions','time_basis')
    if any(a.get(k)!=b.get(k) for k in keys):return None
    try:x,y=Decimal(str(a['value'])),Decimal(str(b['value']))
    except (InvalidOperation,KeyError):return None
    opx,opy=a['operator'],b['operator']
    if opx==opy and opx in ('<=','>='):return True
    if opx==opy=='=':return x==y
    if opx=='<=' and opy=='>=':return y<=x
    if opx=='>=' and opy=='<=':return x<=y
    if opx=='=' and opy=='<=':return x<=y
    if opx=='=' and opy=='>=':return x>=y
    if opy=='=':return compatible(b,a)
    return None

def decimal(text):
    text=text.replace('\xa0','').replace(' ','').replace(',','.')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?',text):raise InvalidOperation
    return Decimal(text)

def deterministic(doc):
    findings=[];bytable=defaultdict(list);latin={b['text'].strip() for b in doc['blocks'] if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',b['text'].strip())}
    for b in doc['blocks']:
        if b.get('table_context'):bytable[b['table_context']['table']].append(b)
        s=b['text'].strip();tr=str.maketrans('асеорхуАВСЕНКМОРТХ','aceopxyABCEHKMOPTX')
        normalized=s.translate(tr)
        # Exact field token + matching Latin sibling/explicit field heading, never an arbitrary mixed-language name.
        if re.fullmatch(r'[A-Za-zА-Яа-я_][A-Za-zА-Яа-я_0-9]*',s) and re.search('[a-zA-Z]',s) and re.search('[а-яА-Я]',s) and re.fullmatch('[A-Za-z_0-9]+',normalized) and (normalized in latin or re.search('(?:имя|название|наименование).*(?:поля|атрибут|параметр|ключ)|(?:поле|атрибут).*имя',b.get('table_context',{}).get('column_name',''),re.I)):
            chars=', '.join(f'{c}: U+{ord(c):04X}' for c in s if re.match('[а-яА-Я]',c))
            findings.append({'category':'техническая логика','severity':'major','kind':'violation','issue':'Кириллица в идентификаторе поля','explanation':f'В имени поля смешаны алфавиты: {chars}.','suggestion':f'Сверить с контрактом API; латинский вариант: {normalized}.','evidence':[{'document':doc['id'],'locator':b['locator'],'quote':s}],'requirement_id':'','search_query':''})
    for ti,bs in bytable.items():
        rows=defaultdict(dict)
        for b in bs:
            tc=b['table_context'];rows[tc['row']].setdefault(tc['column'],[]).append(b)
        for ri,cells in rows.items():
            groups=defaultdict(lambda:defaultdict(list))
            for col,parts in cells.items():
                label=parts[0]['table_context'].get('column_name','').lower();text=' '.join(x['text'] for x in parts).strip();labels=[x.strip() for x in label.split(' / ')];leaf=labels[-1];group=' / '.join(labels[:-1])
                if parts[0]['table_context'].get('vertical_merge')=='continue':continue
                role='quantity' if re.search('количеств|кол-во',leaf) else 'price' if re.search('цен[аы]',leaf) and not re.search('сумм|стоимост',leaf) else 'amount' if re.search('сумма|стоимость',leaf) and not re.search(r'(?:сумма|величина)\s+(?:ндс|налога)|итог',leaf) else None
                if role:
                    basis='net' if re.search(r'без\s*ндс',leaf) else 'gross' if re.search(r'(?:с|включая)\s*ндс',leaf) else 'unspecified'
                    currency='rub' if re.search(r'руб|₽',leaf) else 'eur' if re.search(r'евро|eur|€',leaf) else 'usd' if re.search(r'долл|usd|\$',leaf) else 'unspecified'
                    factor=Decimal(1000000) if re.search(r'\bмлн',leaf) else Decimal(1000) if re.search(r'\bтыс',leaf) else Decimal(1)
                    if role=='quantity' and factor!=1:continue
                    try:groups[group][role].append({'value':decimal(text)*factor,'parts':parts,'basis':basis,'currency':currency})
                    except InvalidOperation:pass
            for group,roles in groups.items():
                if len(roles['quantity'])!=1:continue
                quantity=roles['quantity'][0]
                for price in roles['price']:
                    amounts=[a for a in roles['amount'] if (a['basis'],a['currency'])==(price['basis'],price['currency'])]
                    peers=[p for p in roles['price'] if (p['basis'],p['currency'])==(price['basis'],price['currency'])]
                    if len(amounts)!=1 or len(peers)!=1:continue
                    amount=amounts[0];expected=(quantity['value']*price['value']).quantize(Decimal('.01'));actual=amount['value']
                    if abs(expected-actual)<=Decimal('.01'):continue
                    evidence=[{'document':doc['id'],'locator':b['locator'],'quote':b['text']} for value in (quantity,price,amount) for b in value['parts'] if b['text'].strip()]
                    basis={'gross':'с НДС','net':'без НДС','unspecified':'без явно обозначенного НДС'}[price['basis']]
                    findings.append({'category':'арифметика','severity':'major','kind':'violation','issue':'Сумма строки не равна количеству × цене','explanation':f'{quantity["value"]} × {price["value"]} = {expected}; указано {actual}. Колонки установлены по заголовкам одной группы: {group or "таблица"}; цена и сумма {basis}, единицы приведены к одному масштабу.','suggestion':f'Проверить исходные величины; при неизменных количестве и цене сумма в базовых денежных единицах {expected}.','evidence':evidence,'requirement_id':'','search_query':''})
    return findings
