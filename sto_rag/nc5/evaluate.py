"""Offline evaluator only. The production pipeline must never import this module."""
import argparse
import json
from pathlib import Path
from .common import digest,read,write,DATA

def walk_evidence(value):
    result=[]
    if isinstance(value,dict):
        if 'quote' in value and any(k in value for k in ('locator','paragraph','paragraph_id')):result.append(value)
        for k,v in value.items():
            if k not in ('quote','source_quote'):result.extend(walk_evidence(v))
    elif isinstance(value,list):
        for x in value:result.extend(walk_evidence(x))
    return result

def evaluate(reference_path,report_path,output,adjudication=None):
    ref=read(reference_path);report=read(report_path);assessment=read(adjudication) if adjudication else {};decisions=assessment.get('reference',assessment);rows=[]
    # Inspect schema rather than assuming arbitrary dictionaries are gold findings.
    findings=ref.get('findings',ref.get('confirmed_findings',[]))
    if not isinstance(findings,list):raise ValueError('Unsupported reference findings schema')
    expected_hash=ref.get('source',{}).get('sha256') or ref.get('document',{}).get('sha256') or ref.get('source_sha256')
    actual_hashes={d['sha256'] for d in report['documents']}
    if expected_hash and expected_hash not in actual_hashes:raise ValueError('Reference/source versions differ')
    issued=[f for f in report['findings'] if f['status']=='confirmed']
    issued_ids={f['id'] for f in issued}
    for i,r in enumerate(findings):
        rid=r.get('id',str(i));evidence=walk_evidence(r);locs={str(e.get('locator') or e.get('paragraph') or e.get('paragraph_id')) for e in evidence}
        suggestions=[]
        for f in issued:
            overlap=locs & {str(e['locator']) for e in f['evidence']}
            if overlap:suggestions.append({'finding':f['id'],'shared_locators':sorted(overlap),'issue':f['issue']})
        decision=decisions.get(rid,{})
        matches=decision.get('findings') or ([decision['finding']] if decision.get('finding') else [])
        if decision.get('status')=='found' and (not matches or not isinstance(matches,list) or any(x not in issued_ids for x in matches) or not decision.get('comment')):raise ValueError('Found requires issued confirmed findings and semantic explanation: '+rid)
        rows.append({'reference':rid,'reference_status':r.get('status','confirmed'),'category':r.get('category'),'severity':r.get('severity'),'title':r.get('title',r.get('issue','')),'status':decision.get('status','needs_semantic_adjudication'),'finding':matches[0] if matches else None,'findings':matches,'comment':decision.get('comment','Совпадение локатора само по себе не считается обнаружением'),'candidates':suggestions})
    confirmed_ref=[r for r in findings if r.get('status','confirmed') in ('confirmed','подтверждено','confirmed_finding')]
    found=sum(x['status']=='found' and x['reference_status']=='confirmed' for x in rows);complete=all(x['status']!='needs_semantic_adjudication' for x in rows if x['reference_status']=='confirmed')
    judged=assessment.get('issued',{})
    if set(judged)-issued_ids:raise ValueError('Precision assessment contains a non-issued finding')
    if any(x.get('verdict') not in ('correct','incorrect','partially_correct') or not x.get('comment') for x in judged.values()):raise ValueError('Every precision judgment needs a verdict and a reason')
    negative=[]
    for i,n in enumerate(ref.get('negative_controls',[]),1):
        key='NEG-'+str(i).zfill(2);decision=assessment.get('negative_controls',{}).get(key,{})
        overlap=[f['id'] for f in issued if set(n.get('locators',[])) & {e['locator'] for e in f['evidence']}]
        negative.append({'id':key,'title':n['title'],'candidate_overlaps':overlap,'triggered':decision.get('triggered'),'finding':decision.get('finding'),'comment':decision.get('comment','Нужна семантическая оценка; совпадение локатора не означает ложное срабатывание')})
    precision=sum(x['verdict']=='correct' for x in judged.values())/len(judged) if judged else None
    result={'reference_sha256':digest(Path(reference_path).read_bytes()),'report_sha256':digest(Path(report_path).read_bytes()),'reference_items':len(findings),'confirmed_reference_items':len(confirmed_ref),'issued_confirmed':len(issued),'found':found,'recall':found/len(confirmed_ref) if complete and confirmed_ref else None,'precision':precision,'precision_sample':len(judged),'precision_full_census':len(judged)==len(issued),'precision_reason':'Строгая оценка: частично верное замечание не считается полностью корректным; новые замечания учитываются наравне с референсными.','negative_controls':negative,'negative_controls_complete':all(isinstance(x['triggered'],bool) for x in negative),'adjudication_complete':complete,'rows':rows,'metrics':report['metrics']}
    for dimension in ('category','severity'):
        summary={}
        for row in rows:
            if row['reference_status']!='confirmed':continue
            item=summary.setdefault(row[dimension] or 'unspecified',{'reference':0,'found':0})
            item['reference']+=1;item['found']+=row['status']=='found'
        for item in summary.values():item['recall']=item['found']/item['reference'] if complete else None
        result['by_'+dimension]=summary
    duplicates=[fid for fid,value in judged.items() if value.get('duplicate_of')]
    if any(judged[fid]['duplicate_of'] not in issued_ids for fid in duplicates):raise ValueError('Duplicate must refer to an issued finding')
    result['adjudicated_duplicates']=duplicates;result['duplicate_fraction']=len(duplicates)/len(issued) if len(judged)==len(issued) and issued else None
    write(output,result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--reference',required=True);p.add_argument('--report',required=True);p.add_argument('--out',required=True);p.add_argument('--adjudication');a=p.parse_args();r=evaluate(a.reference,a.report,a.out,a.adjudication);print({k:v for k,v in r.items() if k not in ('rows',)})
