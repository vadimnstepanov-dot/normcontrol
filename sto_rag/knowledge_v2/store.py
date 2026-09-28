"""Local canonical store. Authorization belongs to the portal, not vector payloads.

All writes are explicit; opening this store never imports a legacy catalog.
Index readiness is reported by trusted local index builders in later stages.
"""
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid


class Conflict(ValueError):
    pass


class NotReady(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def checksum(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


KINDS = {'source_revision', 'fragment', 'requirement', 'obligation', 'dependency',
         'applicability', 'profile', 'review_proposal', 'review_case', 'clarification',
         'parse_run', 'table_structure', 'structured_fragment', 'term_definition', 'analysis_run', 'expert_card','publication_policy'}
REQUIRED = {
    'publication_policy': {'version','effective_refs','profiles','curation_digest','catalog','profile_snapshot'},
    'expert_card': {'base_ref','source_revision','card','status','actor_id','reason','action'},
    'term_definition': {'source_revision','fragment_refs','card'},
    'analysis_run': {'source_revision','run_id','extractor_version','coverage_audit'},
    'parse_run': {'source_revision', 'parser_version', 'identity', 'manifest_digest', 'artifact_key'},
    'table_structure': {'parse_ref', 'table'},
    'structured_fragment': {'parse_ref', 'source_revision', 'locator', 'exact_text', 'context_hash', 'structure'},
    'source_revision': {'sha256', 'original_key', 'parser_version'},
    'fragment': {'source_revision', 'locator', 'exact_text', 'search_text', 'context_hash'},
    'requirement': {'fragment_refs', 'modality', 'condition'},
    'obligation': {'requirement_ref', 'action', 'subject', 'object'},
    'dependency': {'from_ref', 'relation', 'target_ref', 'unresolved'},
    'applicability': {'requirement_ref', 'result', 'criteria', 'evidence'},
    'profile': {'definition','source_revision'},
    'review_proposal': {'portal_review_id', 'evidence', 'scope_id'},
    'review_case': {'proposal_ref', 'conditions', 'counterexample', 'approval_event_id'},
    'clarification': {'proposal_ref', 'conditions', 'counterexample', 'approval_event_id'},
}

SCHEMA = '''
CREATE TABLE schema_info(version INTEGER NOT NULL CHECK(version=1));
INSERT INTO schema_info VALUES(1);
CREATE TABLE sets(id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, metadata_revision INTEGER NOT NULL,
    metadata TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE records(id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
    set_id TEXT NOT NULL REFERENCES sets(id), kind TEXT NOT NULL, payload TEXT NOT NULL,
    digest TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(id,version));
CREATE INDEX records_set_kind ON records(set_id,kind);
CREATE TABLE record_links(record_id TEXT NOT NULL, record_version INTEGER NOT NULL,
    target_id TEXT NOT NULL, target_version INTEGER NOT NULL,
    PRIMARY KEY(record_id,record_version,target_id,target_version),
    FOREIGN KEY(record_id,record_version) REFERENCES records(id,version),
    FOREIGN KEY(target_id,target_version) REFERENCES records(id,version));
CREATE TABLE generations(id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES sets(id),
    embedding_space TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('building','ready','failed')),
    manifest_hash TEXT, watermark INTEGER, attestation TEXT);
CREATE TABLE releases(id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES sets(id),
    manifest TEXT NOT NULL, digest TEXT NOT NULL, generation_id TEXT NOT NULL REFERENCES generations(id),
    created REAL NOT NULL);
CREATE TABLE release_state(release_id TEXT PRIMARY KEY REFERENCES releases(id),
    state TEXT NOT NULL CHECK(state IN ('draft','ready','published','revoked')));
CREATE TABLE snapshots(id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
    digest TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY, operation TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, lease TEXT,
    lease_until REAL, cursor TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE TABLE outbox(id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    aggregate_id TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
    created REAL NOT NULL, acknowledged REAL);
CREATE TABLE inbox(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE deliveries(command_id TEXT PRIMARY KEY REFERENCES inbox(id), envelope TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','acknowledged','stale')));
CREATE TRIGGER records_immutable BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER records_no_delete BEFORE DELETE ON records BEGIN SELECT RAISE(ABORT,'referenced history'); END;
CREATE TRIGGER releases_immutable BEFORE UPDATE ON releases BEGIN SELECT RAISE(ABORT,'immutable release'); END;
CREATE TRIGGER releases_no_delete BEFORE DELETE ON releases BEGIN SELECT RAISE(ABORT,'referenced history'); END;
CREATE TRIGGER snapshots_immutable BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON snapshots BEGIN SELECT RAISE(ABORT,'referenced history'); END;
'''


class KnowledgeStore:
    def __init__(self, directory=None):
        value = directory or os.getenv('NORMCONTROL_KNOWLEDGE_DATA')
        if not value:
            raise ValueError('NORMCONTROL_KNOWLEDGE_DATA must explicitly identify a separate v2 directory')
        self.directory = Path(value).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'knowledge-v2.sqlite3'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'schema_info' not in tables:
                raise Conflict('Refusing a database not owned by knowledge-v2')
            if not tables:
                # executescript implicitly commits; keep the schema check and DDL under one lock.
                statement=''
                for line in SCHEMA.splitlines(True):
                    statement+=line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement);statement=''
            if db.execute('SELECT version FROM schema_info').fetchone()[0] != 1:
                raise Conflict('Unsupported canonical schema')

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            deadline = time.monotonic() + 15
            while True:
                try:
                    db.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            db.execute('PRAGMA synchronous=FULL')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, key, kind, aggregate, payload):
        raw = encode(payload)
        old = db.execute('SELECT * FROM outbox WHERE dedupe_key=?', (key,)).fetchone()
        if old:
            if old['payload'] != raw or old['kind'] != kind or old['aggregate_id'] != aggregate:
                raise Conflict('Event key reused with different content')
            return old['id']
        eid = str(uuid.uuid4())
        db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,NULL)',
                   (eid, key, kind, aggregate, raw, checksum(payload), time.time()))
        return eid

    def register_set(self, set_id, scope_id, metadata, revision=1):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM sets WHERE id=?', (set_id,)).fetchone()
            if old:
                if (old['scope_id'], old['metadata_revision'], old['metadata']) != (scope_id, revision, encode(metadata)):
                    raise Conflict('Set identity or metadata conflict')
                return
            db.execute('INSERT INTO sets VALUES(?,?,?,?,?)', (set_id, scope_id, revision, encode(metadata), time.time()))

    def put_record(self, set_id, kind, record_id, version, payload, refs=(), _db=None):
        if kind not in KINDS or not isinstance(payload, dict) or not REQUIRED[kind] <= payload.keys():
            raise ValueError('Unsupported or incomplete typed record')
        if type(version) is not int or version < 1:
            raise ValueError('Positive record version required')
        if kind=='profile':
            from .applicability import validate_profile
            validate_profile(payload['definition'])
        if kind=='parse_run':
            key=payload['artifact_key']
            if not isinstance(key,str) or not key.startswith('structure-runs/') or '..' in key.split('/') or '\\' in key or ':' in key:
                raise ValueError('Invalid structure artifact key')
        if kind == 'applicability' and payload['result'] not in ('applicable', 'not_applicable', 'unknown'):
            raise ValueError('Invalid applicability')
        if kind == 'applicability' and payload['result'] == 'not_applicable' and not payload['evidence']:
            raise ValueError('Non-applicability needs evidence')
        if kind=='source_revision':
            if not isinstance(payload['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',payload['sha256']):raise ValueError('Source SHA-256')
            key=payload['original_key']
            if not isinstance(key,str) or key.startswith(('/', '\\')) or '..' in key.split('/') or ':' in key or '\\' in key:raise ValueError('Original storage key')
        derived=[]
        for key in {'term_definition':['source_revision','fragment_refs'],'analysis_run':['source_revision'],
                    'profile':['source_revision'],'fragment':['source_revision'],'requirement':['fragment_refs'],'obligation':['requirement_ref'],
                    'applicability':['requirement_ref'],'dependency':['from_ref'],'review_case':['proposal_ref'],
                    'clarification':['proposal_ref'], 'parse_run':['source_revision'],
                    'table_structure':['parse_ref'], 'structured_fragment':['parse_ref','source_revision']}.get(kind,[]):
            value=payload[key]
            derived.extend(value if key=='fragment_refs' else [value])
        if kind=='dependency' and not payload['unresolved']:derived.append(payload['target_ref'])
        for ref in [*refs,*derived]:
            if not isinstance(ref,(list,tuple)) or len(ref)!=2 or not isinstance(ref[0],str) or type(ref[1]) is not int or ref[1]<1:
                raise ValueError('Record reference must be [id, version]')
        raw=encode(payload); refs=sorted(set(tuple(r) for r in [*refs,*derived]))
        if _db is None:
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                return self.put_record(set_id,kind,record_id,version,payload,refs,_db=db)
        db=_db
        if db.in_transaction is False:raise ValueError('A batch write requires an active transaction')
        identity=db.execute('SELECT set_id,kind FROM records WHERE id=? LIMIT 1',(record_id,)).fetchone()
        if identity and (identity['set_id'],identity['kind'])!=(set_id,kind):raise Conflict('Version cannot change record ownership/type')
        old=db.execute('SELECT * FROM records WHERE id=? AND version=?',(record_id,version)).fetchone()
        if old:
            old_refs=[tuple(r) for r in db.execute('SELECT target_id,target_version FROM record_links WHERE record_id=? AND record_version=? ORDER BY target_id,target_version',(record_id,version))]
            if (old['set_id'],old['kind'],old['payload'],old_refs)!=(set_id,kind,raw,refs):
                raise Conflict('Immutable record/version collision')
            return old['digest']
        for target,tver in refs:
            row=db.execute('SELECT set_id FROM records WHERE id=? AND version=?',(target,tver)).fetchone()
            if not row or row['set_id']!=set_id:
                raise ValueError('References must exist in the same set; external dependencies remain explicit unresolved records')
        h=checksum(dict(set_id=set_id,kind=kind,id=record_id,version=version,payload=payload,refs=refs))
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?)',(record_id,version,set_id,kind,raw,h,time.time()))
        db.executemany('INSERT INTO record_links VALUES(?,?,?,?)',[(record_id,version,*r) for r in refs])
        return h

    def put_records_batch(self, entries):
        """Atomic source-plus-fragment insert; each entry is (set, kind, id, version, payload)."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return [self.put_record(*entry,_db=db) for entry in entries]

    def create_release(self, set_id, release_id, generation_id, embedding_space, refs, versions):
        if not embedding_space or not versions or not refs:
            raise ValueError('Nonempty release, embedding space and transform versions required')
        refs=sorted(set(tuple(r) for r in refs))
        refs_set=set(refs)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            items=[]
            for rid,version in refs:
                r=db.execute('SELECT * FROM records WHERE id=? AND version=?',(rid,version)).fetchone()
                if not r or r['set_id']!=set_id:raise ValueError('Invalid release item')
                links={tuple(x) for x in db.execute('SELECT target_id,target_version FROM record_links WHERE record_id=? AND record_version=?',(rid,version))}
                if not links <= refs_set:raise ValueError('Missing transitive context in release')
                items.append(dict(id=rid,version=version,kind=r['kind'],digest=r['digest']))
            manifest=dict(schema=1,set_id=set_id,release_id=release_id,generation_id=generation_id,
                          embedding_space=embedding_space,versions=versions,items=items)
            raw=encode(manifest);h=checksum(manifest)
            old=db.execute('SELECT digest FROM releases WHERE id=?',(release_id,)).fetchone()
            if old:
                if old['digest']!=h:raise Conflict('Immutable release collision')
                return manifest
            if db.execute('SELECT 1 FROM generations WHERE id=?',(generation_id,)).fetchone():
                raise Conflict('Generation is already allocated')
            db.execute('INSERT INTO generations VALUES(?,?,?,\'building\',NULL,NULL,NULL)',(generation_id,set_id,embedding_space))
            db.execute('INSERT INTO releases VALUES(?,?,?,?,?,?)',(release_id,set_id,raw,h,generation_id,time.time()))
            db.execute("INSERT INTO release_state VALUES(?,'draft')",(release_id,))
            return manifest

    def attest_ready(self, release_id, attestation):
        """Trusted local builder boundary. No fake index is created in this stage."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM releases WHERE id=?',(release_id,)).fetchone()
            if not r:raise KeyError(release_id)
            manifest=json.loads(r['manifest'])
            required={'canonical','fts','vector','provenance'}
            if (not isinstance(attestation,dict) or any(attestation.get(k) is not True for k in required)
                or attestation.get('manifest_hash')!=r['digest']
                or attestation.get('embedding_space')!=manifest['embedding_space']
                or attestation.get('record_count')!=len(manifest['items'])
                or type(attestation.get('watermark')) is not int or attestation['watermark']<1):
                raise NotReady('All builders must acknowledge the exact manifest and embedding space')
            state=db.execute('SELECT state FROM release_state WHERE release_id=?',(release_id,)).fetchone()[0]
            if state=='revoked':raise Conflict('Revoked release')
            old=db.execute('SELECT attestation FROM generations WHERE id=?',(r['generation_id'],)).fetchone()[0]
            if old and old!=encode(attestation):raise Conflict('Index attestation changed')
            db.execute("UPDATE generations SET state='ready',manifest_hash=?,watermark=?,attestation=? WHERE id=?",
                       (r['digest'],attestation['watermark'],encode(attestation),r['generation_id']))
            if state=='draft':db.execute("UPDATE release_state SET state='ready' WHERE release_id=?",(release_id,))
            payload=dict(kind='release.ready',release_id=release_id,set_id=r['set_id'],manifest=manifest,manifest_hash=r['digest'],attestation=attestation)
            self._event(db,'ready:'+release_id,'release.ready',r['set_id'],payload)
            return payload

    def apply_command(self, command_id, kind, payload):
        """Atomically applies a portal command and records the reply for redelivery."""
        h=checksum(dict(kind=kind,payload=payload))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM inbox WHERE id=?',(command_id,)).fetchone()
            if old:
                if old['digest']!=h:raise Conflict('Command ID reused')
                return json.loads(old['result'])
            if kind=='set.register':
                existing=db.execute('SELECT * FROM sets WHERE id=?',(payload['set_id'],)).fetchone()
                values=(payload['set_id'],payload['scope_id'],payload['metadata_revision'],encode(payload['metadata']))
                if existing and tuple(existing[k] for k in ('id','scope_id','metadata_revision','metadata'))!=values:
                    raise Conflict('Set already differs')
                if not existing:db.execute('INSERT INTO sets VALUES(?,?,?,?,?)',(*values,time.time()))
                result={'kind':'set.registered','set_id':payload['set_id']}
            elif kind=='release.publish':
                r=db.execute('SELECT r.*,s.state FROM releases r JOIN release_state s ON s.release_id=r.id WHERE r.id=?',(payload['release_id'],)).fetchone()
                if not r or r['state'] not in ('ready','published'):raise NotReady('Release not ready')
                if r['set_id']!=payload['set_id'] or r['digest']!=payload['manifest_hash']:raise Conflict('Wrong publication manifest')
                db.execute("UPDATE release_state SET state='published' WHERE release_id=?",(r['id'],))
                result={'kind':'release.published','set_id':r['set_id'],'release_id':r['id'],'manifest_hash':r['digest']}
            elif kind=='release.revoke':
                r=db.execute('SELECT r.*,s.state FROM releases r JOIN release_state s ON s.release_id=r.id WHERE r.id=?',(payload['release_id'],)).fetchone()
                if not r or r['set_id']!=payload['set_id'] or r['state'] not in ('published','revoked'):
                    raise NotReady('Release is not published')
                if not isinstance(payload.get('reason'),str) or not payload['reason'].strip():raise ValueError('Revocation reason')
                db.execute("UPDATE release_state SET state='revoked' WHERE release_id=?",(r['id'],))
                result={'kind':'release.revoked','set_id':r['set_id'],'release_id':r['id'],'reason':payload['reason']}
            else:raise ValueError('Unsupported command')
            db.execute('INSERT INTO inbox VALUES(?,?,?,?)',(command_id,h,encode(result),time.time()))
            self._event(db,'reply:'+command_id,result['kind'],result['set_id'],result)
            return result

    def command_result(self, command_id, kind, payload):
        """Return a durable result for a redelivered command, if present."""
        with self.connection() as db:
            old=db.execute('SELECT digest,result FROM inbox WHERE id=?',(command_id,)).fetchone()
            if not old:return None
            if old['digest']!=checksum(dict(kind=kind,payload=payload)):
                raise Conflict('Command ID reused with another payload')
            return json.loads(old['result'])

    def remember_result(self, command_id, kind, payload, result):
        """Make external ingestion reply durable before delivery to the portal."""
        digest=checksum(dict(kind=kind,payload=payload))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT digest,result FROM inbox WHERE id=?',(command_id,)).fetchone()
            if old:
                if old['digest']!=digest or old['result']!=encode(result):raise Conflict('Changed ingestion reply')
                return json.loads(old['result'])
            db.execute('INSERT INTO inbox VALUES(?,?,?,?)',(command_id,digest,encode(result),time.time()))
            self._event(db,'reply:'+command_id,result['kind'],result['set_id'],result)
            return result

    def pin_snapshot(self, snapshot_id, job_id, release_ids, versions, authorize):
        if not release_ids or not versions:raise NotReady('No normative release selected')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            selected=[]
            for rid in sorted(set(release_ids)):
                r=db.execute('SELECT r.*,s.state FROM releases r JOIN release_state s ON r.id=s.release_id WHERE r.id=?',(rid,)).fetchone()
                if not r or r['state']!='published':raise NotReady('Release unavailable')
                if not authorize(r['set_id']):raise PermissionError('Current read grant required')
                selected.append(dict(set_id=r['set_id'],release_id=rid,manifest_hash=r['digest'],generation_id=r['generation_id'],embedding_space=json.loads(r['manifest'])['embedding_space']))
            data=dict(schema=1,releases=selected,versions=versions)
            old=db.execute('SELECT * FROM snapshots WHERE job_id=? OR id=?',(job_id,snapshot_id)).fetchone()
            if old:
                if old['id']!=snapshot_id or old['job_id']!=job_id or old['digest']!=checksum(data):raise Conflict('Snapshot already pinned')
                return json.loads(old['data'])
            db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)',(snapshot_id,job_id,encode(data),checksum(data),time.time()))
            return data

    def read_snapshot(self, snapshot_id, authorize, *, require_active=False):
        with self.connection() as db:
            r=db.execute('SELECT data,digest FROM snapshots WHERE id=?',(snapshot_id,)).fetchone()
            if not r:raise KeyError(snapshot_id)
            data=json.loads(r[0])
            if checksum(data)!=r['digest']:raise Conflict('Snapshot digest mismatch')
            for item in data['releases']:
                release=db.execute('SELECT r.digest,s.state FROM releases r JOIN release_state s ON s.release_id=r.id WHERE r.id=?',(item['release_id'],)).fetchone()
                if not release or (require_active and release['state']!='published'):raise NotReady('Pinned release unavailable')
                if release['digest']!=item['manifest_hash']:raise Conflict('Pinned release changed')
            if any(not authorize(x['set_id']) for x in data['releases']):raise PermissionError('Snapshot cannot override current ACL')
            return data

    def revoke_release(self, release_id, reason):
        if not reason:raise ValueError('Revocation reason required')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT set_id FROM releases WHERE id=?',(release_id,)).fetchone()
            if not r:raise KeyError(release_id)
            db.execute("UPDATE release_state SET state='revoked' WHERE release_id=?",(release_id,))
            self._event(db,'revoke:'+release_id,'release.revoked',r['set_id'],dict(release_id=release_id,reason=reason))

    def enqueue(self, operation, key, payload, max_attempts=3):
        if type(max_attempts) is not int or not 1<=max_attempts<=10:raise ValueError('Attempt budget')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM tasks WHERE dedupe_key=?',(key,)).fetchone()
            if old:
                if old['operation']!=operation or old['payload']!=encode(payload):raise Conflict('Task key collision')
                return old['id']
            tid=str(uuid.uuid4())
            db.execute("INSERT INTO tasks(id,operation,dedupe_key,payload,state,max_attempts,created) VALUES(?,?,?,?,'pending',?,?)",(tid,operation,key,encode(payload),max_attempts,time.time()))
            return tid

    def claim(self, now=None, ttl=60, operation=None, task_id=None):
        now=time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE tasks SET state=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'pending' END,lease=NULL,lease_until=NULL WHERE state='running' AND lease_until<=?",(now,))
            if task_id is not None:
                r=db.execute("SELECT * FROM tasks WHERE id=? AND state='pending' AND attempts<max_attempts AND (? IS NULL OR operation=?)",(task_id,operation,operation)).fetchone()
            elif operation is None:
                r=db.execute("SELECT * FROM tasks WHERE state='pending' AND attempts<max_attempts ORDER BY created,id LIMIT 1").fetchone()
            else:
                r=db.execute("SELECT * FROM tasks WHERE state='pending' AND attempts<max_attempts AND operation=? ORDER BY created,id LIMIT 1",(operation,)).fetchone()
            if not r:return None
            lease=str(uuid.uuid4())
            db.execute("UPDATE tasks SET state='running',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",(lease,now+ttl,r['id']))
            return dict(id=r['id'],operation=r['operation'],payload=json.loads(r['payload']),cursor=json.loads(r['cursor']),lease=lease)

    def checkpoint(self, task_id, lease, cursor, done=False):
        with self.connection() as db:
            n=db.execute("UPDATE tasks SET cursor=?,state=?,lease_until=? WHERE id=? AND lease=? AND state='running' AND lease_until>?",
                         (encode(cursor),'done' if done else 'running',time.time()+60,task_id,lease,time.time())).rowcount
            if n!=1:raise Conflict('Expired or stale task lease')

    def fail_task(self, task_id, lease, reason, permanent=False):
        if not isinstance(reason,str) or not reason:raise ValueError('Failure reason required')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT attempts,max_attempts FROM tasks WHERE id=? AND lease=? AND state='running' AND lease_until>?",
                           (task_id,lease,time.time())).fetchone()
            if not row:raise Conflict('Expired or stale task lease')
            state='failed' if permanent or row['attempts']>=row['max_attempts'] else 'pending'
            db.execute('UPDATE tasks SET state=?,error=?,lease=NULL,lease_until=NULL WHERE id=?',
                       (state,reason[:1000],task_id))
            return state

    def pending_events(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE acknowledged IS NULL ORDER BY created,id')]

    def save_delivery(self, command_id, envelope):
        with self.connection() as db:
            db.execute("INSERT INTO deliveries VALUES(?,?,'pending') ON CONFLICT(command_id) DO UPDATE SET envelope=excluded.envelope,state='pending'",
                       (command_id,encode(envelope)))

    def pending_delivery(self):
        with self.connection() as db:
            r=db.execute("SELECT envelope FROM deliveries WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
            return json.loads(r[0]) if r else None

    def finish_delivery(self, command_id, acknowledged=True):
        with self.connection() as db:
            db.execute('UPDATE deliveries SET state=? WHERE command_id=?',('acknowledged' if acknowledged else 'stale',command_id))
            if acknowledged:
                db.execute('UPDATE outbox SET acknowledged=? WHERE dedupe_key=?',(time.time(),'reply:'+command_id))

    def acknowledge(self, event_id, payload_hash):
        with self.connection() as db:
            n=db.execute('UPDATE outbox SET acknowledged=? WHERE id=? AND payload_hash=?',(time.time(),event_id,payload_hash)).rowcount
            if n!=1:raise Conflict('Invalid acknowledgment')

    def counts(self):
        with self.connection() as db:
            return {name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('sets','records','releases','snapshots','tasks','outbox')}

    def backup(self, destination):
        destination=Path(destination).resolve()
        if destination.exists():raise Conflict('Backup destination must be new')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as source:
            target=sqlite3.connect(destination)
            try:source.backup(target)
            finally:target.close()
