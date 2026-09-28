import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from knowledge_v2.embedding import exact_chunks
from knowledge_v2.search import GenerationBuilder, HybridSearch, QdrantIndex
from knowledge_v2.store import KnowledgeStore, checksum, Conflict, NotReady


class FakeEncoder:
    def __init__(self):
        self.space=SimpleNamespace(id='test-space',dimension=3,max_tokens=50,
                                   query_prefix='',passage_prefix='')
    def token_count(self,text):return len(text.split())+2
    def encode(self,texts,*,query=False):
        return [[float('ошиб' in t.casefold()),float('резерв' in t.casefold()),1.] for t in texts]


class FakeVectors:
    def __init__(self):self.data={};self.dimension=None
    def prepare(self,generation_id,dimension):self.dimension=dimension
    def upsert(self,generation_id,points):
        for point in points:self.data[(generation_id,point['id'])]=point
    def count(self,generation_id,*,set_id,release_id):
        return sum(p['payload']['set_id']==set_id and p['payload']['release_id']==release_id
                   for (g,_),p in self.data.items() if g==generation_id)
    def search(self,generation_id,vector,*,set_id,release_id,kinds,profiles,applicability,limit,allowed_ids=None):
        rows=[]
        for (g,eid),point in self.data.items():
            p=point['payload']
            if allowed_ids is not None and eid not in allowed_ids:continue
            if (g!=generation_id or p['set_id']!=set_id or p['release_id']!=release_id or
                p['kind'] not in kinds or p['applicability'] not in applicability or
                (profiles is not None and set(p['profile_ids']).isdisjoint(profiles))):
                continue
            score=sum(a*b for a,b in zip(vector,point['vector']))
            rows.append((score,eid))
        return [eid for _,eid in sorted(rows,reverse=True)[:limit]]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=KnowledgeStore(self.tmp.name);self.encoder=FakeEncoder();self.vector=FakeVectors()
        self.allowed={'a','b'}
        self.search=HybridSearch(self.store,self.encoder,self.vector,lambda sid:sid in self.allowed)

    def release(self,set_id,text,*,locator='7.2',profile='sto-oit',release_id=None):
        release_id=release_id or 'release-'+set_id
        ids={name:f'{set_id}-{name}' for name in ('source','fragment','parent','requirement','obligation')}
        self.store.register_set(set_id,'private-'+set_id,{'name':set_id})
        self.store.put_record(set_id,'source_revision',ids['source'],1,
                              {'sha256':('a' if set_id=='a' else 'b')*64,
                               'original_key':f'originals/{set_id}','parser_version':'test'})
        self.store.put_record(set_id,'fragment',ids['parent'],1,
                              {'source_revision':[ids['source'],1],'locator':'7',
                               'exact_text':'Требования к обмену','search_text':'требования к обмену',
                               'context_hash':'parent'})
        self.store.put_record(set_id,'fragment',ids['fragment'],1,
                              {'source_revision':[ids['source'],1],'locator':locator,
                               'exact_text':text,'search_text':text.casefold(),
                               'context_hash':'fragment'})
        card={'locator':locator,'effective_profile_id':profile,'citations':[{'locator':locator,'quote':text}],
              'dependencies':[{'relation':'parent','required':True,'target':'7','unresolved':False}]}
        self.store.put_record(set_id,'requirement',ids['requirement'],1,
                              {'fragment_refs':[[ids['fragment'],1],[ids['parent'],1]],
                               'modality':'mandatory','condition':{},'card':card})
        self.store.put_record(set_id,'obligation',ids['obligation'],1,
                              {'requirement_ref':[ids['requirement'],1],'subject':'Система',
                               'action':'контролировать','object':'ошибки обмена',
                               'citation':{'locator':locator,'quote':text}})
        refs=[(v,1) for v in ids.values()]
        manifest=self.store.create_release(set_id,release_id,'generation-'+release_id,
                                           'test-space',refs,{'parser':'test'})
        GenerationBuilder(self.store,self.encoder,self.vector).build(release_id)
        self.store.attest_ready(release_id,{'canonical':True,'fts':True,'vector':True,'provenance':True,
                 'manifest_hash':checksum(manifest),'embedding_space':'test-space',
                 'record_count':len(manifest['items']),'watermark':1})
        self.store.apply_command('publish-'+release_id,'release.publish',
             {'set_id':set_id,'release_id':release_id,'manifest_hash':checksum(manifest)})
        return release_id,ids

    def test_exact_clause_and_parent_context(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        rows=self.search.reference(release,'7.2',kinds=('requirement',),profiles=['sto-oit'])
        self.assertEqual(rows[0]['record_id'],ids['requirement'])
        self.assertEqual({x['locator'] for x in rows[0]['context']},{'7','7.2'})
        self.assertFalse(rows[0]['global_absence_proven'])

    def test_exact_locator_precedes_semantic_candidates_when_not_in_vector_topk(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        original=self.vector.search
        def omit_exact(*args,**kwargs):
            found=original(*args,**kwargs)
            return [eid for eid in found if self.vector.data[(args[0],eid)]['payload']['kind']!='requirement']
        self.vector.search=omit_exact
        rows=self.search.reference(release,'7.2',limit=20)
        self.assertEqual(rows[0]['locator'],'7.2')

    def test_multiple_profile_memberships_keep_fts_row_linked_to_content(self):
        from unittest.mock import patch
        import knowledge_v2.search as search_module
        original=search_module._entries
        def with_profiles(*args):
            for entry in original(*args):
                entry['profile_ids']=['one','two','three']
                yield entry
        with patch.object(search_module,'_entries',with_profiles):
            release,_=self.release('a','Контроль ошибок обмена обязателен.')
        path=Path(self.tmp.name)/'indexes'/('generation-'+release+'.sqlite3')
        with closing(sqlite3.connect(path)) as db:
            content={r[0] for r in db.execute("SELECT id FROM entries WHERE raw_text LIKE '%ошиб%'")}
            lexical={r[0] for r in db.execute("SELECT e.id FROM search_fts f JOIN entries e ON e.rowid=f.rowid WHERE search_fts MATCH 'ошибок OR ошибки'")}
        self.assertEqual(lexical,content)

    def test_existing_generation_fts_repair_does_not_reembed_or_change_release(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        path=Path(self.tmp.name)/'indexes'/('generation-'+release+'.sqlite3')
        with closing(sqlite3.connect(path)) as db:
            db.execute("DELETE FROM metadata WHERE key='lexical_version'")
            db.execute("INSERT INTO search_fts(search_fts) VALUES('delete-all')");db.commit()
        self.encoder.encode=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('Unexpected embedding'))
        GenerationBuilder(self.store,self.encoder,self.vector).build(release)
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO search_fts(search_fts,rank) VALUES('integrity-check',1)")
            self.assertTrue(db.execute("SELECT rowid FROM search_fts WHERE search_fts MATCH 'ошибок'").fetchall())

    def test_filters_before_limit_and_reauthorize(self):
        a,_=self.release('a','Контроль ошибок обмена обязателен.')
        b,_=self.release('b','Контроль ошибок обмена обязателен.')
        self.assertEqual(len(self.search.reference(a,'ошибки',limit=1)),1)
        self.allowed.remove('a')
        with self.assertRaises(PermissionError):self.search.reference(a,'ошибки')
        rows=self.search.reference(b,'ошибки')
        self.assertTrue(rows)
        self.assertTrue(all(x['record_id'].startswith('b-') for x in rows))

    def test_revoked_after_vector_search_blocks_citation(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        original=self.vector.search
        def revoke(*args,**kwargs):
            rows=original(*args,**kwargs)
            self.allowed.remove('a')
            return rows
        self.vector.search=revoke
        with self.assertRaises(PermissionError):self.search.reference(release,'ошибки')

    def test_profile_filter_excludes_unrelated_fragments_before_limit(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.',profile='sto-oit')
        self.assertEqual(self.search.reference(release,'ошибки',profiles=['sto-chtz']),[])

    def test_corrupted_index_cannot_forge_canonical_quote(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        path=Path(self.tmp.name)/'indexes'/('generation-'+release+'.sqlite3')
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE entries SET raw_text='Поддельная цитата' WHERE kind='requirement'")
            db.commit()
        with self.assertRaises(Conflict):self.search.reference(release,'7.2',kinds=('requirement',))

    def test_full_obligation_ledger_not_top_k(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        ledger=self.search.obligation_ledger(release,profile_ids=['sto-oit'])
        self.assertEqual([x['id'] for x in ledger],[ids['obligation']])
        self.assertEqual(self.search.obligation_ledger(release,profile_ids=['sto-chtz']),[])

    def test_no_old_catalog_fallback_and_space_isolation(self):
        (Path(self.tmp.name)/'catalog-current.json').write_text('{"old":true}')
        with self.assertRaises(KeyError):self.search.reference('no-release','ошибки')
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        self.search.encoder.space.id='other-space'
        with self.assertRaises(Conflict):self.search.reference(release,'ошибки')

    def test_revalidate_quote_after_indexing(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        self.vector.data.clear()
        with self.assertRaises(NotReady):GenerationBuilder(self.store,self.encoder,self.vector).build(release)

    def test_background_generation_is_idempotent_and_scoped(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        builder=GenerationBuilder(self.store,self.encoder,self.vector)
        task=builder.enqueue(release)
        self.assertEqual(task,builder.enqueue(release))
        self.assertTrue(builder.work_once())
        self.assertFalse(builder.work_once())
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?',(task,)).fetchone()[0],'done')

    def test_qdrant_filter_is_in_query_not_after_topk(self):
        client=QdrantIndex();sent={}
        def request(method,path,body):
            sent.update(body);return {'result':{'points':[]}}
        client._request=request
        client.search('generation',[1.,0.,0.],set_id='a',release_id='r',
                      kinds=('requirement',),profiles=['sto-oit'],applicability=('applicable',),limit=2)
        keys={x['key'] for x in sent['filter']['must']}
        self.assertEqual(keys,{'set_id','release_id','generation_id','kind','profile_ids','applicability'})
        self.assertEqual(sent['limit'],2)
        self.assertEqual(sent['params'],{'exact':True})

    def test_warm_manifest_cache_does_not_bypass_revocation_or_tampering(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        self.search.reference(release,'ошибки')
        self.store.revoke_release(release,'test')
        with self.assertRaises(NotReady):self.search.reference(release,'ошибки')
        with self.store.connection() as db:
            db.execute("UPDATE release_state SET state='published' WHERE release_id=?",(release,))
            # Simulate storage corruption past the immutable-store boundary.
            db.execute('DROP TRIGGER releases_immutable')
            db.execute("UPDATE releases SET manifest=replace(manifest,'test-space','other-space') WHERE id=?",(release,))
        with self.assertRaises(Conflict):self.search.reference(release,'ошибки')

    def test_lossless_chunking(self):
        text='Первый длинный раздел. Второй длинный раздел. Третий длинный раздел.'
        chunks=exact_chunks(text,lambda s:len(s)+2,30)
        self.assertEqual(''.join(x[2] for x in chunks),text)
        self.assertGreater(len(chunks),1)


if __name__=='__main__':unittest.main()
