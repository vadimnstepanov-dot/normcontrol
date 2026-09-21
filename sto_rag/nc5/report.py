import html
import json
import time
from collections import Counter
from .common import write,DATA,dumps

LABELS={'preparing':'Подготовка','running':'Проверяется','paused':'Приостановлена','partial':'Завершена с непроверенными областями','completed':'Проверка завершена','failed':'Ошибка выполнения','cancelled':'Отменена'}

def build(engine,jid):
    job=engine.store.job(jid);tasks=engine.store.tasks(jid);findings=engine.store.findings(jid);coverage=list(job['data'].get('coverage_skipped',[]))+list(job['data'].get('deterministic_coverage',[]));limitations=list(job['data']['limitations'])
    for t in tasks:
        r=t['result'] or {}
        coverage.extend({'task':t['id'],'stage':t['stage'],**x} for x in r.get('coverage',[]))
        if t['state']=='failed':limitations.append({'task':t['id'],'stage':t['stage'],'reason':t['error'],'remedy':'Исправить причину и повторить только эту задачу'})
        for x in r.get('invalid',[]):limitations.append({'task':t['id'],**x,'remedy':'Повторная проверка элемента с исходной цитатой'})
        for x in (r.get('raw') or {}).get('limitations',[]):limitations.append({'task':t['id'],'reason':x})
    visual_seen={(image['caption']['document'],loc) for t in tasks if t['state']=='done' for image in t['payload'].get('images',[]) for loc in image.get('occurrences',[])}
    for d in job['data'].get('documents',[]):
        for kind in ('images','objects'):
            limitations.extend({'document':d['name'],**x,**({'state':'visual_reviewed_unconfirmed','reason':'Изображение обработано визуальной моделью; требуется проверка выводов человеком'} if (d['id'],x['locator']) in visual_seen else {})} for x in d['coverage'].get(kind,[]))
    limitations.extend(x for x in job['data'].get('formatting_coverage',[]) if x['state'] in ('unverified','source_conflict'))
    elapsed=sum((t['ended']-t['started']) for t in tasks if t['ended'] and t['started'])
    metrics={'task_seconds':elapsed,'task_count':len(tasks),'cached':sum(bool((t['result'] or {}).get('cached')) for t in tasks), 'prompt_tokens':sum(((t['result'] or {}).get('metrics',{}).get('usage',{}).get('prompt_tokens',0)) for t in tasks),'completion_tokens':sum(((t['result'] or {}).get('metrics',{}).get('usage',{}).get('completion_tokens',0)) for t in tasks),'max_estimated_prompt':max([((t['result'] or {}).get('metrics',{}).get('estimated_tokens',0)) for t in tasks] or [0]),'retries':sum(max(0,t['attempts']-1) for t in tasks)}
    with engine.store.connect() as conn:
        calls=[json.loads(row['data']) for row in conn.execute("SELECT data FROM events WHERE job=? AND kind='generation_attempt'",(jid,))]
    counts=Counter(c['task'] for c in calls)
    generated=[t for t in tasks if t['started'] and not (t['result'] or {}).get('cached')]
    measured=all(t['id'] in counts for t in generated)
    metrics['generation_attempts']=len(calls) if measured else None
    metrics['retries']=sum(max(0,n-1) for n in counts.values()) if measured else None
    metrics['retry_measurement']='event_log' if measured else 'not_recorded_by_legacy_runner'
    metrics['by_stage']={}
    for stage in dict.fromkeys(t['stage'] for t in tasks):
        subset=[t for t in tasks if t['stage']==stage]
        ms=[(t['result'] or {}).get('metrics',{}) for t in subset]
        metrics['by_stage'][stage]={'tasks':len(subset),'cached':sum(bool((t['result'] or {}).get('cached')) for t in subset),
            'task_seconds':sum(t['ended']-t['started'] for t in subset if t['ended'] and t['started']),
            'generation_seconds':sum(m.get('seconds',0) for m in ms),
            'prompt_tokens':sum(m.get('usage',{}).get('prompt_tokens',0) for m in ms),
            'completion_tokens':sum(m.get('usage',{}).get('completion_tokens',0) for m in ms)}
    return {'id':jid,'status':LABELS[job['state']],'state':job['state'],'version':job['data']['version'],'implementation':job['data'].get('implementation'),'catalog':job['data']['catalog'],'model':job['data'].get('model'),'options':{k:v for k,v in job['data']['options'].items() if k.startswith('check_') or k=='formatting'},'documents':job['data'].get('documents',[]),'relationships':job['data'].get('relationships',{}),'findings':findings,'coverage':coverage,'visual_coverage':job['data'].get('visual_coverage',[]),'formatting_coverage':job['data'].get('formatting_coverage',[]),'structure_coverage':job['data'].get('structure_coverage',[]),'fact_coverage':job['data'].get('fact_coverage',{}),'traceability':job['data'].get('traceability',[]),'execution':{'web':'VPS: authentication, uploads, progress and reports','analysis':'local desktop: parsing, RAG, LLM and report construction'},'tasks':[{'id':t['id'],'stage':t['stage'],'state':t['state'],'error':t['error']} for t in tasks],'limitations':limitations,'metrics':metrics,'created':job['created'],'updated':job['updated']}

