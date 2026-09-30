import copy,json,tempfile,unittest,uuid
from pathlib import Path
from knowledge_v2.trace import plan,run,validate_trace,compile_links
from knowledge_v2.store import KnowledgeStore,Conflict

class Model:
    signature='trace-control';context=16000;output_tokens=256;timeout=30
    def __init__(self):self.calls=[];self.outcome='violated';self.claim='contradiction';self.hook=None
    def count(self,p):return len(json.dumps(p,ensure_ascii=False))//4
    def complete(self,p):
        self.calls.append(copy.deepcopy(p))
        if self.hook:self.hook(p)
        evidence=[dict(block_id=b['id'],quote=b['text']) for b in p['documents']]
        outcome='unknown' if p['stage']=='trace_collect' else self.outcome
        return dict(decisions=[dict(obligation_id=r['id'],outcome=outcome,claim='unknown' if outcome=='unknown' else self.claim,reason='Контрольный результат.',evidence=evidence) for r in p['obligations']])

def fixtures():
    docs=[dict(id='source-doc',sha256='s'*64,name='ТЗ.docx',classification={'type':'ТЗ'},gaps=[],
        blocks=[dict(id='s',document='source-doc',locator='p1',location='3.1 — абзац 1',text='RPO не более 12 часов.')]),
        dict(id='target-doc',sha256='t'*64,name='ЧТЗ.docx',classification={'type':'ЧТЗ'},gaps=[],
        blocks=[dict(id='t',document='target-doc',locator='p1',location='4.2 — абзац 1',text='RPO не более 24 часов.')])]
    facts={d['id']:{'document_type':dict(value=d['classification']['type'],evidence=[dict(source=d['id'],locator='title')])} for d in docs}
    link=dict(id=str(uuid.uuid4()),revision=1,release_id='release',set_id='set',source_type='ТЗ',target_type='ЧТЗ',
        condition={'fact':{'name':'document_type','in':['ТЗ']}},trusted=True,mandatory_target=True,requested_mandatory_target=True,
        description='Сохранить RPO.',basis_citations=[{'locator':'5.1','quote':'Ограничения ТЗ должны сохраняться в ЧТЗ.'}])
    return docs,facts,link

