"""Typed numeric constraints: compare bounds only within a proven common scope."""
from decimal import Decimal,InvalidOperation
from collections import defaultdict
import re
from .common import digest

UNITS={'сек':('s',Decimal(1)),'секунд':('s',Decimal(1)),'секунды':('s',Decimal(1)),'с':('s',Decimal(1)),'s':('s',Decimal(1)),
 'мс':('s',Decimal('.001')),'мин':('s',Decimal(60)),'минут':('s',Decimal(60)),'ч':('h',Decimal(1)),'часов':('h',Decimal(1)),
 'дней':('h',Decimal(24)),'суток':('h',Decimal(24)),'гб':('GB',Decimal(1)),'мб':('GB',Decimal('.001'))}

def validate_value(f):
    try:value=Decimal(f['value'].replace(',','.'))
    except (InvalidOperation,AttributeError,KeyError):return
    quotes=' '.join(e['quote'] for e in f['evidence']);matches=[]
    for m in re.finditer(r'-?\d+(?:[ \u00a0]\d{3})*(?:[,.]\d+)?',quotes):
        try:
            if Decimal(m[0].replace(' ','').replace('\u00a0','').replace(',','.'))==value:matches.append(m)
        except InvalidOperation:pass
    if not matches:raise ValueError('Числовое значение факта не подтверждается цитатой')
    if len(matches)==1:
        prefix=quotes[max(0,matches[0].start()-24):matches[0].start()].casefold()
        expected='<=' if re.search(r'не\s+(?:более|больше|выше|позднее)\s*$',prefix) else '>=' if re.search(r'не\s+(?:менее|меньше|ниже)\s*$',prefix) else None
        if expected and f.get('operator')!=expected:raise ValueError('Направление числовой границы не соответствует цитате')

def normalize(f):
    out=dict(f);out['entity_id']=digest([str(f.get(k,'')).casefold().strip() for k in ('entity','scope','environment')])[:16]
    out['fact_id']=digest(f)[:20];out['extraction_method']='bounded_llm';out['confidence']='source_quote_validated';out['related_fact_ids']=[]
    u=UNITS.get(f.get('unit','').casefold().strip('. '))
    try:
        v=Decimal(f['value'].replace(',','.'))
        if u:out['normalized_unit']=u[0];out['normalized_value']=str(v*u[1])
        else:out['normalized_unit']=f.get('unit','');out['normalized_value']=str(v)
    except (InvalidOperation,AttributeError,KeyError):out['normalized_value']=None
    out['source_document_ids']=sorted({e['document'] for e in f.get('evidence',[])})
    return out

def contradictions(facts):
    groups=defaultdict(list);findings=[]
    for f in facts:
        if not f.get('normalized_value') or f.get('operator') not in ('=','<=','>='):continue
        if any(not f.get(k) or f[k].casefold() in ('unknown','не указан','неизвестно') for k in ('entity','parameter','scope','environment')):continue
        if f.get('normalized_unit')=='h' and not f.get('time_basis'):continue
        key=tuple(f.get(k,'').casefold() for k in ('entity','parameter','normalized_unit','scope','environment','conditions','time_basis'))
        groups[key].append(f)
    for key,fs in groups.items():
        lower=None;upper=None
        for f in fs:
            v=Decimal(f['normalized_value'])
            if f['operator'] in ('=','>=') and (lower is None or v>lower[0]):lower=(v,f)
            if f['operator'] in ('=','<=') and (upper is None or v<upper[0]):upper=(v,f)
        if lower and upper and lower[0]>upper[0]:
            a,b=lower[1],upper[1];findings.append({'category':'числовые ограничения','severity':'major','kind':'violation','issue':'Несовместимые числовые ограничения одного параметра','explanation':f'При одинаковых объекте, контуре, условиях и единицах нижняя граница {lower[0]} превышает верхнюю {upper[0]} {key[2]}. Общий допустимый интервал пуст.','suggestion':'Согласовать границы либо явно разделить условия применимости.','evidence':a['evidence']+b['evidence'],'requirement_id':'','search_query':'','fact_ids':[a['fact_id'],b['fact_id']]})
    return findings
