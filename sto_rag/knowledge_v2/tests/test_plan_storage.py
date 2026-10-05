import copy,json,unittest
from knowledge_v2.store import checksum,encode,Conflict
from knowledge_v2.plan_storage import pack,unpack

class PlanStorageTests(unittest.TestCase):
    def payload(self,n=40):
        blocks=[dict(id=str(i),text='Полный исходный текст '*400,location={'table':i}) for i in range(12)]
        rows=[dict(id='r'+str(i),text='Нормативное требование '*100) for i in range(8)]
        batches=[]
        for i in range(n):
            packet=dict(documents=blocks,obligations=rows,completeness={'part':i,'parts':n,'full_text':False})
            batches.append(dict(id=checksum(packet),payload=packet))
        return dict(batches=batches,documents=[dict(blocks=blocks)],rows=rows,scopes={'target':{'expected_ids':[b['id'] for b in blocks]}})
    def test_wire_payloads_identical_after_json_roundtrip(self):
        p=self.payload();before=encode(p)
        restored=unpack(json.loads(encode(pack(p))))
        self.assertEqual(encode(restored),before)
        self.assertEqual(encode(p),before)
        self.assertIs(restored['batches'][0]['payload']['documents'][0],restored['batches'][1]['payload']['documents'][0])
    def test_large_repeated_plan_compacts_at_least_tenfold(self):
        p=self.payload(120)
        self.assertLess(len(encode(pack(p))),len(encode(p))//10)
    def test_legacy_payload_unchanged(self):
        p=self.payload();self.assertIs(unpack(p),p)
    def test_tampering_and_missing_refs_rejected(self):
        for mode in ('changed','missing','version'):
            p=copy.deepcopy(pack(self.payload()))
            if mode=='changed':p['plan_blocks']['0']['text']='changed'
            elif mode=='missing':del p['plan_blocks']['0']
            else:p['plan_storage']='unsupported'
            with self.assertRaises(Conflict):unpack(p)
    def test_conflicting_source_id_rejected(self):
        p=self.payload();p['batches'][1]['payload']=copy.deepcopy(p['batches'][1]['payload']);p['batches'][1]['payload']['documents'][0]['text']='other'
        with self.assertRaises(Conflict):pack(p)

    def test_large_completeness_lists_are_shared_and_wire_identical(self):
        p=self.payload(120)
        expected=['block-'+str(i)+'x'*50 for i in range(3000)]
        for batch in p['batches']:
            batch['payload']['completeness'].update(expected_ids=expected,submitted_ids=expected[:10],gaps=[])
            batch['id']=checksum(batch['payload'])
        compact=pack(p)
        self.assertLess(len(encode(compact)),len(encode(p))//10)
        restored=unpack(json.loads(encode(compact)))
        self.assertEqual(encode(restored),encode(p))
        self.assertIs(restored['batches'][0]['payload']['completeness']['expected_ids'],restored['batches'][1]['payload']['completeness']['expected_ids'])
        compact['plan_scope_lists'][checksum(expected)][0]='tampered'
        with self.assertRaises(Conflict):unpack(compact)

    def test_v1_saved_plans_still_load(self):
        p=self.payload();compact=pack(p)
        compact['plan_storage']='review-plan-refs-v1';compact.pop('plan_scope_lists')
        self.assertEqual(encode(unpack(compact)),encode(p))
