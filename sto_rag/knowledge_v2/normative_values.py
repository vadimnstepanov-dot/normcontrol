"""Exact references for repeated normative values; no summarization."""
import copy
from collections import Counter
from .store import encode,Conflict

REF='normative_value_ref'
FIELDS=('obligations','normative_contexts','normative_structures','applicability_contexts','sources')
POLICY=('normative_values — справочник дословных повторяющихся нормативных значений. '
        'Объект с единственным полем normative_value_ref раскрывается по этому справочнику; '
        'значение может быть строкой, списком или объектом и содержать вложенные ссылки. '
        'Раскрой все ссылки перед оценкой условия, исключения, зависимости и происхождения. '
        'Это точное представление исходных значений, не сокращение требований.')

def pack(value):
    if 'normative_values' in value:raise Conflict('Reserved normative values dictionary')
    counts=Counter()
    def key(v):
        if isinstance(v,(str,list,dict)):
            text=encode(v)
            if len(text)>=160:return text
    def collect(v):
        if isinstance(v,dict) and REF in v:raise Conflict('Reserved normative value reference')
        k=key(v)
        if k:counts[k]+=1
        if isinstance(v,dict):
            for x in v.values():collect(x)
        elif isinstance(v,list):
            for x in v:collect(x)
    for field in FIELDS:
        if field in value:collect(value[field])
    refs={};pool={}
    def replace(v,skip=False):
        k=key(v)
        if not skip and k and counts[k]>1:
            if k not in refs:
                ref='V'+str(len(refs)+1);refs[k]=ref
                pool[ref]=replace(v,True)
            return {REF:refs[k]}
        if isinstance(v,dict):return {n:replace(x) for n,x in v.items()}
        if isinstance(v,list):return [replace(x) for x in v]
        return v
    out=dict(value)
    for field in FIELDS:
        if field not in value:continue
        root=value[field]
        if field=='obligations':out[field]=[{k:replace(v) for k,v in row.items()} for row in root]
        elif isinstance(root,dict):out[field]={k:replace(v) for k,v in root.items()}
        else:out[field]=replace(root,True)
    if pool:out['normative_values']=pool
    return out

def unpack(value):
    out=copy.deepcopy(value);pool=out.pop('normative_values',{})
    def expand(v,active=frozenset()):
        if isinstance(v,dict) and set(v)=={REF}:
            ref=v[REF]
            if ref not in pool or ref in active:raise Conflict('Missing or cyclic normative value')
            return expand(pool[ref],active|{ref})
        if isinstance(v,dict):return {k:expand(x,active) for k,x in v.items()}
        if isinstance(v,list):return [expand(x,active) for x in v]
        return v
    for field in FIELDS:
        if field in out:out[field]=expand(out[field])
    return out
