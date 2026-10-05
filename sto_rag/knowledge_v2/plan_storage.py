"""Lossless references for repeated evidence in durable review plans."""
from .store import Conflict, checksum

VERSION = 'review-plan-refs-v2'

def pack(payload):
    if payload.get('plan_storage'):return payload
    blocks={}; obligations={}; scope_lists={}
    def intern(pool,value):
        key=value['id']
        if key in pool and pool[key]!=value:raise Conflict('Plan reference identity differs')
        pool[key]=value
        return key
    batches=[]
    for batch in payload['batches']:
        packet=batch['payload']
        compact=dict(packet,documents=[intern(blocks,b) for b in packet['documents']],
                     obligations=[intern(obligations,r) for r in packet['obligations']])
        completeness=packet.get('completeness')
        if completeness is not None:
            compact['completeness']=dict(completeness)
            for key,value in completeness.items():
                if isinstance(value,list):
                    ref=checksum(value)
                    if ref in scope_lists and scope_lists[ref]!=value:
                        raise Conflict('Plan scope reference identity differs')
                    scope_lists[ref]=value
                    compact['completeness'][key]={'plan_scope_list_ref':ref}
        batches.append(dict(batch,payload=compact))
    return dict(payload,batches=batches,plan_storage=VERSION,
                plan_blocks=blocks,plan_obligations=obligations,plan_scope_lists=scope_lists)

def unpack(payload):
    if not payload.get('plan_storage'):return payload
    if payload['plan_storage'] not in (VERSION,'review-plan-refs-v1'):raise Conflict('Unknown plan storage version')
    batches=[]
    try:
        for batch in payload['batches']:
            packet=batch['payload']
            restored=dict(packet,documents=[payload['plan_blocks'][k] for k in packet['documents']],
                          obligations=[payload['plan_obligations'][k] for k in packet['obligations']])
            if payload['plan_storage']==VERSION and 'completeness' in packet:
                restored['completeness']=dict(packet['completeness'])
                for key,value in packet['completeness'].items():
                    if isinstance(value,dict) and set(value)=={'plan_scope_list_ref'}:
                        restored['completeness'][key]=payload['plan_scope_lists'][value['plan_scope_list_ref']]
            if checksum(restored)!=batch['id']:raise Conflict('Restored plan packet changed')
            batches.append(dict(batch,payload=restored))
    except (KeyError,TypeError) as error:
        raise Conflict('Missing plan reference') from error
    result=dict(payload,batches=batches)
    for k in ('plan_storage','plan_blocks','plan_obligations','plan_scope_lists'):result.pop(k,None)
    return result
