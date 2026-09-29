"""Compare complete-document packing with grouped exhaustive section passes."""
from .store import checksum
from bisect import bisect_left

VERSION='context-budget-v5'

def plan(rows,blocks,client,scope,max_group=8):
    from .review import request
    if not rows or not blocks:return [],list(rows)
    # Keep atoms from the same source clause together: their normative evidence
    # is shared in transport, while every obligation retains its own decision.
    ordered=sorted(rows,key=lambda r:(str(r.get('source_revision','')),str(r.get('requirement_ref',r['id']))))
    limit=max(1,min(max_group,client.output_tokens//128 or 1))
    reserve=2*client.output_tokens+512
    fit_cache={}
    budget=client.context-reserve
    weights=[0]
    for block in blocks:weights.append(weights[-1]+len(block['text'])+40+len(str(block.get('header_path',[]))))
    def count(group,evidence):
        key=(tuple(r['id'] for r in group),tuple(b['id'] for b in evidence))
        if key in fit_cache:return fit_cache[key]
        measured=dict(scope,part=999999,parts=999999,submitted_ids=[b['id'] for b in evidence],full_text=False)
        fit_cache[key]=client.count(request(group,evidence,measured))
        return fit_cache[key]
    def fits(group,evidence):return count(group,evidence)<=budget
    built={}
    def build(group,call_limit=None):
        key=tuple(r['id'] for r in group)
        if key not in built:
            result=build_uncached(group,call_limit)
            if result is None:return None  # Dominated candidate, never a partial plan.
            built[key]=result
        return built[key]
    def build_uncached(group,call_limit=None):
        if fits(group,blocks):
            complete=dict(scope,part=0,parts=1,submitted_ids=[b['id'] for b in blocks],full_text=True)
            packet=request(group,blocks,complete)
            return [dict(id=checksum(packet),payload=packet)],[]
        if not fits(group,[]):
            if len(group)==1:return [],list(group)
            mid=len(group)//2;left,lf=build(group[:mid]);right,rf=build(group[mid:]);return left+right,lf+rf
        chunks=[];start=0
        while start<len(blocks):
            if call_limit is not None and len(chunks)>=call_limit:return None
            # Interpolate a candidate using measured endpoints and cheap text
            # weights. Weights choose only the probe: exact template counts
            # always decide fit, including the last accepted boundary.
            end=start;high=len(blocks)
            low_tokens=count(group,[]);high_tokens=count(group,blocks[start:high])
            if high_tokens<=budget:end=high
            else:
                while high-end>1:
                    fraction=max(.01,min(.99,(budget-low_tokens)/max(1,high_tokens-low_tokens)))
                    target=weights[end]+fraction*(weights[high]-weights[end])
                    mid=max(end+1,min(high-1,bisect_left(weights,target,end+1,high)))
                    tokens=count(group,blocks[start:mid])
                    if tokens<=budget:end=mid;low_tokens=tokens
                    else:high=mid;high_tokens=tokens
            if end==start:
                if len(group)==1:return [],list(group)
                mid=len(group)//2;left,lf=build(group[:mid]);right,rf=build(group[mid:]);return left+right,lf+rf
            boundaries=[i for i in range(start+1,end) if blocks[i].get('headings')!=blocks[i-1].get('headings')]
            if end<len(blocks) and boundaries and boundaries[-1]>start+(end-start)//2:end=boundaries[-1]
            chunks.append(blocks[start:end]);start=end
        batches=[]
        for index,evidence in enumerate(chunks):
            complete=dict(scope,part=index,parts=len(chunks),submitted_ids=[b['id'] for b in evidence],full_text=len(chunks)==1)
            payload=request(group,evidence,complete)
            batches.append(dict(id=checksum(payload),payload=payload))
        return batches,[]
    # Optimize all contiguous group sizes, not just the largest group and the
    # whole-document special case. Filling the context with norms can otherwise
    # leave only a few cells per request and create dozens of document passes.
    # DP's lexicographic cost first preserves executable coverage, then minimizes
    # actual requests, then repeated document text. Every candidate is measured
    # with the same template/tokenizer used for inference.
    if len(ordered)<=limit and fits(ordered,blocks):return build(ordered)
    best={len(ordered):((0,0,0),[],[])}
    for start in range(len(ordered)-1,-1,-1):
        winner=None
        # Establish a finite bound with single-atom groups first. Large groups
        # are abandoned as soon as their partial cost already exceeds it.
        for size in range(1,min(limit,len(ordered)-start)+1):
            tail_cost,tail_tasks,tail_failed=best[start+size]
            # At least one call is necessary for a nonempty executable group.
            # Equal calls can still improve repeated text, so do not skip ties.
            if winner and winner[0][0]==0 and tail_cost[0]==0 and 1+tail_cost[1]>winner[0][1]:continue
            group=ordered[start:start+size]
            if size>1 and not fits(group,[]):continue  # DP already considers its subdivisions.
            call_limit=winner[0][1]-tail_cost[1] if winner and winner[0][0]==0 and tail_cost[0]==0 else None
            result=build(group,call_limit)
            if result is None:continue
            tasks,failed=result
            repeated=sum(sum(len(b['text']) for b in t['payload']['documents']) for t in tasks)
            cost=(len(failed)+tail_cost[0],len(tasks)+tail_cost[1],repeated+tail_cost[2])
            if winner is None or cost<winner[0]:winner=(cost,tasks+tail_tasks,failed+tail_failed)
            if cost==(0,(len(ordered)-start+limit-1)//limit,
                         sum(len(b['text']) for b in blocks)*((len(ordered)-start+limit-1)//limit)):break
        best[start]=winner
    return best[0][1],best[0][2]


def summary(rows,batches,oversized,docs):
    from collections import Counter
    text=sum(len(b['text']) for d in docs for b in d['blocks'])
    repeated=sum(len(b['text']) for batch in batches for b in batch['payload']['documents'])
    return dict(strategy='minimum_calls_contiguous_groups_v5',
        documents=[dict(id=d['id'],type=d.get('classification',{}).get('type','unknown'),blocks=len(d['blocks']),
                        text_chars=sum(len(b['text']) for b in d['blocks']),gaps=len(d.get('gaps',[]))) for d in docs],
        applicability=dict(Counter(r['applicability']['result'] for r in rows)),
        analyzed_obligations=len({r['id'] for b in batches for r in b['payload']['obligations']}),
        oversized=len(oversized),tasks=len(batches),max_base_model_calls=2*len(batches),
        document_text_passes=round(repeated/max(1,text),2),
        verification='decisive_proposals_only; unknown stays unknown',complete_scope_required=True)
