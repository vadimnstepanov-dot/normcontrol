import copy,json,unittest
from unittest.mock import patch
from knowledge_v2.shared_evidence import descriptors,plan,select,validate_collection,STAGE
from knowledge_v2.review import aggregate
from knowledge_v2.store import checksum,Conflict
from .test_review import ReviewTests,Model


class PlanningTests(unittest.TestCase):
    def test_span_roundtrip_keeps_all_ids_and_invalid_ranges_are_rejected(self):
        from knowledge_v2.shared_evidence import encode_spans,decode_spans
        ids=['B001','B002','B003','B004'];c={'matches':[{'obligation_id':'r','block_ids':['B001','B002','B004']}],'uncertain_obligations':[]}
        self.assertEqual(decode_spans(encode_spans(c,ids),ids),c)
        for a,b in (('B004','B001'),('B001','unknown')):
            with self.assertRaises(ValueError):decode_spans({'matches':{'r':[{'first':a,'last':b}]},'uncertain_obligations':[]},ids)
    def test_collector_wire_closes_aliases_and_restores_exact_source_ids(self):
        from .test_gap_transport import GapTransportTests
        from knowledge_v2.review_wire import COMPACT_NORMS_VERSION
        helper=GapTransportTests();p=helper.fixture();p['stage']=STAGE
        p['obligations'][0]['atom']={'text':'Условие.','conditions':['если'],'exceptions':['кроме']}
        c=helper.client(COMPACT_NORMS_VERSION);before=copy.deepcopy(p)
        req=c.request(p);fields=req['response_format']['json_schema']['schema']['properties']
        self.assertEqual(fields['matches']['properties'],{'R001':{'$ref':'#/$defs/spans'}})
        self.assertFalse(fields['matches']['additionalProperties'])
        spans=req['response_format']['json_schema']['schema']['$defs']['spans']
        self.assertEqual(spans['items']['properties']['first']['enum'],['B001'])
        original=c.http
        def reply(path,*args,**kwargs):
            if path=='/v1/chat/completions':return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'matches':{'R001':[{'first':'B001','last':'B001'}]},'uncertain_obligations':[]})}}]}
            return original(path,*args,**kwargs)
        c.http=reply
        with patch('knowledge_v2.model_profile.ensure'):raw=c.complete(p)
        self.assertEqual(raw,{'matches':[{'obligation_id':'rule','block_ids':['block']}],'uncertain_obligations':[]})
        self.assertEqual(p,before)

    def test_every_block_seen_once_with_every_norm_and_no_positive_scope(self):
        rows=[{'id':str(i),'document_id':'d','atom':{'text':'x','conditions':['when'],'exceptions':['unless']}} for i in range(40)]
        blocks=[dict(id=str(i),text='x'*100,document='d',locator='p'+str(i)) for i in range(100)]
        model=Model();model.context=5000
        scope=dict(expected_ids=[b['id'] for b in blocks],gaps=[],document_ids=['d'])
        batches,failed=plan(rows,blocks,model,scope)
        self.assertFalse(failed)
        collectors=[b for b in batches if b['payload']['stage']==STAGE]
        self.assertEqual([x['id'] for b in collectors for x in b['payload']['documents']],scope['expected_ids'])
        for b in collectors:
            self.assertEqual(b['payload']['obligations'],rows)
            self.assertFalse(b['payload']['completeness']['full_text'])
            self.assertLessEqual(model.count(b['payload'])+2*model.output_tokens+512,model.context)
        self.assertEqual(len(batches)-len(collectors),len(rows))

    def test_descriptors_preserve_conditions_exceptions_parameters_without_mutation(self):
        row={'id':'r','atom':{'text':'x','description':'x','object':'x','conditions':['a'],'exceptions':['b'],'parameters':{'min':12},'citations':[{'quote':'x'}]}}
        original=copy.deepcopy(row);lean=descriptors([row])[0]
        self.assertEqual(row,original)
        for k in ('conditions','exceptions','parameters'):self.assertEqual(lean['atom'][k],row['atom'][k])

    def test_outside_duplicate_and_invalid_collection_refs_fail_closed(self):
        for raw in ({'matches':[{'obligation_id':'other','block_ids':['b']}],'uncertain_obligations':[]},
                    {'matches':[{'obligation_id':'r','block_ids':['other']}],'uncertain_obligations':[]},
                    {'matches':[],'uncertain_obligations':['other']},
                    {'matches':[{'obligation_id':'r','block_ids':[]},{'obligation_id':'r','block_ids':[]}],'uncertain_obligations':[]}):
            with self.subTest(raw=raw),self.assertRaises(ValueError):validate_collection(raw,[{'id':'r'}],[{'id':'b'}])

    def test_whole_table_surroundings_and_missing_collector(self):
        blocks=[dict(id=str(i),document='d',text=str(i),table='t' if i<10 else None,locator='p'+str(i)) for i in range(20)]
        batch=dict(collectors=['c'],scope_id='d',payload={'obligations':[{'id':'r'}]})
        p={'documents':[{'blocks':blocks}],'scopes':{'d':{'expected_ids':[b['id'] for b in blocks]}}}
        with self.assertRaises(ValueError):select(batch,p,{})
        selected,_=select(batch,p,{'c':{'collection':{'matches':[{'obligation_id':'r','block_ids':['5']}],'uncertain_obligations':[]}}})
        self.assertEqual([b['id'] for b in selected],[str(i) for i in range(10)])


