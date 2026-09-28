"""Compare complete-document packing with grouped exhaustive section passes."""
from .store import checksum

def plan(rows,blocks,client,scope,max_group=8):
    from .review import request
    if not rows or not blocks:return [],list(rows)
    # Keep atoms from the same source clause together: their normative evidence
    # is shared in transport, while every obligation retains its own decision.
    ordered=sorted(rows,key=lambda r:(str(r.get('source_revision','')),str(r.get('requirement_ref',r['id']))))
    limit=max(1,min(max_group,client.output_tokens//128 or 1))
    reserve=2*client.output_tokens+512
    def fits(group,evidence):
        measured=dict(scope,part=999999,parts=999999,submitted_ids=[b['id'] for b in evidence],full_text=False)
        return client.count(request(group,evidence,measured))+reserve<=client.context
    built={}
    def build(group):
        key=tuple(r['id'] for r in group)
        if key not in built:built[key]=build_uncached(group)
        return built[key]
    def build_uncached(group):
        if not fits(group,[]):
            if len(group)==1:return [],list(group)
            mid=len(group)//2;left,lf=build(group[:mid]);right,rf=build(group[mid:]);return left+right,lf+rf
        chunks=[];start=0;previous=None
        while start<len(blocks):
            # Start near the preceding capacity instead of re-tokenizing a huge
            # remaining document in every binary search. Every final fit is exact.
            end=start;low=start+1;high=len(blocks)
            if previous:
                probe=min(high,start+previous)
                if fits(group,blocks[start:probe]):end=probe;low=probe+1
                else:high=probe-1
            while low<=high:
                mid=(low+high)//2
                if fits(group,blocks[start:mid]):end=mid;low=mid+1
                else:high=mid-1
            if end==start:
                if len(group)==1:return [],list(group)
                mid=len(group)//2;left,lf=build(group[:mid]);right,rf=build(group[mid:]);return left+right,lf+rf
            boundaries=[i for i in range(start+1,end) if blocks[i].get('headings')!=blocks[i-1].get('headings')]
            if end<len(blocks) and boundaries and boundaries[-1]>start+(end-start)//2:end=boundaries[-1]
            chunks.append(blocks[start:end]);previous=end-start;start=end
        batches=[]
        for index,evidence in enumerate(chunks):
            complete=dict(scope,part=index,parts=len(chunks),submitted_ids=[b['id'] for b in evidence],full_text=len(chunks)==1)
            payload=request(group,evidence,complete)
            batches.append(dict(id=checksum(payload),payload=payload))
        return batches,[]
    # Strategy A: maximum grouped pass. Strategy B: prefer the whole document
    # and choose the largest obligation group that fits, without splitting it.
    # Select fewer calls; when equal, select less repeated input.
    bulk=[];bulk_failed=[]
    for start in range(0,len(ordered),limit):
        tasks,failed=build(ordered[start:start+limit]);bulk.extend(tasks);bulk_failed.extend(failed)
    full=[];full_failed=[];start=0
    while start<len(ordered):
        available=min(limit,len(ordered)-start);best=0;low=1;high=available
        while low<=high:
            middle=(low+high)//2
            if fits(ordered[start:start+middle],blocks):best=middle;low=middle+1
            else:high=middle-1
        size=best or available;tasks,failed=build(ordered[start:start+size]);full.extend(tasks);full_failed.extend(failed);start+=size
    def cost(option):
        tasks,failed=option
        return len(failed),len(tasks),sum(len(b['payload']['documents']) for b in tasks)
    return min(((bulk,bulk_failed),(full,full_failed)),key=cost)

def summary(rows,batches,oversized,docs):
    from collections import Counter
    text=sum(len(b['text']) for d in docs for b in d['blocks'])
    repeated=sum(len(b['text']) for batch in batches for b in batch['payload']['documents'])
    return dict(strategy='compare_complete_document_and_grouped_passes',
        documents=[dict(id=d['id'],type=d.get('classification',{}).get('type','unknown'),blocks=len(d['blocks']),
                        text_chars=sum(len(b['text']) for b in d['blocks']),gaps=len(d.get('gaps',[]))) for d in docs],
        applicability=dict(Counter(r['applicability']['result'] for r in rows)),
        analyzed_obligations=len({r['id'] for b in batches for r in b['payload']['obligations']}),
        oversized=len(oversized),tasks=len(batches),max_base_model_calls=2*len(batches),
        document_text_passes=round(repeated/max(1,text),2),
        verification='decisive_proposals_only; unknown stays unknown',complete_scope_required=True)