class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=KnowledgeStore(Path(self.tmp.name));self.model=Model();self.docs,self.facts,self.link=fixtures()
    def execute(self,links=None,callback=None,job='job'):
        return run(self.store,job,[self.link] if links is None else links,self.docs,self.facts,lambda *a:True,self.model,lambda *a:True,callback or (lambda *a:False))
    def test_reference_remains_source_and_is_not_missing_target(self):
        self.docs[0]['review_role']='approved_reference'
        b,initial=plan([self.link],self.docs,self.facts,lambda *a:True,self.model)
        self.assertEqual(len(b),1);self.assertEqual(b[0]['payload']['obligations'][0]['source_documents'],['source-doc'])
        self.link['source_type']='ЧТЗ';self.link['target_type']='ТЗ';self.link['condition']={'fact':{'name':'document_type','in':['ЧТЗ']}}
        b,initial=plan([self.link],self.docs,self.facts,lambda *a:True,self.model)
        self.assertEqual(b,[]);self.assertEqual(initial[0]['state'],'not_applicable');self.assertNotIn('absence_proof',initial[0])
    def test_proved_chain_and_weakened_constraint_have_exact_both_sides(self):
        self.model.outcome='satisfied';self.model.claim='presence'
        r=self.execute();self.assertEqual(r['rows'][0]['state'],'checked');self.assertEqual(len(r['rows'][0]['evidence']),2)
        self.model.outcome='violated';self.model.claim='contradiction';r=self.execute(job='other')
        self.assertEqual(r['counts'],{'violated':1});self.assertEqual(r['rows'][0]['evidence'][1]['location'],'4.2 — абзац 1')
    def test_unconfirmed_basis_cannot_make_confirmed_violation(self):
        self.link['trusted']=False;r=self.execute();self.assertEqual(r['rows'][0]['state'],'unknown');self.assertTrue(r['rows'][0]['preliminary_violation'])
    def test_missing_catalog_edges_never_make_package_violation(self):
        r=self.execute([]);self.assertEqual(r['rows'],[]);self.assertTrue(r['missing_catalog_links']);self.assertEqual(self.model.calls,[])
    def test_missing_mandatory_document_and_unknown_type(self):
        self.docs.pop();r=self.execute();self.assertEqual(r['rows'][0]['state'],'violated');self.assertTrue(r['rows'][0]['absence_proof']['complete'])
        self.docs.append(dict(id='unknown',sha256='a'*64,classification={'type':'unknown'},gaps=[],blocks=[]))
        self.assertEqual(self.execute(job='unknown')['rows'][0]['state'],'unknown')
    def test_pmi_optional_at_stage_requires_proof(self):
        self.link['target_type']='ПМИ';self.link['condition']={'fact':{'name':'stage','in':['acceptance']}}
        self.facts['source-doc']['stage']=dict(value='design',evidence=[dict(source='source-doc',locator='3.1')])
        r=self.execute();self.assertEqual(r['rows'][0]['state'],'not_applicable')
        del self.facts['source-doc']['stage'];self.assertEqual(self.execute(job='no-proof')['rows'][0]['state'],'unknown')
    def test_unread_area_and_partial_scope_never_prove_absence(self):
        b,_=plan([self.link],self.docs,self.facts,lambda *a:True,self.model);p=b[0]['payload']
        raw=self.model.complete(p);raw['decisions'][0].update(claim='absence')
        p['completeness']['gaps']=[{'locator':'image1'}]
        with self.assertRaises(ValueError):validate_trace(p,raw)
        p['completeness']['gaps']=[];p['completeness']['full_text']=False
        with self.assertRaises(ValueError):validate_trace(p,raw)
    def test_no_forged_or_one_sided_evidence(self):
        b,_=plan([self.link],self.docs,self.facts,lambda *a:True,self.model);p=b[0]['payload'];raw=self.model.complete(p)
        raw['decisions'][0]['evidence'].pop()
        with self.assertRaises(ValueError):validate_trace(p,raw)
        raw=self.model.complete(p);raw['decisions'][0]['evidence'][0]['quote']='Иная несуществующая цитата'
        with self.assertRaises(ValueError):validate_trace(p,raw)
    def test_pause_replay_and_current_acl(self):
        r=self.execute(callback=lambda *a:True);self.assertEqual(r['state'],'paused');self.assertFalse(self.model.calls)
        r=self.execute();calls=len(self.model.calls);r2=self.execute();self.assertEqual(len(self.model.calls),calls);self.assertEqual(r['rows'],r2['rows'])
        # Revocation during generation cannot persist a successful result.
        allowed=[True];self.model.hook=lambda p:allowed.__setitem__(0,False)
        with self.assertRaises(PermissionError):run(self.store,'revoked',[self.link],self.docs,self.facts,lambda *a:True,self.model,lambda *a:allowed[0],lambda *a:False)
    def test_exhaustive_collection_for_large_corpus_does_not_claim_absence(self):
        self.model.context=3000
        for i in range(12):self.docs[1]['blocks'].append(dict(id=f'big-{i}',document='target-doc',locator=f'p{i+2}',text=('Описание элемента '+str(i)+' ')*100))
        b,initial=plan([self.link],self.docs,self.facts,lambda *a:True,self.model)
        self.assertFalse(initial);self.assertTrue(any(x['payload']['stage']=='trace_collect' for x in b));self.assertTrue(b[-1]['collectors'])
        submitted={x['id'] for p in b[:-1] for x in p['payload']['documents']}
        self.assertEqual(submitted,{x['id'] for d in self.docs for x in d['blocks']})
        result=self.execute();self.assertEqual(result['rows'][0]['state'],'unknown') # all evidence cannot fit; never clip.
