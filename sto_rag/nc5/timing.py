"""A provisional forecast includes dependent work which is not queued yet."""
import math
import re
from collections import defaultdict
from statistics import median
from .search import terms


def remaining_seconds(job,tasks,findings,documents,budgets,cached_keys,now):
    samples=defaultdict(list);inputs=defaultdict(list);outputs=defaultdict(list);prompt_rates=[];generation_rates=[]
    for task in tasks:
        result=task.get('result') or {};metrics=result.get('metrics',{})
        if result.get('cached'):continue
        stage=task['stage'];timings=metrics.get('timings',{});usage=metrics.get('usage',{})
        if task.get('ended') and task.get('started'):samples[stage].append(task['ended']-task['started'])
        if usage.get('prompt_tokens'):inputs[stage].append(usage['prompt_tokens'])
        if usage.get('completion_tokens'):outputs[stage].append(usage['completion_tokens'])
        if timings.get('prompt_per_second',0)>0:prompt_rates.append(timings['prompt_per_second'])
        if timings.get('predicted_per_second',0)>0:generation_rates.append(timings['predicted_per_second'])
    default_output={'language':800,'logic':1800,'sto':700,'cross':1100,'inter':1100,'verify':500}
    def seconds(stage,prompt=None):
        if prompt_rates and generation_rates:
            pin=prompt or (median(inputs[stage]) if inputs[stage] else 4000)
            pout=median(outputs[stage]) if outputs[stage] else default_output.get(stage,1000)
            return pin/median(prompt_rates)+pout/median(generation_rates)+1
        return median(samples[stage]) if samples[stage] else 35
    remaining=0
    for task in tasks:
        if task['state'] not in ('pending','running'):continue
        budget=budgets.get(task['id'],{})
        predicted=1 if task.get('cache_key') in cached_keys and task['state']=='pending' else seconds(task['stage'],budget.get('input_tokens'))
        if task['state']=='running':predicted=max(3,predicted-(now-(task.get('started') or now)))
        remaining+=predicted
    derived=job['data'].get('derived',[]);options=job['data']['options'];future={}
    if 'cross' not in derived and options.get('check_logic',True):
        topics=defaultdict(list)
        logic=[t for t in tasks if t['stage']=='logic']
        for task in logic:
            for fact in (task.get('result') or {}).get('facts',[]):
                topics[' '.join(sorted(set(terms(fact['parameter'])))) or 'other'].append(fact)
        groups=sum(math.ceil(len(fs)/8) for fs in topics.values() if len({(e['document'],e['locator']) for f in fs for e in f['evidence']})>=2)
        finished=sum(t['state'] in ('done','split') for t in logic)
        projected=math.ceil(groups*len(logic)/finished) if finished else math.ceil(len(logic)/3)
        links=sum(math.ceil(sum(bool(re.search(r'приложени[еяию]\s*[А-ЯA-Z\d]|(?:рисунк|таблиц|пункт)[аеуы]?\s+\d',b['text'],re.I)) and not b.get('toc') for b in d['blocks'])/12) for d in documents)
        future['cross']=projected+links
    if 'inter' not in derived:
        related=job['data'].get('relationships',{}).get('groups',[])
        future['inter']=12*len(related)
    if 'verify' not in derived:
        candidates=sum(f['status']=='candidate' for f in findings)
        analysed=sum(t['state']=='done' and t['stage']!='verify' for t in tasks)
        reviewed=sum(f['status'] not in ('style','candidate','verifying') for f in findings)
        future_analysis=sum(future.values())+sum(t['state'] in ('pending','running') and t['stage']!='verify' for t in tasks)
        predicted_candidates=future_analysis*(reviewed/max(1,analysed) if analysed else .3)
        future['verify']=math.ceil((candidates+predicted_candidates)/3)
    remaining+=sum(count*seconds(stage) for stage,count in future.items())
    if job['state']=='preparing' and not tasks:remaining=max(remaining,60*max(1,len(job['data'].get('paths',[]))))
    return remaining,future
