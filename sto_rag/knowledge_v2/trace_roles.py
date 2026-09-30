"""Opt-in source-grounded roles for trace routing; original facts stay immutable."""
import copy,re

VERSION='trace-title-roles-scope-v1'
WORKS='ТЗ на выполнение работ'

def enrich(docs,facts,verify):
    prepared=copy.deepcopy(docs);effective=copy.deepcopy(facts);derived={}
    for doc in prepared:
        original=facts.get(doc['id'],{}).get('document_type',{})
        if not original.get('evidence') or not all(verify('document_type',original.get('value'),e) for e in original['evidence']):continue
        classification=doc.get('classification',{})
        types=classification.get('types',[classification.get('type','')])
        if not set(types).intersection({'ТЗ','Техническое задание'}):continue
        title=doc['blocks'][:35]
        proof=None
        for i,block in enumerate(title):
            if block.get('table') or re.sub(r'\s+','',block.get('text','')).casefold()!='техническоезадание':continue
            for sub in title[i+1:i+4]:
                if sub.get('table') or not re.match(r'^на\s+выполнение\s+работ\b',sub.get('text','').strip(),re.I):continue
                proof=[dict(block_id=b['id'],locator=b['locator'],quote=b['text']) for b in (block,sub)]
                break
            if proof:break
        if not proof or WORKS in types:continue
        values=original['value'] if isinstance(original['value'],list) else [original['value']]
        values=list(dict.fromkeys(values+[WORKS]))
        evidence=dict(source=doc['id'],locator=proof[0]['locator'],quote=proof[0]['quote'],
            policy=VERSION,role=WORKS,role_evidence=proof)
        fact=dict(value=values,evidence=[evidence],complete=False)
        effective.setdefault(doc['id'],{})['document_type']=fact
        classification['types']=list(dict.fromkeys(types+[WORKS]))
        derived[doc['id']]=dict(role=WORKS,evidence=proof,fact=fact)
    def verified(name,value,evidence):
        item=derived.get(evidence.get('source'))
        if item and name=='document_type' and value==item['fact']['value'] and evidence in item['fact']['evidence']:return True
        return verify(name,value,evidence)
    return prepared,effective,verified,derived
