"""CPU retrieval over clean text, with source-provenance-preserving context bundles.

The derivative index never changes normative releases, approval or review ledgers.
Only the final, bounded candidates are loaded from the canonical store.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading

from .search import _manifest, _index_path
from .store import Conflict, NotReady, checksum

VERSION='context-search-v1'
KINDS=('requirement','term_definition','fragment','structured_fragment','review_case','clarification')
_LOCK=threading.Lock()
_STANDARD=re.compile(r'\b(СТО\s*РЖД|ГОСТ(?:\s*Р)?)\s*(\d+(?:\.\d+)+)\s*[-–—]\s*(\d{4})',re.I)
_WORDS=re.compile(r'[^\W_]+',re.UNICODE)
_UUID=re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b',re.I)
_STOP=set('а и или по в во на из к ко о об от до с со у для при это этой этого эти тот что как какой какие каким почему сколько ли же бы быть должен должны следует можно надо нужно используемый используемые приложенный приложенные документ документы документа документе документах требование требования раздел разделе пункт пункте сто ржд гост согласно предусмотрены проверь расскажи дай'.split())
_STOP.update('использовать использоваться используются используется применяться применяются применяется'.split())
_TEMPLATES={
 'оит':'описание информационной технологии','пми':'программа и методика испытаний',
 'чтз':'частное техническое задание','тз':'техническое задание','пр':'проектное решение',
 'рос':'руководство по организации сопровождения','рабд':'руководство администратора баз данных',
 'рас':'руководство администратора системы','рп':'руководство пользователя'}

def stem(word):
    """Small deterministic Russian morphology; no model or dictionary in RAM."""
    word=word.casefold().replace('ё','е')
    if not re.fullmatch('[а-я]+',word) or len(word)<4:return word
    # Long endings first. Keep the meaningful stem for information/functional terms.
    for ending in ('иями','ями','ами','ого','ему','ому','ыми','ими','иях','ах','ях','ов','ев','ых','их','ий','ый','ой','ая','яя','ое','ее','ые','ие','ую','юю','ам','ям','ом','ем','ей','ию','ия','ии','ы','и','а','я','у','ю','е','о','ь'):
        if word.endswith(ending) and len(word)-len(ending)>=3:return word[:-len(ending)]
    return word

def terms(text):
    return list(dict.fromkeys(stem(x) for x in _WORDS.findall(text.casefold().replace('ё','е')) if x not in _STOP and len(x)>1 and not _UUID.fullmatch(x)))

def normalized(text):return ' '.join(terms(text))

def query_plan(query):
    standards=[' '.join(m.group(1).upper().split())+' '+m.group(2)+'-'+m.group(3) for m in _STANDARD.finditer(query)]
    topic=_STANDARD.sub(' ',query)
    topic=re.sub(r'\b(?:раздел(?:у|а|е)?|пункт(?:у|а|е)?)\s+\d+(?:\.\d+)*',' ',topic,flags=re.I)
    templates=[];lower=topic.casefold();topic_roots=set(terms(topic))
    for alias,name in _TEMPLATES.items():
        if re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',lower) or set(terms(name)).issubset(topic_roots):
            templates.append(name)
            topic=re.sub(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',name,topic,flags=re.I)
    root_topic=topic
    for name in templates:
        template_roots=set(terms(name))
        root_topic=' '.join(w for w in _WORDS.findall(root_topic) if stem(w) not in template_roots)
    roots=terms(root_topic)[:24] or terms(topic)[:24]
    return dict(query=query,semantic_query=' '.join(topic.split()) or query,terms=roots,
                standards=list(dict.fromkeys(standards)),templates=list(dict.fromkeys(templates)))

def _order(locator):
    return tuple((0,int(x)) if x.isdigit() else (1,x) for x in re.split(r'(\d+)',locator))

def _template(headings):
    for heading in headings:
        match=re.search(r'шаблон\s+документ(?:а|ов)?\s*[«"]([^»"]+)',heading,re.I)
        if match:return match.group(1).casefold().strip()
    return ''

def _verified(db,items,ref,set_id):
    ref=tuple(ref);item=items.get(ref)
    if not item:raise Conflict('Context outside pinned manifest')
    row=db.execute('SELECT set_id,kind,payload,digest FROM records WHERE id=? AND version=?',ref).fetchone()
    if not row or row['set_id']!=set_id or row['digest']!=item['digest']:raise Conflict('Context provenance mismatch')
    value=json.loads(row['payload'])
    refs=[tuple(r) for r in db.execute('SELECT target_id,target_version FROM record_links WHERE record_id=? AND record_version=? ORDER BY target_id,target_version',ref)]
    if checksum(dict(set_id=set_id,kind=row['kind'],id=ref[0],version=ref[1],payload=value,refs=refs))!=item['digest']:raise Conflict('Canonical context digest mismatch')
    return row['kind'],value

def build_index(store,manifest,digest):
    """Streaming, disposable lexical derivative; no embedding or canon mutation."""
    directory=store.directory/'context-indexes';directory.mkdir(exist_ok=True)
    target=directory/(manifest['generation_id']+'-'+VERSION+'.sqlite3')
    if target.exists():
        with closing(sqlite3.connect(target)) as db:
            meta=dict(db.execute('SELECT key,value FROM metadata'))
        if meta.get('manifest_hash')!=digest or meta.get('version')!=VERSION:raise Conflict('Context index identity mismatch')
        return target
    with _LOCK:
        if target.exists():return build_index(store,manifest,digest)
        handle,name=tempfile.mkstemp(prefix='context-',suffix='.building',dir=directory);os.close(handle)
        tmp=Path(name)
        try:
            with store.connection() as source,closing(sqlite3.connect(tmp)) as db:
                items={(r['id'],r['version']):r for r in manifest['items']};effective=None
                policy=next((r for r in manifest['items'] if r['kind']=='publication_policy'),None)
                catalog={}
                if policy:
                    _,v=_verified(source,items,(policy['id'],policy['version']),manifest['set_id'])
                    effective=set(map(tuple,v['effective_refs']));catalog={tuple(x['ref']):x for x in v.get('catalog',[])}
                db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE sources(id TEXT PRIMARY KEY,version INTEGER,sha256 TEXT,name TEXT,designation TEXT);
                    CREATE TABLE entries(id TEXT,version INTEGER,kind TEXT,source_id TEXT,locator TEXT,template TEXT,heading TEXT,title TEXT,body TEXT,profiles TEXT,PRIMARY KEY(id,version));
                    CREATE INDEX entry_source_locator ON entries(source_id,locator);
                    CREATE VIRTUAL TABLE text_fts USING fts5(body,title,heading,content='entries',content_rowid='rowid',tokenize='unicode61');''')
                sources={};fragment_profiles={}
                for ref,item in items.items():
                    if item['kind']=='source_revision':
                        _,v=_verified(source,items,ref,manifest['set_id']);sources[ref[0]]=dict(version=ref[1],sha256=v['sha256'],name=v.get('filename',''),designation='')
                    if item['kind'] in ('requirement','term_definition') and (effective is None or ref in effective):
                        _,v=_verified(source,items,ref,manifest['set_id']);card=v.get('card',{})
                        memberships=card.get('profile_ids') or [card.get('effective_profile_id','')]
                        for f in v.get('fragment_refs',[]):fragment_profiles.setdefault(tuple(f),set()).update(x for x in memberships if x)
                for ref,item in items.items():
                    kind=item['kind']
                    if kind not in KINDS or (kind in ('requirement','term_definition') and effective is not None and ref not in effective):continue
                    _,v=_verified(source,items,ref,manifest['set_id']);origin=v.get('source_revision')
                    if not origin and v.get('fragment_refs'):
                        _,f=_verified(source,items,v['fragment_refs'][0],manifest['set_id']);origin=f.get('source_revision')
                    if not origin or origin[0] not in sources:continue
                    sid=origin[0];card=v.get('card',{});structure=v.get('structure',{})
                    headings=structure.get('heading_path',[]);profiles=fragment_profiles.get(ref,set())
                    locator=v.get('locator') or card.get('locator') or catalog.get(ref,{}).get('locator','')
                    if locator=='t1/r1/c3':
                        match=_STANDARD.search(v.get('exact_text',''))
                        if match:sources[sid]['designation']=' '.join(match.group(1).upper().split())+' '+match.group(2)+'-'+match.group(3)
                    if kind in ('fragment','structured_fragment'):
                        body=v['exact_text'];title=structure.get('table_label','')
                    elif kind in ('requirement','term_definition'):
                        units=card.get('obligations',[])
                        body='\n'.join(x.get('text','') if isinstance(x,dict) else str(x) for x in units)
                        body=body or '\n'.join(x.get('quote','') for x in card.get('citations',[]))
                        title=card.get('description','');profiles=set(card.get('profile_ids') or [card.get('effective_profile_id','')])-{'',None}
                        routes=[r for u in units if isinstance(u,dict) for r in u.get('source_routes',[])]
                        headings=list(dict.fromkeys(h for r in routes for h in r.get('section',[])))
                        # Imported routes live on atomic obligations, not the parent card.
                        if not headings and v.get('fragment_refs'):
                            for fref in v['fragment_refs'][:5]:
                                _,f=_verified(source,items,fref,manifest['set_id']);headings.extend(f.get('structure',{}).get('heading_path',[]))
                    else:body=' '.join(str(v.get(k,'')) for k in ('conditions','counterexample','summary'));title=''
                    if not body.strip():continue
                    heading='\n'.join(dict.fromkeys(headings))[:3000];template=_template(headings)
                    db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',(*ref,kind,sid,locator,template,heading,title,body,json.dumps(sorted(profiles))))
                    rowid=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    db.execute('INSERT INTO text_fts(rowid,body,title,heading) VALUES(?,?,?,?)',(rowid,normalized(body),normalized(title),normalized(heading)))
                for sid,v in sources.items():db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(sid,v['version'],v['sha256'],v['name'],v['designation']))
                db.executemany('INSERT INTO metadata VALUES(?,?)',[('manifest_hash',digest),('version',VERSION)]);db.commit()
            os.replace(tmp,target)
            return target
        finally:tmp.unlink(missing_ok=True)

def _match(plan):return ' OR '.join('"'+x+'"' for x in plan['terms'])

def _relevance(row,plan):
    body=set(terms(row['body']));title=set(terms(row['title']));heading=set(terms(row['heading']))
    roots=set(plan['terms']);den=max(1,len(roots))
    coverage=len(roots&body)/den
    score=4*coverage+1.3*len(roots&title)/den+.6*len(roots&heading)/den
    if plan['templates']:
        score+=2.5 if row['template'] in plan['templates'] else -.9 if row['template'] else -.15
    if row['kind'] in ('requirement','term_definition'):score+=.15
    if len(row['body'])<100 and re.match(r'^\s*(?:\d+[\d.]*\s|\(обязательное\))',row['body']):score-=1.4
    if row['body'].startswith('Примечание') and len(body&roots)<2:score-=1
    if len(row['body'].strip())<8:score-=1
    return score,coverage

class ContextSearch:
    def __init__(self,store,encoder,vector,authorize):
        self.store,self.encoder,self.vector,self.authorize=store,encoder,vector,authorize

    def reference(self,release_id,query,*,limit=6,profiles=None,expected_set_id=None):
        if not isinstance(query,str) or not 1<=len(query.strip())<=1200 or not 1<=limit<=30:raise ValueError('Context search budget')
        manifest,digest,state,_=_manifest(self.store,release_id)
        if expected_set_id is not None and manifest['set_id']!=expected_set_id:raise Conflict('Dialogue search set')
        if not self.authorize(manifest['set_id']):raise PermissionError('Read grant required')
        if state!='published':raise NotReady('Release not published')
        if manifest['embedding_space']!=self.encoder.space.id:raise Conflict('Query embedding space differs')
        plan=query_plan(query);path=build_index(self.store,manifest,digest)
        items={(x['id'],x['version']):x for x in manifest['items']}
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=sqlite3.Row
            selected_sources=[r['id'] for r in db.execute('SELECT id,designation FROM sources') if r['designation'] in plan['standards']]
            scope=[];args=[]
            if plan['standards']:
                if not selected_sources:return []
                scope.append('e.source_id IN ('+','.join('?' for _ in selected_sources)+')');args.extend(selected_sources)
            if profiles is not None:
                if not profiles:return []
                scope.append('EXISTS (SELECT 1 FROM json_each(e.profiles) p WHERE p.value IN ('+','.join('?' for _ in profiles)+'))');args.extend(profiles)
            where=' AND '+ ' AND '.join(scope) if scope else ''
            lexical=[]
            if plan['terms']:
                lexical=list(db.execute('SELECT e.*,bm25(text_fts,1.0,1.6,1.3) rank FROM text_fts f JOIN entries e ON e.rowid=f.rowid WHERE text_fts MATCH ?'+where+' ORDER BY bm25(text_fts,1.0,1.6,1.3) LIMIT 80',(_match(plan),*args)))
            # Exact navigation still works; scope/profiles apply before limit.
            exact=list(db.execute('SELECT e.* FROM entries e WHERE e.locator=?'+where+' LIMIT 30',(query.strip(),*args)))
            ranked={};exact_refs={(r['id'],r['version']) for r in exact}
            for rank,row in enumerate(lexical+exact):
                key=(row['id'],row['version']);relevance,coverage=_relevance(row,plan)
                ranked[key]=(relevance+1/(rank+1),row,coverage)
            # One bounded dense query complements lexical retrieval. Clean text,
            # template routing and final evidence resolution remain on CPU.
            query_vector=self.encoder.encode([plan['semantic_query']],query=True)[0]
            with closing(_index_path(self.store,manifest,digest)) as legacy:
                vector_ids=self.vector.search(manifest['generation_id'],query_vector,set_id=manifest['set_id'],release_id=release_id,kinds=KINDS,profiles=profiles,applicability=('applicable','unknown'),limit=48)
                for rank,eid in enumerate(vector_ids):
                    candidate=legacy.execute('SELECT record_id,version FROM entries WHERE id=?',(eid,)).fetchone()
                    if not candidate:continue
                    key=(candidate['record_id'],candidate['version'])
                    row=db.execute('SELECT e.* FROM entries e WHERE e.id=? AND e.version=?'+where,(*key,*args)).fetchone()
                    if not row:continue
                    relevance,coverage=_relevance(row,plan)
                    prev=ranked.get(key,(relevance,row,coverage));ranked[key]=(prev[0]+.45/(rank+1),row,coverage)
            sources={r['id']:dict(r) for r in db.execute('SELECT * FROM sources')}
            result=[];seen=set()
            with self.store.connection() as canonical:
                for key,(score,row,coverage) in sorted(ranked.items(),key=lambda x:(x[0] not in exact_refs,-x[1][0],x[0])):
                    if not self.authorize(manifest['set_id']):raise PermissionError('Read grant revoked')
                    resolved=self._resolve(canonical,db,manifest,items,sources,row,plan)
                    if not resolved or not resolved.get('quote'):continue
                    signature=(resolved['source_id'],resolved['locator'],resolved['quote'])
                    if signature in seen:continue
                    seen.add(signature);resolved['score']=score;result.append(resolved)
                    if len(result)>=limit:break
            if not self.authorize(manifest['set_id']):raise PermissionError('Read grant revoked')
            return result

    def _resolve(self,canonical,index,manifest,items,sources,row,plan):
        ref=(row['id'],row['version']);kind,value=_verified(canonical,items,ref,manifest['set_id'])
        source=sources[row['source_id']];_,origin=_verified(canonical,items,(source['id'],source['version']),manifest['set_id'])
        context=[];citations=[];summary='';complete=True;trust={};quality={}
        def fragment(sid,locator):
            options=index.execute("SELECT id,version FROM entries WHERE source_id=? AND locator=? AND kind IN ('fragment','structured_fragment') ORDER BY kind,id",(sid,locator)).fetchall()
            if not options:return None
            _,v=_verified(canonical,items,(options[0]['id'],options[0]['version']),manifest['set_id']);return v
        if kind in ('requirement','term_definition'):
            card=value.get('card',{});trust=card.get('publication_trust',{});quality=card.get('quality',{})
            units=card.get('obligations',[])
            scored=sorted([u for u in units if isinstance(u,dict)],key=lambda u:-len(set(terms(u.get('text','')))&set(plan['terms'])))
            matching=[u for u in scored if set(terms(u.get('text','')))&set(plan['terms'])]
            chosen=(matching or scored)[:2]
            summary='\n'.join(u.get('text','') for u in chosen)
            citations=[c for u in chosen for c in u.get('citations',[])] or card.get('citations',[])
            if not citations:
                for fref in value.get('fragment_refs',[]):
                    _,f=_verified(canonical,items,fref,manifest['set_id'])
                    if f['locator']==row['locator']:citations=[dict(locator=f['locator'],quote=f['exact_text'])];break
            # Legacy fixture citations use one source; imported citations pin SHA.
            valid=[]
            for c in citations:
                sid=next((sid for sid,s in sources.items() if s['sha256']==c.get('source_sha256')),None) if c.get('source_sha256') else row['source_id']
                if sid is None:raise Conflict('Norm citation source is outside pinned release')
                f=fragment(sid,c.get('locator',''))
                if not f or not c.get('quote') or c['quote'] not in f['exact_text']:raise Conflict('Quoted norm not backed by pinned source')
                if 'start' in c and 'end' in c and f['exact_text'][c['start']:c['end']]!=c['quote']:raise Conflict('Norm citation offsets differ')
                valid.append((sid,f,c['quote']))
            if not valid:return None
            valid.sort(key=lambda x:(-len(set(terms(x[2]))&set(plan['terms'])),_order(x[1]['locator'])))
            sid,primary,quote=valid[0];source=sources[sid]
            for sid,f,q in valid[1:]:context.append(dict(locator=f['locator'],exact_text=q,source_id=sid))
            complete=not any(d.get('unresolved') for d in card.get('dependencies',[]))
            for dependency in card.get('dependencies',[]):
                if not dependency.get('required'):continue
                target=dependency.get('target')
                related=fragment(sid,target) if isinstance(target,str) else None
                if related:context.append(dict(locator=related['locator'],exact_text=related['exact_text'],source_id=sid))
                elif dependency.get('unresolved'):complete=False
        elif kind in ('fragment','structured_fragment'):
            primary=value;quote=value['exact_text'];sid=row['source_id']
        else:
            primary=dict(locator=row['locator']);quote=row['body'];sid=row['source_id']
        locator=primary['locator']
        # A list item needs its lead-in and related list items, not arbitrary UUID order.
        list_item=lambda t:bool(re.match(r'^\s*(?:[-–—]|[а-яa-z]\)|\d+[.)])\s',t))
        if re.fullmatch(r'p\d+',locator):
            number=int(locator[1:]);lead=number
            # Adjacent clauses in the same heading can define font/size or
            # conditions separately. Include relevant neighbours, not all pages.
            heading=primary.get('structure',{}).get('heading_path',[])
            roots=set(plan['terms'])
            for n in range(max(1,number-2),number+3):
                if n==number:continue
                f=fragment(sid,'p'+str(n))
                if f and heading and f.get('structure',{}).get('heading_path',[])==heading and (roots&set(terms(f['exact_text'])) or re.match(r'^(?:Если |В случае|При этом|Исключение|Примечание)',f['exact_text'])):
                    context.append(dict(locator=f['locator'],exact_text=f['exact_text'],source_id=sid))
            if list_item(quote) or quote.rstrip().endswith(':'):
                for n in range(number-1,max(0,number-14),-1):
                    f=fragment(sid,'p'+str(n))
                    if not f:break
                    if f['exact_text'].rstrip().endswith(':'):lead=n;break
                    if not list_item(f['exact_text']):break
                    lead=n
                for n in range(lead,lead+19):
                    f=fragment(sid,'p'+str(n))
                    if not f:break
                    text=f['exact_text']
                    if n>lead and not list_item(text) and not text.startswith('Примечание') and not re.match(r'При описании|В случае|Если ',text):break
                    if n==lead+18:complete=False;break
                    if f['locator']!=locator:context.append(dict(locator=f['locator'],exact_text=text,source_id=sid))
        elif re.fullmatch(r't\d+/r\d+/c\d+',locator):
            table,tablerow,column=re.findall(r'\d+',locator)
            peers=index.execute("SELECT DISTINCT locator FROM entries WHERE source_id=? AND (locator LIKE ? OR locator=?) ORDER BY locator LIMIT 25",(sid,f't{table}/r{tablerow}/%',f't{table}/r1/c{column}')).fetchall()
            if len(peers)>24:complete=False;peers=peers[:24]
            for peer in peers:
                f=fragment(sid,peer['locator'])
                if f and f['locator']!=locator:context.append(dict(locator=f['locator'],exact_text=f['exact_text'],source_id=sid))
        unique={}
        for c in context:
            key=(c['source_id'],c['locator'],c['exact_text'])
            if key!=(source['id'],locator,quote):unique[key]=c
        context=sorted(unique.values(),key=lambda x:(x['source_id'],_order(x['locator'])))
        truncated=len(context)>16 or sum(len(x['exact_text']) for x in context)>10000
        if truncated:
            bounded=[];size=0
            for c in context[:16]:
                if size+len(c['exact_text'])>10000:break
                bounded.append(c);size+=len(c['exact_text'])
            context=bounded
        # Derived interpretation is distinct from exact source quote. No machine fields.
        return dict(record_id=row['id'],version=row['version'],kind=kind,source_id=source['id'],source_name=source['name'],source_revision=[source['id'],source['version']],
            locator=locator,quote=quote,summary=summary,context=context,context_complete=complete and not truncated,
            context_truncated=truncated,context_scope='selected_obligation',release_id=manifest['release_id'],generation_id=manifest['generation_id'],
            evidence_scope='fragment',global_absence_proven=False,unresolved=[],trust=trust,quality=quality,
            material_type='experience' if kind in ('review_case','clarification') else 'requirement' if kind in ('requirement','term_definition') else 'source_excerpt')
