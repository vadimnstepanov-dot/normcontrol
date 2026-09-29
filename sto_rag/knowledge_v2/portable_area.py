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


def source_evidence(store, db, sid, source_id, source, command_id):
    """Read-only source verification; no LLM, inferred table roles or old cards.

    Imported context hashes are transport identities, not trusted local evidence.
    DOCX locations/text are rebuilt from its checksummed OOXML. For other formats
    require already parsed source-scoped fragments (never guessed OCR content).
    """
    from pathlib import Path
    import hashlib
    path=(store.directory/source['original_key']).resolve(strict=True)
    if not path.is_relative_to((store.directory/'originals').resolve()):raise ValueError('Unsafe original source path')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=source['sha256']:raise ValueError('Original file digest differs')
    if path.suffix.casefold()=='.docx':
        import tempfile
        from .structure_docx import read_docx
        with tempfile.TemporaryDirectory(prefix='import-evidence-') as assets:
            blocks=read_docx(path,Path(assets))['blocks']
        found={b['locator']:dict(locator=b['locator'],text=b['exact_text'],
            context_hash=checksum(['portable-source-evidence-v1',source['sha256'],b['locator'],b['exact_text']]),
            structure={k:b[k] for k in ('heading_path','heading_addresses','table_label','row','column','source_locator','kind') if k in b}) for b in blocks}
    else:
        found={}
        for row in db.execute("SELECT payload FROM records WHERE set_id=? AND kind IN ('fragment','structured_fragment') ORDER BY rowid",(sid,)):
            b=json.loads(row[0])
            if b.get('source_revision')!=[source_id,1]:continue
            previous=found.get(b['locator'])
            if previous and previous['text']!=b['exact_text']:raise ValueError('Ambiguous source fragment')
            found[b['locator']]=dict(locator=b['locator'],text=b['exact_text'],context_hash=b['context_hash'],structure=b.get('structure',{}))
    if not found:raise ValueError('Original has no readable source evidence')
    return found


def rebind_citations(value, contexts, source_hash):
    """Verify offsets AND text before assigning a local context hash."""
    if isinstance(value,dict):
        if {'locator','quote','start','end','context_hash'}<=set(value):
            f=contexts.get(value['locator']);a=value['start'];b=value['end']
            if (not f or value.get('source_sha256',source_hash)!=source_hash or type(a) is not int or type(b) is not int
                or not 0<=a<b<=len(f['text']) or f['text'][a:b]!=value['quote']):raise ValueError('Source citation mismatch')
            value['context_hash']=f['context_hash'];value['source_sha256']=source_hash
        else:
            for child in value.values():rebind_citations(child,contexts,source_hash)
    elif isinstance(value,list):
        for child in value:rebind_citations(child,contexts,source_hash)


def direct_card(store,db,sid,item,source,source_contexts):
    from .expert import verify
    from .applicability import validate_expression
    card=copy.deepcopy(item['card']);rebind_citations(card,source_contexts,source['sha256'])
    if card.get('applicability'):validate_expression(card['applicability'])
    # Externally supplied expert/semantic completeness assertions cannot become
    # local approval. Only lexical provenance is established by this operation.
    for name in ('expert_approval','expert_resolution','publication_trust','refinement','merged_base_refs'):card.pop(name,None)
    card['validation']=dict(provenance=dict(status='verified',errors=[]),
        completeness=dict(semantic='needs_review',structural='source_quotes_verified',reasons=['independent_json_requires_expert_review']))
    evidence={}
    def collect(value):
        if isinstance(value,dict):
            if {'locator','quote','start','end','context_hash'}<=set(value):evidence[value['locator']]=source_contexts[value['locator']]
            else:
                for child in value.values():collect(child)
        elif isinstance(value,list):
            for child in value:collect(child)
    collect(card)
    refs=[]
    for loc,f in evidence.items():
        fid=str(uuid.uuid5(uuid.UUID(item['source_id']),'portable-evidence:'+f['context_hash']))
        store.put_record(sid,'fragment',fid,1,dict(source_revision=[item['source_id'],1],locator=loc,
            exact_text=f['text'],search_text=f['text'],context_hash=f['context_hash'],structure=f.get('structure',{})),_db=db)
        refs.append([fid,1])
    card['id']=item['id'];card['expert_approved']=False;card['expert_status']='unreviewed'
    verify(card,list(evidence.values()),source['sha256'])
    base_id=item['base_ids'][0]
    store.put_record(sid,'term_definition' if card['entity_type']=='definition' else 'requirement',base_id,1,
        dict(source_revision=[item['source_id'],1],fragment_refs=refs,card=card,modality=card.get('modality','unknown'),
            condition=card.get('applicability',{'unknown':'Imported reference material'}),extractor_version='portable-source-evidence-v1',import_origin='independent_json'),_db=db)
    return card,list(evidence.values())


