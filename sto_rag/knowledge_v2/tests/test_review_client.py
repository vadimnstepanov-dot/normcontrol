import copy,json,unittest
from knowledge_v2.review_client import wire_payload,LlamaClient
from knowledge_v2.review_wire import restore,serialize

def unpack(wire):
    columns=wire['document_columns']
    return [dict(zip(columns,row)) for row in wire['documents']]

class WireTests(unittest.TestCase):
    def test_aliases_keep_full_evidence_and_do_not_mutate_durable_plan(self):
        payload={'stage':'verify','obligations':[{'id':'a'*64,'obligation_id':'uuid-a','context':['Full condition']},{'id':'b'*64,'obligation_id':'uuid-b'}],
                 'documents':[{'id':'document','text':'Full text'}], 'completeness':{'complete':False},
                 'proposed':[{'obligation_id':'b'*64,'outcome':'unknown'}]}
        before=copy.deepcopy(payload);wire,ids=wire_payload(payload)
        self.assertEqual(payload,before);self.assertEqual(unpack(wire)[0]['text'],payload['documents'][0]['text'])
        self.assertEqual(wire['completeness'],payload['completeness'])
        self.assertEqual(wire['obligations'][0]['context'],payload['obligations'][0]['context'])
        self.assertEqual(wire['obligations'][0]['id'],'R001');self.assertEqual(wire['proposed'][0]['obligation_id'],'R002')
        self.assertEqual(ids['R002'],'b'*64)
        c=LlamaClient.__new__(LlamaClient);c.model='test';c.output_tokens=1024
        req=c.request(payload);schema=req['response_format']['json_schema']['schema']['properties']['decisions']
        self.assertEqual(schema['minItems'],2);self.assertEqual(schema['maxItems'],2)
        self.assertEqual(schema['items']['properties']['obligation_id']['enum'],['R001','R002'])

    def test_duplicate_or_foreign_ids_fail_closed(self):
        with self.assertRaises(ValueError):wire_payload({'obligations':[{'id':'x'},{'id':'x'}]})
        with self.assertRaises(ValueError):wire_payload({'obligations':[{'id':'x'}],'proposed':[{'obligation_id':'foreign'}]})

    def test_large_coverage_and_profiles_do_not_consume_model_context(self):
        row=dict(id='r',atom={'condition':'В тестовой среде','exception':'Кроме архивных данных'},
                 context=[{'exact_text':'Полная таблица ограничений'}],dependencies=[{'condition':'После согласования'}],
                 applicability={'result':'applicable'},profile_versions=[{'id':str(i),'expression':{'fact':{'name':'type','in':['T']}}} for i in range(100)])
        ids=[str(i).zfill(64) for i in range(1000)]
        scope=dict(expected_ids=ids,submitted_ids=ids[:2],full_text=False,parts=500,gaps=[{'reason':'Unread diagram'}])
        payload=dict(obligations=[row],documents=[{'id':ids[0],'text':'Доказательство'}],completeness=scope)
        before=copy.deepcopy(payload);wire,_=wire_payload(payload)
        self.assertEqual(payload,before)
        self.assertEqual(wire['obligations'][0]['atom'],row['atom'])
        self.assertEqual([wire['normative_contexts'][ref] for ref in wire['obligations'][0]['context_refs']],row['context'])
        self.assertEqual(wire['obligations'][0]['dependencies'],row['dependencies'])
        self.assertEqual(wire['obligations'][0]['applicability'],row['applicability'])
        self.assertNotIn('profile_versions',wire['obligations'][0])
        self.assertEqual(wire['completeness']['expected_block_count'],1000)
        self.assertEqual(wire['completeness']['submitted_block_count'],2)
        self.assertFalse(wire['completeness']['full_text'])
        self.assertEqual(wire['completeness']['gap_reasons'][wire['completeness']['gaps'][0]['reason_ref']],scope['gaps'][0]['reason'])
        self.assertLess(len(json.dumps(wire)),len(json.dumps(payload))/10)

    def test_tables_conditions_and_document_identity_survive_compaction(self):
        text='При согласовании комиссии — не менее 48 часов, кроме тестовой среды.'
        fragment=dict(exact_text=text,locator='A/3',structure=dict(heading_path=['Условия'],table='t1',row=4,column=2,span=2,
                      merge_origin=[4,1],header_path=['Согласование','Минимум'],search_text=text,source_sha256='x'*64))
        row=dict(id='r1',atom={'condition':'После согласования','exception':'Кроме тестовой среды'},context=[fragment],dependencies=[{'target_ref':['f',1]}])
        original=dict(obligations=[row,dict(row,id='r2')],documents=[dict(id='first',document='d1',text=text,locator='t2/r1/c1',
            headings=['Раздел 3'],header_path=['RTO'],table='t2',row=1)],completeness={'document_ids':['d1'],'full_text':False,'gaps':[]})
        wire,identities=wire_payload(original)
        self.assertEqual(len(wire['normative_contexts']),1)
        self.assertEqual(wire['obligations'][0]['context_refs'],wire['obligations'][1]['context_refs'])
        norm=next(iter(wire['normative_contexts'].values()));structure=wire['normative_structures'][norm['structure_ref']]
        self.assertEqual(norm['exact_text'],text)
        for key in ('span','merge_origin','header_path','heading_path'):self.assertEqual(structure[key],fragment['structure'][key])
        block=unpack(wire)[0];self.assertEqual(block['text'],text)
        self.assertEqual(wire['document_structures'][block['structure_ref']]['document'],'D1')
        answer={'decisions':[dict(obligation_id='R001',evidence=[{'block_id':'B001','quote':text}])]}
        restore(answer,identities)
        self.assertEqual(answer['decisions'][0]['obligation_id'],'r1')
        self.assertEqual(answer['decisions'][0]['evidence'][0]['block_id'],'first')
        with self.assertRaises(ValueError):restore({'decisions':[dict(obligation_id='R002',evidence=[{'block_id':'B999','quote':text}])]},identities)

    def test_verify_keeps_the_same_input_prefix(self):
        check=dict(stage='check',obligations=[{'id':'r','context':[{'exact_text':'Условие'}]}],
                   documents=[{'id':'b','document':'d','text':'Полная цитата'}],completeness={'full_text':True})
        verify=dict(check,stage='verify',proposed=[dict(obligation_id='r',evidence=[{'block_id':'b','quote':'Полная цитата'}])])
        a,_=wire_payload(check);b,_=wire_payload(verify)
        self.assertEqual(serialize(a).split(',"stage":')[0],serialize(b).split(',"stage":')[0])
