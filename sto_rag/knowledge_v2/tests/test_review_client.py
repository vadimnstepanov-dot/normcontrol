import copy,json,unittest
from unittest.mock import patch
from knowledge_v2.review_client import wire_payload,LlamaClient
from knowledge_v2.review_wire import restore,serialize

def unpack(wire):
    columns=wire['document_columns']
    return [dict(zip(columns,row)) for row in wire['documents']]

class WireTests(unittest.TestCase):
    def test_model_block_alias_is_restored_before_source_quote_is_attached(self):
        from knowledge_v2.store import checksum
        from knowledge_v2.review_wire import VERSION
        c=LlamaClient.__new__(LlamaClient);c.model='test';c.output_tokens=4096;c.context=49152
        c.signature=checksum(dict(props={},model='test',temperature=0,thinking=False,wire_version=VERSION))
        c.count=lambda payload:0
        def reply(path,*args,**kwargs):
            if path=='/props':return {}
            if path=='/v1/models':return {'data':[{'id':'test'}]}
            return {'choices':[dict(finish_reason='stop',message={'content':json.dumps({'decisions':[
                dict(obligation_id='R001',outcome='satisfied',claim='presence',reason='Есть',evidence=[{'block_id':'B001'}])
            ]})})]}
        c.http=reply
        payload=dict(stage='check',obligations=[{'id':'rule'}],documents=[{'id':'source-block','text':'Не менее\u00a048 часов; кроме тестовой среды.'}])
        with patch('knowledge_v2.model_profile.ensure'):
            decision=c.complete(payload)['decisions'][0]
        self.assertEqual(decision['obligation_id'],'rule')
        self.assertEqual(decision['evidence'],[{'block_id':'source-block','quote':payload['documents'][0]['text']}])

    def test_normative_evidence_is_block_only_and_verify_does_not_duplicate_quotes(self):
        c=LlamaClient.__new__(LlamaClient);c.model='test';c.output_tokens=4096
        original=dict(stage='verify',obligations=[{'id':'r'}],documents=[{'id':'b','text':'Полный исходный текст'}],
            proposed=[dict(obligation_id='r',evidence=[dict(block_id='b',quote='Полный исходный текст')])])
        before=copy.deepcopy(original);req=c.request(original)
        item=req['response_format']['json_schema']['schema']['properties']['decisions']['items']['properties']['evidence']['items']
        self.assertEqual(item['required'],['block_id']);self.assertFalse(item['additionalProperties'])
        packed=json.loads(req['messages'][1]['content'])
        self.assertEqual(packed['proposed'][0]['evidence'],[{'block_id':'B001'}])
        self.assertEqual(original,before)
        trace=c.request(dict(original,stage='trace_check'))
        evidence=trace['response_format']['json_schema']['schema']['properties']['decisions']['items']['properties']['evidence']['items']
        self.assertEqual(evidence['required'],['block_id','quote'])

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
        self.assertEqual(wire['applicability_contexts'][wire['obligations'][0]['applicability_ref']],row['applicability'])
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

    def test_table_row_and_headers_are_exact_without_repeating_heading_structure(self):
        blocks=[dict(id='b'+str(i),document='doc',text=str(i),locator='t1/r'+str(i)+'/c2',
                     headings=['Раздел данных'],heading_refs=['p10'],table=1,row=i,header_path=['Параметр','Предел']) for i in range(1,101)]
        wire,_=wire_payload(dict(obligations=[{'id':'rule'}],documents=blocks))
        self.assertEqual(len(wire['document_structures']),1)
        self.assertEqual(len(wire['document_headers']),1)
        for original,packed in zip(blocks,unpack(wire)):
            self.assertEqual(packed['text'],original['text'])
            self.assertEqual(packed['row'],original['row'])
            self.assertEqual(packed['locator'],original['locator'])
            self.assertEqual(wire['document_headers'][packed['header_ref']],original['header_path'])
            structure=wire['document_structures'][packed['structure_ref']]
            for key in ('headings','heading_refs','table'):self.assertEqual(structure[key],original[key])

    def test_normative_quotes_conditions_and_source_revisions_survive(self):
        citation=dict(quote='Если согласовано: не более 12 ч.',locator='p5',context_hash='hash',source_sha256='sha',start=4,end=20)
        atom=dict(description='Ограничение',conditions=['После согласования'],exceptions=['Кроме испытаний'],citations=[citation])
        fragment=dict(exact_text=citation['quote'],search_text='поисковая копия',ref=['fragment',2],source_revision=['standard',3])
        original=dict(obligations=[dict(id='r',atom=atom,context=[fragment],dependencies=[{'condition':'только по решению комиссии'}])])
        before=copy.deepcopy(original);wire,_=wire_payload(original);row=wire['obligations'][0]
        self.assertEqual(original,before)
        for key in ('description','conditions','exceptions'):self.assertEqual(row['atom'][key],atom[key])
        self.assertEqual(row['dependencies'],original['obligations'][0]['dependencies'])
        compact=row['atom']['citations'][0]
        self.assertEqual(compact['quote'],citation['quote']);self.assertEqual(compact['locator'],citation['locator'])
        self.assertEqual(wire['sources'][compact['source_ref']],'sha')
        f=wire['normative_contexts'][row['context_refs'][0]]
        self.assertEqual(f['exact_text'],fragment['exact_text'])
        self.assertEqual(wire['sources'][f['source_revision_alias']],['standard',3])
