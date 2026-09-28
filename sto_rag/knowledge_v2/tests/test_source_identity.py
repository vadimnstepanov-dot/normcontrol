import copy,unittest,tempfile
from pathlib import Path
from knowledge_v2.source_identity import FIELDS,validate,extract
from knowledge_v2.store import checksum


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.text='Порядок технического проектирования. УТВЕРЖДЕН распоряжением № 100/р от 24.08.2026. Директор И.И. Иванов.'
        self.blocks=[dict(locator='p1',exact_text=self.text,context_hash=checksum(self.text),source_sha256='a'*64)]
        self.reply={k:dict(value='',citations=[]) for k in FIELDS};self.reply['confidence']=.91
        for k,v in dict(short_title='Порядок технического проектирования',full_title='Порядок технического проектирования',approved_by='Директор И.И. Иванов',approval_date='24.08.2026',approval_document_number='100/р',approval_document_date='24.08.2026').items():self.reply[k]=dict(value=v,citations=[dict(locator='p1',quote=self.text)])
    def test_exact_title_requisites_and_uninvented_signer(self):
        r=validate(self.reply,self.blocks);self.assertEqual(r['status'],'identified');self.assertEqual(r['fields']['signed_by']['value'],'')
        self.assertEqual(r['fields']['approval_document_number']['value'],'100/р')
        self.assertEqual(r['fields']['short_title']['citations'][0]['context_hash'],checksum(self.text))
    def test_forged_number_and_quote_not_trusted(self):
        bad=copy.deepcopy(self.reply);bad['approval_document_number']['value']='999/р'
        r=validate(bad,self.blocks);self.assertEqual(r['fields']['approval_document_number']['value'],'');self.assertEqual(r['status'],'needs_review')
        bad=copy.deepcopy(self.reply);bad['approved_by']['citations'][0]['quote']='Другой директор'
        self.assertEqual(validate(bad,self.blocks)['fields']['approved_by']['value'],'')
    def test_cached_call_and_bounded_selection(self):
        reply=self.reply
        class Model:
            signature='test-source-identity';calls=0
            def complete(self,policy,data,schema):
                self.calls+=1
                assert sum(len(b['text']) for b in data['title_fragments'])<=16000
                return dict(value=reply,seconds=.01,usage={})
        with tempfile.TemporaryDirectory() as tmp:
            model=Model();source=dict(sha256='a'*64)
            a=extract('source',source,self.blocks,Path(tmp),model);b=extract('source',source,self.blocks,Path(tmp),model)
            self.assertEqual(model.calls,1);self.assertEqual(b['metrics']['reused'],1);self.assertEqual(a['fields'],b['fields'])

