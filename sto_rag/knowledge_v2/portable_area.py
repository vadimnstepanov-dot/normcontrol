"""Portable area contract; original files, ACLs, model settings and trust never travel."""
import copy,json,re,time,uuid,math
from .store import checksum,encode,Conflict
from .curation import profile_graph
from .glossary import identity

FORMAT='normcontrol.normative-area';VERSION=1
LIMIT=32*1024*1024


def validate(value):
    required={'format','version','exported_at','area','sources','profiles','requirements','glossary','interdocument_requirements'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'controls'}:raise ValueError('Invalid portable area fields')
    if value['format']!=FORMAT or value['version']!=VERSION:raise ValueError('Unsupported normative area format/version')
    if len(encode(value).encode('utf8'))>LIMIT:raise ValueError('JSON area exceeds 32 MiB')
    if not isinstance(value['exported_at'],str) or len(value['exported_at'])>100:raise ValueError('Export timestamp')
    if not isinstance(value['area'],dict) or set(value['area'])!={'name','description','automatic'} or not 1<=len(value['area']['name'])<=160 or type(value['area']['automatic']) is not bool:raise ValueError('Area metadata')
    if not isinstance(value['glossary'],dict) or set(value['glossary'])!={'entries','choices'}:raise ValueError('Glossary fields')
    if not isinstance(value['area']['description'],str) or len(value['area']['description'])>4000:raise ValueError('Area description')
    lists=[value['sources'],value['profiles'],value['requirements'],value['glossary']['entries'],value['glossary']['choices'],value['interdocument_requirements']]
    if any(not isinstance(x,list) for x in lists) or len(value['sources'])>100 or len(value['profiles'])>100 or len(value['requirements'])+len(value['glossary']['entries'])>10000:raise ValueError('Portable collection limit')
    known={}
    for kind,rows in [('source',value['sources']),('profile',value['profiles']),('card',value['requirements']+value['glossary']['entries'])]:
        ids=set()
        for row in rows:
            uuid.UUID(row['id'])
            if row['id'] in ids:raise ValueError('Duplicate '+kind+' ID')
            ids.add(row['id'])
        known[kind]=ids
    for source in value['sources']:
        if not {'id','filename','sha256'}<=set(source) or not set(source)<={'id','filename','sha256','metadata'} or not re.fullmatch(r'[0-9a-f]{64}',source['sha256']):raise ValueError('Source digest')
        from .source_identity import FIELDS
        if not isinstance(source.get('metadata',{}),dict) or not set(source.get('metadata',{}))<=set(FIELDS) or any(not isinstance(v,str) or len(v)>2000 for v in source.get('metadata',{}).values()):raise ValueError('Source metadata')
        if not isinstance(source['filename'],str) or not 1<=len(source['filename'])<=255:raise ValueError('Source filename')
    profiles=[]
    for row in value['profiles']:
        if set(row)!={'id','definition'}:raise ValueError('Profile fields')
        d=row['definition']
        if set(d)!={'name','description','parents','bindings','expression'}:raise ValueError('Profile definition')
        from .applicability import validate_expression
        validate_expression(d['expression'])
        if not isinstance(d['name'],str) or not 1<=len(d['name'])<=160 or not isinstance(d['description'],str) or not 1<=len(d['description'])<=4000:raise ValueError('Profile name/description')
        if not isinstance(d['bindings'],list) or len(d['bindings'])>200:raise ValueError('Profile bindings')
        if not isinstance(d['parents'],list) or len(d['parents'])>100 or any(x not in known['profile'] for x in d['parents']):raise ValueError('Profile parent')
        for b in d['bindings']:
            if set(b)!={'source_id','profile_id'} or b['source_id'] not in known['source'] or not isinstance(b['profile_id'],str):raise ValueError('Profile source binding')
        profiles.append(dict(id=row['id'],definition=d))
    profile_graph(profiles)
    controls=value.get('controls',[]);seen=set()
    if not isinstance(controls,list) or len(controls)>11000:raise ValueError('Controls limit')
    objects=dict(known,area={'area'},link={l.get('id') for l in value['interdocument_requirements']})
    for control in controls:
        if not isinstance(control,dict) or set(control)!={'kind','id','enabled','deleted'} or control['kind'] not in objects or control['id'] not in objects[control['kind']] or type(control['enabled']) is not bool or type(control['deleted']) is not bool or (control['deleted'] and control['enabled']):raise ValueError('Object control')
        key=(control['kind'],control['id'])
        if key in seen:raise ValueError('Duplicate object control')
        seen.add(key)
    for row in value['requirements']+value['glossary']['entries']:
        if set(row)!={'id','source_id','payload'} or row['source_id'] not in known['source']:raise ValueError('Card source')
        card=row['payload']
        from .expert import TYPES
        if not isinstance(card,dict) or card.get('entity_type') not in TYPES or not card.get('description') or not card.get('citations'):raise ValueError('Normative card fields')
        if not isinstance(card['description'],str) or len(card['description'])>32000 or not isinstance(card['citations'],list) or len(card['citations'])>200:raise ValueError('Card text/citations')
        for citation in card['citations']:
            if not isinstance(citation,dict) or not {'locator','quote','start','end','context_hash'}<=citation.keys():raise ValueError('Citation identity')
        for field in ('conditions','exceptions','obligations','ambiguities'):
            if field in card and (not isinstance(card[field],list) or len(card[field])>500):raise ValueError('Card collection')
        for field in ('confidence','model_confidence'):
            if card.get(field) is not None and (type(card[field]) not in (int,float) or not math.isfinite(card[field]) or not 0<=card[field]<=1):raise ValueError('Model confidence')
        if card.get('local_profile') and card['local_profile'] not in known['profile']:raise ValueError('Local profile absent')
        if card.get('entity_type')=='definition' and card.get('term'):identity(card)
    if any(c['payload']['entity_type']=='definition' for c in value['requirements']) or any(c['payload']['entity_type']!='definition' for c in value['glossary']['entries']):raise ValueError('Glossary classification')
    choices=set()
    for choice in value['glossary']['choices']:
        if set(choice)!={'key','active_entry_id'} or choice['active_entry_id'] not in {c['id'] for c in value['glossary']['entries']}:raise ValueError('Glossary choice')
        c=next(x for x in value['glossary']['entries'] if x['id']==choice['active_entry_id'])
        if identity(c['payload'])[0]!=choice['key']:raise ValueError('Glossary choice name/type')
        if choice['key'] in choices:raise ValueError('Duplicate glossary choice')
        choices.add(choice['key'])
    for link in value['interdocument_requirements']:
        if not isinstance(link,dict) or any(link.get(k) not in known['card'] for k in ('source','target','basis')):raise ValueError('Interdocument reference')
        from .trace import validate_link
        validate_link(link)
    return value