def apply(store,command_id,payload,authorize):
    sid=payload['set_id'];actor=payload['actor_id']
    if not authorize(actor,sid,'review'):raise PermissionError('Area import permission revoked')
    if not authorize(actor,sid,'upload'):raise PermissionError('Area import upload permission revoked')
    old=store.command_result(command_id,'area.import',payload)
    if old:return old
    updates=[];extra_contexts={};source_context_cache={}
    from .expert import context,verify
    from .structure import atomic_json
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in payload['cards']:
            if item.get('evidence_mode')=='source_fragments':
                row=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind='source_revision'",(item['source_id'],sid)).fetchone()
                if not row:raise ValueError('Original source unavailable')
                source=json.loads(row[0])
                if source['sha256']!=item['sha256']:raise ValueError('Original file digest differs')
                if item['source_id'] not in source_context_cache:source_context_cache[item['source_id']]=source_evidence(store,db,sid,item['source_id'],source,command_id)
                # Additional glossary origins belong to their own checksummed
                # originals. Never validate a foreign paragraph against the
                # primary source merely because its locator happens to match.
                local_item=copy.deepcopy(item)
                origins=local_item['card'].pop('additional_origins',[])
                allowed_sources={c['source_id'] for c in payload['cards']}|{s['id'] for s in payload.get('source_metadata',[])}
                for origin in origins:
                    origin_id=origin.get('source_id')
                    if origin_id not in allowed_sources:raise ValueError('Additional origin outside imported sources')
                    origin_row=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind='source_revision'",(origin_id,sid)).fetchone()
                    if not origin_row:raise ValueError('Additional original unavailable')
                    original=json.loads(origin_row[0])
                    if origin.get('source_sha256')!=original['sha256']:raise ValueError('Additional original digest differs')
                    if origin_id not in source_context_cache:source_context_cache[origin_id]=source_evidence(store,db,sid,origin_id,original,command_id)
                    fragment=source_context_cache[origin_id].get(origin.get('locator'))
                    verbatim=origin.get('verbatim')
                    if not fragment or not isinstance(verbatim,str) or not verbatim or verbatim not in fragment['text']:raise ValueError('Additional origin text differs')
                    rebind_citations(origin,source_context_cache[origin_id],original['sha256'])
                card,evidence=direct_card(store,db,sid,local_item,source,source_context_cache[item['source_id']])
                if origins:card['additional_origins']=origins
                card['ambiguities']=list(dict.fromkeys([*card.get('ambiguities',[]),'Импорт JSON: необходима экспертная сверка формулировки и применимости.']))
                state=dict(card=card,base_ref=[item['base_ids'][0],1],extra_base_refs=[],source_revision=[item['source_id'],1],status='unreviewed',actor_id=actor,reason='Импорт JSON непосредственно из проверенного оригинала; требуется экспертная сверка.',action='create')
                store.put_record(sid,'expert_card',item['id'],1,state,refs=[state['base_ref']],_db=db)
                updates.append(dict(id=item['id'],source_id=item['source_id'],base_id=item['base_ids'][0],payload=card,contexts=evidence,status='unreviewed',revision=1))
                continue
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
