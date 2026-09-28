"""Generation-pinned FTS/Qdrant search over the canonical v2 knowledge store.

Indexes are disposable derivatives. Every result is resolved from an immutable
release manifest and is re-authorized before its exact source is returned.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import tempfile
import urllib.request
import urllib.error
import uuid

from .embedding import exact_chunks
from .store import Conflict, NotReady, checksum


INDEX_KINDS = {'fragment', 'structured_fragment', 'requirement', 'term_definition', 'obligation', 'review_case', 'clarification'}
LEXICAL_VERSION = '2'
SCHEMA = '''
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE entries(id TEXT PRIMARY KEY,record_id TEXT NOT NULL,version INTEGER NOT NULL,
  set_id TEXT NOT NULL,release_id TEXT NOT NULL,generation_id TEXT NOT NULL,
  kind TEXT NOT NULL,locator TEXT NOT NULL,designation TEXT NOT NULL,
  profile_id TEXT NOT NULL,applicability TEXT NOT NULL,
  chunk_start INTEGER NOT NULL,chunk_end INTEGER NOT NULL,raw_text TEXT NOT NULL,
  normalized TEXT NOT NULL);
CREATE INDEX entries_filter ON entries(set_id,release_id,generation_id,kind,profile_id,applicability);
CREATE INDEX entries_exact ON entries(set_id,release_id,generation_id,locator,designation);
CREATE TABLE entry_profiles(entry_id TEXT NOT NULL,profile_id TEXT NOT NULL,
  PRIMARY KEY(entry_id,profile_id));
CREATE INDEX entry_profiles_filter ON entry_profiles(profile_id,entry_id);
CREATE TABLE aliases(entry_id TEXT NOT NULL,alias TEXT NOT NULL,PRIMARY KEY(entry_id,alias));
CREATE INDEX aliases_lookup ON aliases(alias,entry_id);
CREATE VIRTUAL TABLE search_fts USING fts5(raw_text,normalized,content='entries',content_rowid='rowid',tokenize='unicode61');
'''


def normalize(text):
    return re.sub(r'\s+', ' ', text.casefold().replace('ё', 'е')).strip()


def _terms(query):
    # Quoted tokens avoid FTS operators in untrusted input, including a clause number.
    return [t for t in re.findall(r'[^\W_]+', normalize(query)) if len(t) > 1][:16]


def _aliases(entry):
    text=entry['raw_text'][:300]
    result={normalize(entry['locator']),normalize(entry['designation'])}
    result.update(normalize(x) for x in re.findall(
        r'\b(?:СТО\s+РЖД|ГОСТ\s+Р|ГОСТ)\s+[\w.\-/]+',text,re.I))
    number=re.match(r'^\s*(\d+(?:\.\d+){1,6})\b',text)
    if number:result.add(number.group(1))
    return sorted(x for x in result if x)


def _manifest(store, release_id, cache=None):
    with store.connection() as db:
        row = db.execute('SELECT r.*,s.state FROM releases r JOIN release_state s ON s.release_id=r.id WHERE r.id=?',
                         (release_id,)).fetchone()
        if not row:
            raise KeyError(release_id)
        cached=cache.get(release_id) if cache is not None else None
        # Only immutable, byte-identical manifest content is reused. State,
        # generation and authorization are read again on every request.
        if cached and cached[:2] == (row['digest'],row['manifest']):
            manifest=cached[2]
        else:
            manifest = json.loads(row['manifest'])
            if checksum(manifest) != row['digest']:
                raise Conflict('Release manifest digest mismatch')
            if cache is not None:
                cache.clear()  # bound memory to one potentially large release
                cache[release_id]=(row['digest'],row['manifest'],manifest)
        generation = db.execute('SELECT * FROM generations WHERE id=?',(manifest['generation_id'],)).fetchone()
        if not generation or generation['embedding_space'] != manifest['embedding_space']:
            raise Conflict('Generation/embedding space mismatch')
        return manifest, row['digest'], row['state'], dict(generation)


def _record_text(kind, payload):
    if kind in ('fragment','structured_fragment'):
        return payload['exact_text'], payload.get('locator',''), ''
    if kind in ('requirement','term_definition'):
        card = payload.get('card', {})
        citations = card.get('citations', [])
        primary = next((c.get('quote','') for c in citations if c.get('locator')==card.get('locator')), '')
        return (primary.strip() or
                ' '.join(str(x) for x in card.get('obligations', []))), card.get('locator',''), card.get('effective_profile_id','')
    if kind == 'obligation':
        return ' '.join(str(payload.get(k,'')) for k in ('subject','action','object')), payload.get('citation',{}).get('locator',''), ''
    return ' '.join(str(payload.get(k,'')) for k in ('conditions','counterexample','summary')), payload.get('locator',''), ''


def _entries(store, manifest, encoder):
    refs = {(item['id'], item['version']): item for item in manifest['items']}
    with store.connection() as db:
        policy=next((i for i in manifest['items'] if i['kind']=='publication_policy'),None)
        effective=None
        if policy:
            material=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',(policy['id'],policy['version'])).fetchone()[0])
            effective=set(map(tuple,material['effective_refs']))
        fragment_profiles={}
        for (rid,version),item in refs.items():
            if item['kind'] not in ('requirement','term_definition'):continue
            if effective is not None and (rid,version) not in effective:continue
            req=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',(rid,version)).fetchone()[0])
            card=req.get('card',{})
            memberships=set(card.get('profile_ids') or [card.get('effective_profile_id','')])-{'',None}
            for ref in req['fragment_refs']:
                fragment_profiles.setdefault(tuple(ref),set()).update(memberships)
        for (rid, version), item in refs.items():
            kind = item['kind']
            if kind not in INDEX_KINDS:
                continue
            row = db.execute('SELECT * FROM records WHERE id=? AND version=?',(rid,version)).fetchone()
            if not row or row['set_id'] != manifest['set_id'] or row['digest'] != item['digest']:
                raise Conflict('Canonical record differs from release')
            payload = json.loads(row['payload'])
            if effective is not None:
                if kind in ('requirement','term_definition') and (rid,version) not in effective:continue
                if kind=='obligation' and tuple(payload['requirement_ref']) not in effective:continue
            raw, locator, profile_id = _record_text(kind,payload)
            if not raw.strip():
                continue
            if kind in ('requirement','term_definition'):
                applicability=payload.get('card',{}).get('validation',{}).get('applicability',{}).get('result','unknown')
                profile_ids=payload.get('card',{}).get('profile_ids') or [profile_id]
            elif kind=='obligation':
                parent=tuple(payload['requirement_ref'])
                if parent not in refs:raise Conflict('Obligation parent outside release')
                parent_row=db.execute('SELECT payload FROM records WHERE id=? AND version=?',parent).fetchone()
                parent_card=json.loads(parent_row[0]).get('card',{})
                profile_id=parent_card.get('effective_profile_id','')
                profile_ids=parent_card.get('profile_ids') or [profile_id]
                applicability=parent_card.get('validation',{}).get('applicability',{}).get('result','unknown')
            else:
                applicability='applicable'
                profile_ids=sorted(fragment_profiles.get((rid,version),set())) if kind in ('fragment','structured_fragment') else []
            if applicability not in ('applicable','not_applicable','unknown'):
                raise Conflict('Invalid applicability state')
            designation = payload.get('designation','') or locator
            for start,end,piece in exact_chunks(raw,encoder.token_count,
                                                  min(encoder.space.max_tokens,1024),
                                                  encoder.space.passage_prefix):
                eid = str(uuid.uuid5(uuid.NAMESPACE_URL,f"{manifest['generation_id']}:{rid}:{version}:{start}:{end}"))
                yield dict(id=eid,record_id=rid,version=version,set_id=manifest['set_id'],
                           release_id=manifest['release_id'],generation_id=manifest['generation_id'],
                           kind=kind,locator=locator,designation=designation,profile_id=profile_id,
                           profile_ids=sorted(set(x for x in profile_ids if x)),
                           applicability=applicability,chunk_start=start,chunk_end=end,
                           raw_text=piece,normalized=normalize(piece))


class QdrantIndex:
    """Local Qdrant REST backend. No silent embedded or remote fallback."""
    def __init__(self, url='http://127.0.0.1:6333', *, exact_search=None):
        if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost|qdrant)(?::\d+)?',url.rstrip('/')):
            raise ValueError('Qdrant must be local or on the private Compose network')
        self.url = url.rstrip('/')
        self.exact_search=(os.getenv('KNOWLEDGE_QDRANT_EXACT_SEARCH','1')=='1'
                           if exact_search is None else bool(exact_search))

    def _request(self, method, path, body=None):
        raw = None if body is None else json.dumps(body,ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.url+path,data=raw,method=method,
                                     headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:
            return json.load(response)

    @staticmethod
    def collection(generation_id):
        return 'norm_v2_' + re.sub('[^a-zA-Z0-9_]', '_', generation_id)

    def prepare(self, generation_id, dimension):
        name=self.collection(generation_id)
        try:
            info=self._request('GET',f'/collections/{name}')
        except urllib.error.HTTPError as exc:
            if exc.code!=404:raise
            self._request('PUT',f'/collections/{name}',{'vectors':{'size':dimension,'distance':'Cosine'}})
        else:
            vectors=info['result']['config']['params']['vectors']
            if vectors['size']!=dimension or vectors['distance'].lower()!='cosine':
                raise Conflict('Qdrant collection has another embedding dimension')
        for field in ('set_id','release_id','generation_id','kind','profile_ids','applicability'):
            self._request('PUT',f'/collections/{name}/index',{'field_name':field,'field_schema':'keyword'})

    def upsert(self, generation_id, points):
        if points:
            self._request('PUT',f'/collections/{self.collection(generation_id)}/points?wait=true',
                          {'points':points})

    def search(self,generation_id,vector,*,set_id,release_id,kinds,profiles,applicability,limit,allowed_ids=None):
        must=[{'key':key,'match':{'value':value}} for key,value in
              (('set_id',set_id),('release_id',release_id),('generation_id',generation_id))]
        must.append({'key':'kind','match':{'any':sorted(kinds)}})
        must.append({'key':'applicability','match':{'any':sorted(applicability)}})
        if profiles is not None:
            must.append({'key':'profile_ids','match':{'any':sorted(set(profiles))}})
        if allowed_ids is not None:
            if not allowed_ids:return []
            must.append({'has_id':allowed_ids})
        response=self._request('POST',f'/collections/{self.collection(generation_id)}/points/query',
                               {'query':vector,'filter':{'must':must},'limit':limit,'with_payload':False,
                                'params':{'exact':self.exact_search}})
        return [row['id'] for row in response['result']['points']]

    def count(self,generation_id,*,set_id,release_id):
        result=self._request('POST',f'/collections/{self.collection(generation_id)}/points/count',
                             {'filter':{'must':[{'key':k,'match':{'value':v}} for k,v in
                                (('set_id',set_id),('release_id',release_id),('generation_id',generation_id))]},
                              'exact':True})
        return result['result']['count']


class GenerationBuilder:
    def __init__(self,store,encoder,vector):
        self.store,self.encoder,self.vector=store,encoder,vector

    def enqueue(self,release_id):
        manifest,_,_,_=_manifest(self.store,release_id)
        if manifest['embedding_space']!=self.encoder.space.id:
            raise Conflict('Requested generation uses another embedding space')
        return self.store.enqueue('generation.build','generation:'+manifest['generation_id'],
                                  {'release_id':release_id,'embedding_space':self.encoder.space.id},
                                  max_attempts=3)

    def work_once(self):
        """Build in the background queue; keep lease alive during long CPU work."""
        task=self.store.claim(operation='generation.build',ttl=90)
        if task is None:return False
        stop=threading.Event();errors=[]
        def heartbeat():
            while not stop.wait(20):
                try:self.store.checkpoint(task['id'],task['lease'],
                                          {'release_id':task['payload']['release_id'],'phase':'index'})
                except Exception as exc:errors.append(exc);return
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        try:
            if task['payload']['embedding_space']!=self.encoder.space.id:
                raise Conflict('Worker embedding space changed')
            self.build(task['payload']['release_id'])
            if errors:raise Conflict('Index build lease lost')
            self.store.checkpoint(task['id'],task['lease'],{'phase':'built'},done=True)
            return True
        except Exception as exc:
            try:self.store.fail_task(task['id'],task['lease'],type(exc).__name__+': '+str(exc),
                                     permanent=isinstance(exc,(Conflict,ValueError)))
            except Conflict:pass
            raise
        finally:
            stop.set();thread.join(timeout=2)

    def build(self,release_id,*,batch_size=16):
        manifest,digest,state,generation=_manifest(self.store,release_id)
        if manifest['embedding_space']!=self.encoder.space.id:
            raise Conflict('Embedding space changed')
        directory=self.store.directory/'indexes'
        directory.mkdir(exist_ok=True)
        target=directory/(manifest['generation_id']+'.sqlite3')
        if target.exists():
            with closing(sqlite3.connect(target)) as db:
                metadata=dict(db.execute('SELECT key,value FROM metadata'))
                if metadata.get('manifest_hash')!=digest or metadata.get('embedding_space')!=self.encoder.space.id:
                    raise Conflict('Generation file already belongs to another manifest')
                expected=int(metadata['entry_count'])
            if self.vector.count(manifest['generation_id'],set_id=manifest['set_id'],release_id=release_id)!=expected:
                raise NotReady('Vector generation count differs from FTS')
            if metadata.get('lexical_version')!=LEXICAL_VERSION:
                repair_lexical_index(target,digest,self.encoder.space.id)
            return target
        tmp=target.with_suffix('.building')
        if tmp.exists():
            tmp.unlink()  # disposable interrupted index, never canonical data
        db=sqlite3.connect(tmp)
        try:
            db.executescript(SCHEMA)
            self.vector.prepare(manifest['generation_id'],self.encoder.space.dimension)
            batch=[];total=0
            def flush():
                nonlocal total
                if not batch:return
                vectors=self.encoder.encode([x['raw_text'] for x in batch])
                points=[]
                for entry,vec in zip(batch,vectors):
                    columns=('id','record_id','version','set_id','release_id','generation_id','kind',
                             'locator','designation','profile_id','applicability','chunk_start',
                             'chunk_end','raw_text','normalized')
                    values=tuple(entry[key] for key in columns)
                    db.execute('INSERT INTO entries VALUES('+','.join('?' for _ in values)+')',values)
                    # Profile membership inserts have their own rowids. Capture
                    # the content row immediately, before touching another table.
                    rowid=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    db.executemany('INSERT INTO entry_profiles VALUES(?,?)',
                                   [(entry['id'],pid) for pid in entry['profile_ids']])
                    db.execute('INSERT INTO search_fts(rowid,raw_text,normalized) VALUES(?,?,?)',
                               (rowid,entry['raw_text'],entry['normalized']))
                    db.executemany('INSERT INTO aliases VALUES(?,?)',[(entry['id'],x) for x in _aliases(entry)])
                    points.append({'id':entry['id'],'vector':vec,
                                   'payload':{key:entry[key] for key in ('set_id','release_id','generation_id','kind','profile_ids','applicability')}})
                self.vector.upsert(manifest['generation_id'],points)
                total+=len(batch);batch.clear()
            for entry in _entries(self.store,manifest,self.encoder):
                batch.append(entry)
                if len(batch)>=batch_size:flush()
            flush()
            if total==0:raise NotReady('No searchable content in release')
            db.execute('INSERT INTO metadata VALUES(?,?)',('manifest_hash',digest))
            db.execute('INSERT INTO metadata VALUES(?,?)',('embedding_space',self.encoder.space.id))
            db.execute('INSERT INTO metadata VALUES(?,?)',('entry_count',str(total)))
            db.execute('INSERT INTO metadata VALUES(?,?)',('lexical_version',LEXICAL_VERSION))
            db.commit()
            actual=self.vector.count(manifest['generation_id'],set_id=manifest['set_id'],release_id=release_id)
            if actual!=total:raise NotReady(f'Qdrant count {actual} != FTS count {total}')
        finally:
            db.close()
        os.replace(tmp,target)
        return target


def repair_lexical_index(path,manifest_hash,embedding_space):
    """Atomically repair disposable FTS row links without re-embedding or new norms.

    Readers keep the old inode until they finish. A failed repair never replaces
    the active index; the canonical release, entries and vector IDs are unchanged.
    """
    path=Path(path)
    handle=tempfile.NamedTemporaryFile(dir=path.parent,prefix=path.name+'.repair-',delete=False)
    temporary=Path(handle.name);handle.close()
    try:
        with closing(sqlite3.connect(path)) as source,closing(sqlite3.connect(temporary)) as dest:
            meta=dict(source.execute('SELECT key,value FROM metadata'))
            if meta.get('manifest_hash')!=manifest_hash or meta.get('embedding_space')!=embedding_space:
                raise Conflict('Cannot repair another generation')
            source.backup(dest)
            dest.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")
            dest.execute("INSERT INTO search_fts(search_fts,rank) VALUES('integrity-check',1)")
            dest.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('lexical_version',LEXICAL_VERSION))
            dest.commit()
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def _index_path(store,manifest,digest):
    path=store.directory/'indexes'/(manifest['generation_id']+'.sqlite3')
    if not path.is_file():raise NotReady('FTS generation is not built')
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    meta=dict(db.execute('SELECT key,value FROM metadata'))
    if meta.get('manifest_hash')!=digest or meta.get('embedding_space')!=manifest['embedding_space']:
        db.close();raise Conflict('Index generation/manifest mismatch')
    return db


def _filter(kinds,profiles,applicability):
    if not kinds or not set(kinds)<=INDEX_KINDS or not applicability:
        raise ValueError('Explicit record kinds and applicability required')
    clauses=['e.kind IN ('+','.join('?' for _ in kinds)+')',
             'e.applicability IN ('+','.join('?' for _ in applicability)+')']
    args=list(kinds)+list(applicability)
    if profiles is not None:
        profile_values=sorted(set(profiles))
        if not profile_values:raise ValueError('Explicit profile selection cannot be empty')
        clauses.append('EXISTS (SELECT 1 FROM entry_profiles ep WHERE ep.entry_id=e.id AND ep.profile_id IN ('+
                       ','.join('?' for _ in profile_values)+'))')
        args.extend(profile_values)
    return clauses,args


class HybridSearch:
    def __init__(self,store,encoder,vector,authorize):
        if not callable(authorize):raise ValueError('Authorization callback required')
        self.store,self.encoder,self.vector,self.authorize=store,encoder,vector,authorize
        self._manifest_cache={}
        self._items_cache=None

    def _open(self,release_id,*,allow_draft=False):
        manifest,digest,state,generation=_manifest(self.store,release_id,self._manifest_cache)
        if not self.authorize(manifest['set_id']):raise PermissionError('Set read grant required')
        if state not in (('draft','ready','published') if allow_draft else ('published',)):
            raise NotReady('Release not published')
        if manifest['embedding_space']!=self.encoder.space.id:
            raise Conflict('Query and release use different embedding spaces')
        return manifest,_index_path(self.store,manifest,digest)

    def reference(self,release_id,query,*,kinds=('requirement','fragment','structured_fragment','term_definition'),profiles=None,
                  applicability=('applicable','unknown'),limit=20,allow_draft=False,eligible_refs=None):
        if not isinstance(query,str) or not query.strip() or not 1<=limit<=100:
            raise ValueError('Nonempty query and bounded top-k required')
        manifest,db=self._open(release_id,allow_draft=allow_draft)
        try:
            clauses,args=_filter(kinds,profiles,applicability)
            if eligible_refs is not None:
                if not eligible_refs:return []
                db.execute('CREATE TEMP TABLE eligible_records(id TEXT,version INTEGER,PRIMARY KEY(id,version))')
                db.executemany('INSERT OR IGNORE INTO eligible_records VALUES(?,?)',eligible_refs)
                clauses.append('EXISTS (SELECT 1 FROM eligible_records er WHERE er.id=e.record_id AND er.version=e.version)')
            scope=['e.set_id=?','e.release_id=?','e.generation_id=?']+clauses
            base=[manifest['set_id'],release_id,manifest['generation_id']]+args
            ranks=[]
            # Start from the selective alias, not every record in the release.
            # Without this ordering SQLite can scan 100k entries for a miss.
            for row in db.execute('SELECT e.id FROM aliases a INDEXED BY aliases_lookup CROSS JOIN entries e ON e.id=a.entry_id WHERE '+
                                  ' AND '.join(scope)+' AND a.alias=? LIMIT ?',
                                  (*base,normalize(query),limit*4)):
                ranks.append(row['id'])
            routes=[ranks]
            exact_ids=set(ranks)
            terms=_terms(query)
            if terms:
                match=' OR '.join('"'+t.replace('"','')+'"' for t in terms)
                lexical=[r['id'] for r in db.execute('SELECT e.id FROM search_fts f JOIN entries e ON e.rowid=f.rowid '
                    'WHERE search_fts MATCH ? AND '+' AND '.join(scope)+' ORDER BY bm25(search_fts) LIMIT ?',
                    (match,*base,limit*4))]
                routes.append(lexical)
            query_vector=self.encoder.encode([query],query=True)[0]
            vector_options={}
            if eligible_refs is not None:
                vector_options['allowed_ids']=[r['id'] for r in db.execute('SELECT e.id FROM entries e WHERE '+' AND '.join(scope),base)]
            routes.append(self.vector.search(manifest['generation_id'],query_vector,
                          set_id=manifest['set_id'],release_id=release_id,kinds=kinds,
                          profiles=profiles,applicability=applicability,limit=limit*4,**vector_options))
            scores={}
            for route in routes:
                for rank,eid in enumerate(route):scores[eid]=scores.get(eid,0)+1/(60+rank+1)
            result=[];seen=set()
            if self._items_cache is None or self._items_cache[0] is not manifest:
                self._items_cache=(manifest,{(r['id'],r['version']):r for r in manifest['items']})
            items=self._items_cache[1]
            with self.store.connection() as source:
                # A requested exact locator/designation is navigation, not just
                # another weak RRF vote. Keep all ACL/profile filters in force.
                for eid in sorted(scores,key=lambda x:(x not in exact_ids,-scores[x],x)):
                    entry=db.execute('SELECT * FROM entries e WHERE id=? AND '+' AND '.join(scope),
                                     (eid,*base)).fetchone()
                    if not entry or (entry['record_id'],entry['version']) in seen:continue
                    seen.add((entry['record_id'],entry['version']))
                    result.append(self._resolve(manifest,entry,scores[eid],items,source))
                    if len(result)>=limit:break
            return result
        finally:db.close()

    def _resolve(self,manifest,entry,score,items,source):
        # Re-check at quote load, after potentially long embedding/search calls.
        if not self.authorize(manifest['set_id']):raise PermissionError('Read grant revoked')
        key=(entry['record_id'],entry['version'])
        if key not in items:raise Conflict('Index result outside pinned manifest')
        row=source.execute('SELECT * FROM records WHERE id=? AND version=?',key).fetchone()
        if not row or row['set_id']!=manifest['set_id'] or row['digest']!=items[key]['digest']:
            raise Conflict('Quote provenance mismatch')
        payload=json.loads(row['payload'])
        context=[];unresolved=[]
        if row['kind'] in ('requirement','term_definition'):
            # Stage-4 links include the primary fragment, parent headings,
            # table context, and resolved dependency fragments.
            for ref in payload['fragment_refs']:
                ref=tuple(ref)
                if ref not in items:raise Conflict('Dependency outside pinned manifest')
                fragment=source.execute('SELECT * FROM records WHERE id=? AND version=?',ref).fetchone()
                if not fragment or fragment['digest']!=items[ref]['digest'] or fragment['kind'] not in ('fragment','structured_fragment'):
                    raise Conflict('Dependency provenance mismatch')
                fact=json.loads(fragment['payload'])
                context.append(dict(id=ref[0],locator=fact['locator'],exact_text=fact['exact_text'],
                                    context_hash=fact['context_hash']))
            unresolved=[d for d in payload.get('card',{}).get('dependencies',[]) if d.get('unresolved')]
        raw,locator,_=_record_text(row['kind'],payload)
        if raw[entry['chunk_start']:entry['chunk_end']]!=entry['raw_text']:
            raise Conflict('Index quote differs from canonical text')
        return dict(record_id=entry['record_id'],version=entry['version'],kind=row['kind'],
                    locator=locator,quote=entry['raw_text'],score=score,
                    source_revision=payload.get('source_revision'),release_id=manifest['release_id'],
                    generation_id=manifest['generation_id'],evidence_scope='fragment',
                    global_absence_proven=False,context=context,unresolved=unresolved,
                    context_complete=not unresolved)

    def obligation_ledger(self,release_id,*,profile_ids,facts=None,verify_evidence=None,allow_draft=False):
        """Full register of applicable obligations; deliberately no top-k."""
        manifest,db=self._open(release_id,allow_draft=allow_draft)
        db.close()
        if not profile_ids:raise ValueError('Document profiles required')
        refs={(x['id'],x['version']):x for x in manifest['items']}
        if not self.authorize(manifest['set_id']):raise PermissionError('Read grant revoked')
        with self.store.connection() as source:
            rows=[]
            policy=next((x for x in manifest['items'] if x['kind']=='publication_policy'),None)
            effective=None
            if policy:
                value=json.loads(source.execute('SELECT payload FROM records WHERE id=? AND version=?',(policy['id'],policy['version'])).fetchone()[0])
                effective=set(map(tuple,value['effective_refs']))
            for key,item in refs.items():
                if item['kind']!='obligation':continue
                record=source.execute('SELECT * FROM records WHERE id=? AND version=?',key).fetchone()
                if not record or record['digest']!=item['digest']:raise Conflict('Obligation provenance mismatch')
                obligation=json.loads(record['payload']); req_ref=tuple(obligation['requirement_ref'])
                if effective is not None and req_ref not in effective:continue
                if req_ref not in refs:raise Conflict('Requirement outside release')
                requirement=json.loads(source.execute('SELECT payload FROM records WHERE id=? AND version=?',req_ref).fetchone()[0])
                card=requirement.get('card',{})
                memberships=set(card.get('profile_ids') or [card.get('effective_profile_id')])
                if memberships.isdisjoint(profile_ids):continue
                applicability=card.get('validation',{}).get('applicability',{}).get('result','unknown')
                evidence=[];missing=[]
                if facts is not None:
                    from .applicability import evaluate
                    evaluation=evaluate(requirement['condition'],facts,verify_evidence)
                    applicability=evaluation['result'];evidence=evaluation['evidence'];missing=evaluation['missing']
                rows.append(dict(id=key[0],requirement_id=req_ref[0],profile_ids=sorted(memberships),
                                 applicability=applicability,applicability_evidence=evidence,
                                 applicability_missing=missing,citation=obligation.get('citation'),
                                 action=obligation['action']))
        if not self.authorize(manifest['set_id']):raise PermissionError('Read grant revoked')
        return rows
