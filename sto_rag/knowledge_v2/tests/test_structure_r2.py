import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from knowledge_v2.tables import cell,finalize,continuations,row_bundles
from knowledge_v2.word_evidence import apply_probe
from knowledge_v2.structural_model import validate_decision,interpret_tables,refresh_table_blocks
from knowledge_v2.structure import Cache


def table(tid='t1'):
    return finalize(dict(id=tid,rows=3,columns=2,method='ooxml',header_basis='first_row_candidate',header_rows=[1],
        cells=[cell(f'{tid}/r{r}/c{c}',r,c,text) for r,c,text in [(1,1,'CPU'),(1,2,'Processor'),(2,1,'RAM'),
              (2,2,'Memory'),(3,1,'SSD'),(3,2,'Storage')]],issues=[]))


class StructureR2Tests(unittest.TestCase):
    def test_render_numbers_use_exact_ordered_source_and_keep_original_text(self):
        result=dict(blocks=[dict(locator='p1',exact_text='Common requirements',number_label='11',numbering={'num':'1'}),
                            dict(locator='p2',exact_text='Item',numbering={'num':'2'}),
                            dict(locator='p3',exact_text='Unsupported',numbering={'num':'3'})],tables=[],
                    coverage=[dict(locator=x,reason='number') for x in ['p1/numbering','p2/numbering','p3/numbering','document/numbering']])
        probe=dict(paragraphs=[dict(text='Common requirements',label='7',page=31),dict(text='Item',label='а)',page=32)],
                   tables=[],method='test-render',page_basis='derived')
        apply_probe(result,probe,'evidence.pdf')
        self.assertEqual(result['blocks'][0]['number_label'],'7')
        self.assertEqual(result['blocks'][0]['computed_number_label'],'11')
        self.assertEqual(result['blocks'][0]['exact_text'],'Common requirements')
        self.assertEqual([x['locator'] for x in result['coverage']],['p3/numbering','document/numbering'])

    def test_ambiguous_repeated_paragraphs_do_not_get_number_proof(self):
        r=dict(blocks=[dict(locator='p'+str(i),exact_text='same') for i in range(2)],tables=[],coverage=[])
        apply_probe(r,dict(paragraphs=[dict(text='same',label=str(i)) for i in range(2)],tables=[],method='test',page_basis='derived'),'x')
        self.assertTrue(all(not b.get('numbering_verified') for b in r['blocks']))

    def test_continuation_handles_word_slivers_but_not_swapped_columns(self):
        def t(i,widths,cols,text):
            return finalize(dict(id=i,rows=1,columns=len(widths),grid_widths=widths,caption=('Таблица 1' if i=='a' else 'Продолжение таблицы 1'),
                header_rows=[1],header_basis='expert',cells=[cell(i+'x',1,1,text[0],colspan=cols),cell(i+'y',1,cols+1,text[1],colspan=len(widths)-cols)]))
        a=t('a',[100,100],1,['Name','Value']);b=t('b',[99,1,100],2,['Name','Value']);continuations([a,b])
        self.assertEqual(b['continuation_of'],'a')
        c=t('c',[99,1,100],2,['Value','Name']);continuations([b,c])
        self.assertIn('unresolved_continuation',c['issues'])
        d=t('d',[70,130],1,['Name','Value']);continuations([a,d]);self.assertIn('unresolved_continuation',d['issues'])

    def test_role_validation_rejects_fake_proof_and_invalid_confidence(self):
        d=dict(role='glossary',header_rows=[],preamble_rows=[],confidence=.9,evidence_cells=[dict(row=1,column=1)],reason='terms',uncertainties=[])
        validate_decision(table(),d)
        for change in [dict(evidence_cells=[dict(row=9,column=1)]),dict(confidence=float('nan')),dict(header_rows=[1]),dict(role='column_table')]:
            with self.assertRaises(ValueError):validate_decision(table(),dict(d,**change))

    def test_roles_preserve_all_values_refresh_blocks_and_cache_identical_shapes(self):
        class Client:
            signature='fake-v1';calls=0
            def complete(self,policy,data,schema):
                self.calls+=1
                return dict(value={'decisions':[dict(id=t['id'],role='glossary',header_rows=[],preamble_rows=[],
                    confidence=.95,evidence_cells=[dict(row=1,column=1)],reason='terms',uncertainties=[]) for t in data['tables']]},seconds=1,usage={})
        with tempfile.TemporaryDirectory() as tmp:
            client=Client();cache=Cache(Path(tmp),{'input':'fixture'})
            r=dict(tables=[table(),table('t2')],blocks=[dict(locator='t1/r2/c2',exact_text='Memory',header_path=['Processor'])])
            before=[[c['exact_text'] for c in t['cells']] for t in r['tables']]
            interpret_tables(r,cache,client);refresh_table_blocks(r)
            self.assertEqual(client.calls,1);self.assertEqual(r['table_interpretation']['unique_shapes'],1)
            self.assertEqual(before,[[c['exact_text'] for c in t['cells']] for t in r['tables']])
            self.assertEqual(r['blocks'][0]['header_path'],[])
            self.assertFalse(r['tables'][0]['confirmed_conclusions_allowed'])
            self.assertEqual(r['tables'][0]['structural_review_basis'],'model_candidate')
            self.assertEqual(len([c for b in row_bundles(r['tables'][0]) for c in b['cells']]),6)
            interpret_tables(dict(tables=[table('t3')]),cache,client);self.assertEqual(client.calls,1)

    def test_low_confidence_does_not_remove_issue(self):
        class Client:
            signature='uncertain'
            def complete(self,policy,data,schema):
                return dict(value={'decisions':[dict(id=data['tables'][0]['id'],role='glossary',header_rows=[],preamble_rows=[],
                    confidence=.6,evidence_cells=[dict(row=1,column=1)],reason='uncertain',uncertainties=['mixed'])]},seconds=1,usage={})
        with tempfile.TemporaryDirectory() as tmp:
            r=dict(tables=[table()]);interpret_tables(r,Cache(Path(tmp),{}),Client())
            self.assertIn('header_inferred',r['tables'][0]['issues'])

    def test_invalid_model_header_is_recorded_without_aborting_other_tables(self):
        class Client:
            signature='invalid-header'
            def complete(self,policy,data,schema):
                return dict(value={'decisions':[dict(id=data['tables'][0]['id'],role='column_table',header_rows=[1,3],preamble_rows=[],
                    confidence=.99,evidence_cells=[dict(row=1,column=1)],reason='bad',uncertainties=[])]},seconds=1,usage={})
        with tempfile.TemporaryDirectory() as tmp:
            r=dict(tables=[table()]);interpret_tables(r,Cache(Path(tmp),{}),Client())
            self.assertIn('model_structure_rejected',r['tables'][0]['issues'])
            self.assertEqual(r['tables'][0]['header_rows'],[1])
            self.assertTrue(r['tables'][0]['structural_interpretation']['validation_errors'])

    def test_model_cache_keeps_source_and_input_isolation_across_parser_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=Cache(Path(tmp)/'v1',{'source_sha256':'source','code':1})
            b=Cache(Path(tmp)/'v2',{'source_sha256':'source','code':2})
            key=a.model_key('table',{'full_input':'first','model':'revision-a'})
            a.put(key,{'result':1})
            self.assertEqual(key,b.model_key('table',{'full_input':'first','model':'revision-a'}))
            self.assertEqual(b.get(key),{'result':1})
            self.assertNotEqual(key,b.model_key('table',{'full_input':'changed','model':'revision-a'}))
            self.assertNotEqual(key,b.model_key('table',{'full_input':'first','model':'revision-b'}))
            c=Cache(Path(tmp)/'v3',{'source_sha256':'other'})
            self.assertNotEqual(key,c.model_key('table',{'full_input':'first','model':'revision-a'}))

    def test_repeated_header_has_source_proof_and_only_nearest_row_binding(self):
        t=table();t['cells'][4]['exact_text']='CPU';t['cells'][5]['exact_text']='Processor'
        t['rows']=4;t['cells'] += [cell('t1/r4/c1',4,1,'DISK'),cell('t1/r4/c2',4,2,'Volume')]
        d=dict(role='column_table',header_rows=[1,3],preamble_rows=[],confidence=.9,evidence_cells=[dict(row=1,column=1)],reason='repeat',uncertainties=[])
        validate_decision(t,d);t.update(header_rows=d['header_rows'],repeated_header_rows=d['repeated_header_rows']);finalize(t)
        self.assertEqual(t['cells'][-1]['header_refs'],['t1/r3/c2'])
        self.assertEqual(t['cells'][3]['header_refs'],['t1/r1/c2'])

    def test_preamble_is_context_in_each_bundle_not_lost(self):
        t=table();t.update(header_rows=[2],preamble_rows=[1])
        bundles=list(row_bundles(t,1))
        self.assertEqual(len(bundles),1)
        self.assertEqual({c['row'] for c in bundles[0]['headers']},{1,2})


if __name__=='__main__':unittest.main()