def markdown(report):
    r=report;lines=['# '+r['status'],'',f'Конвейер {r["version"]}; каталог {r["catalog"]}.','', 'Отсутствие подтверждённых замечаний не означает соответствия при непроверенных областях.','']
    for d in r['documents']:lines+=['- '+d['name']+' — '+d['profile']['type']+'; SHA-256: '+d['sha256']]
    for status,title in [('confirmed','Подтверждённые замечания'),('question','Вопросы'),('candidate','Предварительные кандидаты'),('verifying','На перепроверке'),('style','Редакторские предложения')]:
        lines+=['','## '+title,'']
        for f in r['findings']:
            if f['status']!=status:continue
            lines+=['### '+f['id'][:8]+' — '+f['issue'],'']
            if f.get('quality_gate'):lines+=['Ограничение подтверждения: '+f['quality_gate'],'','Исходная гипотеза модели: '+f['explanation'],'']
            else:lines+=[f['explanation'],'']
            lines+=['Предложение: '+f['suggestion'],'']
            for e in f['evidence']:lines+=[e['address'],'','> '+e['quote'].replace('\n','\n> '),'']
            if f.get('source'):
                lines+=['Основание: '+f['source']['document_name']+'; '+f['source']['clause']+'; '+f['source']['source_locator'],'']
                if f['source'].get('source_quote'):lines+=['Цитата нормативного источника:','','> '+f['source']['source_quote'].replace('\n','\n> '),'']
            if f.get('verification'):lines+=['Перепроверка: '+f['verification'],'']
    lines+=['','## Покрытие и ограничения','',f'Задач: {r["metrics"]["task_count"]}; время задач: {r["metrics"]["task_seconds"]/60:.1f} мин.','']
    for l in r['limitations']:lines+=['- '+(l if isinstance(l,str) else dumps(l))]
    lines+=['','## Матрица требований','']
    for c in r['coverage']:lines+=['- '+c['requirement_id']+': '+c['state']+' — '+c['reason']]
    return '\n'.join(lines)

def html_report(report):
    esc=html.escape
    body=[]
    for line in markdown(report).splitlines():
        if line.startswith('### '):body.append('<h3>'+esc(line[4:])+'</h3>')
        elif line.startswith('## '):body.append('<h2>'+esc(line[3:])+'</h2>')
        elif line.startswith('# '):body.append('<h1>'+esc(line[2:])+'</h1>')
        elif line.startswith('> '):body.append('<blockquote>'+esc(line[2:])+'</blockquote>')
        elif line:body.append('<p>'+esc(line)+'</p>')
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Нормоконтроль</title><style>body{font:16px/1.6 system-ui;max-width:1060px;margin:40px auto;padding:24px;color:#183047;background:#f5f7fa}h1,h2{color:#114a56}h3{border-top:1px solid #ccd8de;padding-top:24px}blockquote{background:white;border-left:4px solid #189e91;padding:16px;white-space:pre-wrap}p{overflow-wrap:anywhere}</style>'+''.join(body)+'</html>'

def export(engine,jid):
    r=build(engine,jid);folder=DATA/'jobs'/jid;write(folder/'report.json',r)
    (folder/'report.md').write_text(markdown(r),encoding='utf-8');(folder/'report.html').write_text(html_report(r),encoding='utf-8');return folder
