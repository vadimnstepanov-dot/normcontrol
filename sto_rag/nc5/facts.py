"""Typed numeric constraints: compare bounds only within a proven common scope."""
from decimal import Decimal,InvalidOperation
from collections import defaultdict
import re
from .common import digest

UNITS={'сек':('s',Decimal(1)),'секунд':('s',Decimal(1)),'секунды':('s',Decimal(1)),'с':('s',Decimal(1)),'s':('s',Decimal(1)),
 'мс':('s',Decimal('.001')),'мин':('s',Decimal(60)),'минут':('s',Decimal(60)),'ч':('h',Decimal(1)),'часов':('h',Decimal(1)),
 'дней':('h',Decimal(24)),'суток':('h',Decimal(24)),'гб':('GB',Decimal(1)),'мб':('GB',Decimal('.001'))}

CARDINALS={word:Decimal(n) for n,forms in enumerate((
    'ноль нуля нулю нулем нуле',
    'один одна одно одного одной одному одним одну',
    'два две двух двум двумя', 'три трех трем тремя',
    'четыре четырех четырем четырьмя', 'пять пяти пятью',
    'шесть шести шестью', 'семь семи семью', 'восемь восьми восемью',
    'девять девяти девятью', 'десять десяти десятью')) for word in forms.split()}
COMPOUND_NUMBER=re.compile(r'^(?:одиннадц|двенадц|тринадц|четырнадц|пятнадц|шестнадц|семнадц|восемнадц|девятнадц|двадц|тридц|сорок|пятьдесят|пятидесят|шестьдесят|шестидесят|семьдесят|семидесят|восемьдесят|восьмидесят|девяност|сто$|ста$|сот|двест|двухсот|трехсот|четырехсот|пятисот|шестисот|семисот|восьмисот|девятисот|тысяч|миллион|миллиард|триллион|цел|десят|половин)')

def canonical_operator(operator):
    value=str(operator).casefold().strip()
    return {'≤':'<=','≥':'>=','не более':'<=','не больше':'<=','не выше':'<=',
            'не менее':'>=','не меньше':'>=','не ниже':'>=','равно':'='}.get(value,value)

def word_numbers(text):
    # Only standalone small cardinals. Never take the "two" out of twenty-two,
    # two hundred, or two-and-a-half; unsupported compositions remain unproven.
    tokens=list(re.finditer(r'[а-яё]+|\d+(?:[,.]\d+)?',text.casefold()))
    for i,token in enumerate(tokens):
        word=token[0].replace('ё','е')
        if word not in CARDINALS:continue
        neighbors=[tokens[k][0].replace('ё','е') for k in (i-1,i+1) if 0<=k<len(tokens)]
        if any(w in CARDINALS or w[0].isdigit() or COMPOUND_NUMBER.match(w) for w in neighbors):continue
        suffix=text[token.end():token.end()+30].casefold()
        if re.match(r'\s+с\s+половин',suffix):continue
        if re.match(r'\s*(?:тыс|млн|млрд)\b',suffix):continue
        yield token,CARDINALS[word]

def validate_value(f):
    try:value=Decimal(f['value'].replace(',','.'))
    except (InvalidOperation,AttributeError,KeyError):return
    quotes=' '.join(e['quote'] for e in f['evidence']);matches=[]
    for m in re.finditer(r'-?\d+(?:[ \u00a0]\d{3})*(?:[,.]\d+)?',quotes):
        try:
            if Decimal(m[0].replace(' ','').replace('\u00a0','').replace(',','.'))==value:matches.append(m)
        except InvalidOperation:pass
    matches.extend(m for m,n in word_numbers(quotes) if n==value)
    if not matches:raise ValueError('Числовое значение факта не подтверждается цитатой')
    if len(matches)==1:
        prefix=quotes[max(0,matches[0].start()-48):matches[0].start()].casefold()
        tail=r'\s*(?:чем\s*)?(?:на\s*)?$'
        expected='<=' if re.search(r'не\s+(?:более|больше|выше|позднее)'+tail,prefix) else '>=' if re.search(r'не\s+(?:менее|меньше|ниже)'+tail,prefix) else None
        if expected and canonical_operator(f.get('operator'))!=expected:raise ValueError('Направление числовой границы не соответствует цитате')

def normalize(f):
    out=dict(f);out['entity_id']=digest([str(f.get(k,'')).casefold().strip() for k in ('entity','scope','environment')])[:16]
    out['operator']=canonical_operator(f.get('operator',''))
    if any(re.search(r'\bрекоменд\w*',e.get('quote',''),re.I) for e in f.get('evidence',[])):
        out['constraint_strength']='advisory'
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
        if f.get('constraint_strength')=='advisory':continue
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