def remap(value,ids):
    if isinstance(value,dict):return {k:remap(v,ids) for k,v in value.items()}
    if isinstance(value,list):return [remap(v,ids) for v in value]
    if isinstance(value,str):return ids.get(value,value)
    return value


def apply(store,command_id,payload,authorize):
    sid=payload['set_id'];actor=payload['actor_id']
    if not authorize(actor,sid,'review'):raise PermissionError('Area import permission revoked')
    if not authorize(actor,sid,'upload'):raise PermissionError('Area import upload permission revoked')
    old=store.command_result(command_id,'area.import',payload)
    if old:return old
    updates=[];extra_contexts={}
    from .expert import context,verify
    from .structure import atomic_json
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in payload['cards']:
            evidence=[];bases=[]
            for base_id in item['base_ids']:
                row=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind IN ('requirement','term_definition')",(base_id,sid)).fetchone()
                if not row:raise ValueError('Original normative evidence missing')
                base=json.loads(row[0])
                if base['source_revision'][0]!=item['source_id']:raise ValueError('Import source differs')
                bases.append(base);evidence.extend(context(store,db,sid,base))
            source=json.loads(db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=?",(item['source_id'],sid)).fetchone()[0])
            if source['sha256']!=item['sha256']:raise ValueError('Original file digest differs')
            card=copy.deepcopy(item['card'])
            # Verify every nested condition/parameter quote, not only the headline citation.
            def citations(value):
                if isinstance(value,dict):
                    if {'locator','quote','start','end','context_hash'}<=set(value):yield value
                    else:
                        for v in value.values():yield from citations(v)
                elif isinstance(value,list):
                    for v in value:yield from citations(v)
            all_cites={checksum(c):c for c in citations(card)}
            present={(c['locator'],c['context_hash']) for c in evidence}
            for cite in all_cites.values():
                key=(item['source_id'],cite['locator'],cite['context_hash'])
                if (cite['locator'],cite['context_hash']) in present:continue
                if key not in extra_contexts:
                    found=[]
                    for fragment_row in db.execute("SELECT payload FROM records WHERE set_id=? AND kind IN ('fragment','structured_fragment') AND json_extract(payload,'$.locator')=? AND json_extract(payload,'$.context_hash')=?",(sid,cite['locator'],cite['context_hash'])):
                        fragment=json.loads(fragment_row[0]);belongs=fragment.get('source_revision')==[item['source_id'],1]
                        if not belongs and fragment.get('parse_ref'):
                            parse=db.execute("SELECT payload FROM records WHERE set_id=? AND id=? AND version=? AND kind='parse_run'",(sid,*fragment['parse_ref'])).fetchone()
                            belongs=bool(parse and json.loads(parse[0]).get('source_revision')==[item['source_id'],1])
                        if belongs:found.append(dict(locator=fragment['locator'],context_hash=fragment['context_hash'],text=fragment['exact_text']))
                    if not found:raise ValueError('Nested source evidence unavailable')
                    if len({f['text'] for f in found})!=1:raise ValueError('Ambiguous nested source evidence')
                    extra_contexts[key]=found[0]
                evidence.append(extra_contexts[key])
            proof=dict(card,citations=list(all_cites.values()));verify(proof,list({c['locator']:c for c in evidence}.values()),source['sha256'])
            card['citations']=proof['citations'];card['id']=item['id'];card['expert_status']='unreviewed';card['expert_approved']=False
            for name in ('expert_approval','expert_resolution','publication_trust','refinement','merged_base_refs'):card.pop(name,None)
            card['ambiguities']=list(dict.fromkeys([*card.get('ambiguities',[]),'Импорт JSON: сопоставьте формулировку и условия с первоисточником; чужая экспертная отметка не переносится.']))
            # No external provenance assertion is accepted. Evidence is verified locally above.
            card['validation']=copy.deepcopy(bases[0]['card'].get('validation',{}));card['validation']['provenance']=dict(status='verified',errors=[])
            state=dict(card=card,base_ref=[item['base_ids'][0],1],extra_base_refs=[[x,1] for x in item['base_ids'][1:]],source_revision=[item['source_id'],1],status='unreviewed',actor_id=actor,reason='Импорт настроенной нормативной области; первоисточник проверен, требуется экспертное подтверждение.',action='create')
            store.put_record(sid,'expert_card',item['id'],1,state,refs=[state['base_ref'],*state['extra_base_refs']],_db=db)
            updates.append(dict(id=item['id'],source_id=item['source_id'],base_id=item['base_ids'][0],payload=card,status='unreviewed',revision=1))
        directory=store.directory/'area-imports';directory.mkdir(exist_ok=True)
        atomic_json(directory/(command_id+'.json'),updates)
        result=dict(kind='area.import.done',set_id=sid,count=len(updates),digest=checksum(updates))
        db.execute('INSERT INTO inbox VALUES(?,?,?,?)',(command_id,checksum(dict(kind='area.import',payload=payload)),encode(result),time.time()))
    return result