class SharedReviewTests(unittest.TestCase):
    add=ReviewTests.add
    create=ReviewTests.create
    def setUp(self):
        ReviewTests.setUp(self);self.model.shared_evidence=True
        previous=self.model.complete
        def complete(p):
            if p['stage']!=STAGE:return previous(p)
            self.model.calls.append(copy.deepcopy(p))
            return dict(matches=[dict(obligation_id=r['id'],block_ids=[b['id'] for b in p['documents']]) for r in p['obligations']],uncertain_obligations=[])
        self.model.complete=complete

    def test_shared_collection_preserves_verified_conflict_and_resumes_without_calls(self):
        tid=self.create();self.runner.run_once(tid);report=self.runner.report(tid)
        self.assertTrue(all(d['state']=='violated' for d in report['decisions']))
        self.assertEqual(sum(p['stage']==STAGE for p in self.model.calls),2)
        before=len(self.model.calls);self.runner.run_once(tid);self.assertEqual(len(self.model.calls),before)

    def test_empty_collection_runs_complete_fallback_not_absence_by_search(self):
        prev=self.model.complete
        self.model.complete=lambda p:dict(matches=[],uncertain_obligations=[]) if p['stage']==STAGE else prev(p)
        tid=self.create();self.runner.run_once(tid);report=self.runner.report(tid)
        self.assertTrue(all(d['state']=='violated' and not d['global_absence_proven'] for d in report['decisions']))
        with self.store.connection() as db:c=json.loads(db.execute('select cursor from tasks where id=?',(tid,)).fetchone()[0])
        finals=[v for v in c['results'].values() if v['decisions']]
        self.assertTrue(all(v['experience']['fallback_parts'] for v in finals))

    def test_positive_result_needs_two_complete_collectors_and_verification(self):
        previous=self.model.complete
        def answer(p):
            raw=previous(p)
            if p['stage']!=STAGE:
                for d in raw['decisions']:d.update(outcome='satisfied',claim='presence')
            return raw
        self.model.complete=answer;tid=self.create();self.runner.run_once(tid)
        self.assertTrue(all(d['state']=='checked' for d in self.runner.report(tid)['decisions']))
        self.assertTrue(any(p['stage']=='verify' for p in self.model.calls))

    def test_uncertain_collector_forces_fallback(self):
        previous=self.model.complete
        def answer(p):
            raw=previous(p)
            if p['stage']==STAGE:raw['uncertain_obligations']=[r['id'] for r in p['obligations']]
            return raw
        self.model.complete=answer;tid=self.create();self.runner.run_once(tid)
        with self.store.connection() as db:c=json.loads(db.execute('select cursor from tasks where id=?',(tid,)).fetchone()[0])
        self.assertTrue(all(v['experience'].get('fallback_parts') for v in c['results'].values() if v['decisions']))

    def test_child_checkpoint_change_is_rejected(self):
        tid=self.create();self.runner.run_once(tid)
        from knowledge_v2.shared_evidence import _collect
        from knowledge_v2.plan_storage import unpack
        with self.store.connection() as db:p=unpack(json.loads(db.execute('select payload from tasks where id=?',(tid,)).fetchone()[0]))
        packet=next(b['payload'] for b in p['batches'] if b['payload']['stage']==STAGE)
        key=checksum(['shared-normative-evidence-v1',p['snapshot'],p['owner'],p['job_id'],packet])
        path=self.store.directory/'shared-evidence-checkpoints'/(key+'.json')
        saved=json.loads(path.read_text());saved['value']['matches']=[];path.write_text(json.dumps(saved))
        with self.assertRaises(Conflict):_collect(self.runner,p,packet)
