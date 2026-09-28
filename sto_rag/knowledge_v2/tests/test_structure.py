import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from knowledge_v2.structure_docx import read_docx
from knowledge_v2.structure import parse_structure, Cache, atomic_json
from knowledge_v2.tables import cell,finalize,compare_tables,continuations,row_bundles,constraints
from knowledge_v2.numbering import Numbering
from knowledge_v2.ingest import W
import xml.etree.ElementTree as ET


def make_docx(path,body,numbering=None):
    with ZipFile(path,'w') as z:
        z.writestr('word/document.xml',f'<w:document xmlns:w="{W[1:-1]}"><w:body>{body}</w:body></w:document>')
        if numbering:z.writestr('word/numbering.xml',numbering)
    return path


def paragraph(s):return '<w:p><w:r><w:t>'+s+'</w:t></w:r></w:p>'
def tc(text,props=''):return '<w:tc><w:tcPr>'+props+'</w:tcPr>'+paragraph(text)+'</w:tc>'
def tr(cells,header=False):return '<w:tr>'+('<w:trPr><w:tblHeader/></w:trPr>' if header else '')+cells+'</w:tr>'


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.assets=self.root/'assets';self.assets.mkdir()

    def test_preceding_legend_follows_only_proven_continuations(self):
        def table(header='Условие'):
            return '<w:tbl>'+tr(tc(header),True)+tr(tc('Да'))+tr(tc('Нет'))+'</w:tbl>'
        legend='Примечание – В таблице «Да» означает обязательность, «Нет» — исключение.'
        body=(paragraph(legend)+paragraph('Таблица 7')+table()+
              paragraph('Продолжение таблицы 7')+table()+
              paragraph('Окончание таблицы 7')+table()+
              paragraph('Продолжение таблицы 7')+table('Другая шапка')+
              paragraph('Таблица 8')+table())
        r=read_docx(make_docx(self.root/'a.docx',body),self.assets)
        for t in r['tables'][:3]:
            self.assertIn('p1',t['note_refs'])
            self.assertTrue(all('p1' in c['note_refs'] for c in t['cells']))
        for t in r['tables'][3:]:self.assertNotIn('p1',t['note_refs'])
        for b in r['blocks']:
            if b.get('table') in ('t1','t2','t3'):self.assertIn('p1',b['note_refs'])
        # Re-evaluation after a corrected header removes an obsolete inherited link.
        r['tables'][1]['cells'][0]['exact_text']='Другая шапка'
        continuations(r['tables'])
        self.assertNotIn('p1',r['tables'][1]['note_refs'])
        self.assertFalse(any('p1' in c['note_refs'] for c in r['tables'][1]['cells']))

    def test_preceding_note_for_another_table_is_not_borrowed(self):
        legend='Примечание – В таблице 3 «Да» означает обязательность, «Нет» — исключение.'
        body=paragraph(legend)+paragraph('Таблица 4')+'<w:tbl>'+tr(tc('Условие'),True)+tr(tc('Да'))+tr(tc('Нет'))+'</w:tbl>'
        r=read_docx(make_docx(self.root/'a.docx',body),self.assets)
        self.assertEqual(r['tables'][0]['note_refs'],[])

    def test_docx_merges_multilevel_headers_notes_and_empty_not_filled(self):
        table=('<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>'+
            tr(tc('Операция','<w:vMerge w:val="restart"/>')+tc('Время, с','<w:gridSpan w:val="2"/>'),True)+
            tr(tc('','<w:vMerge/>')+tc('Штатная')+tc('Пиковая'),True)+
            tr(tc('Поиск')+tc('≤ 3,5')+tc(''))+'</w:tbl>')
        p=make_docx(self.root/'a.docx',paragraph('Таблица 2')+table+paragraph('Примечание 1. Кроме восстановления.'))
        r=read_docx(p,self.assets);t=r['tables'][0]
        self.assertEqual(t['cells'][0]['rowspan'],2)
        self.assertEqual(t['cells'][1]['colspan'],2)
        target=next(c for c in t['cells'] if c['exact_text']=='≤ 3,5')
        self.assertEqual(target['header_refs'],['t1/r1/c2','t1/r2/c2'])
        self.assertEqual(target['note_refs'],['p2'])
        self.assertEqual(t['structure_status'],'pass')
        self.assertEqual(len([c for c in t['cells'] if c['exact_text']=='']),1)
        self.assertEqual(target['constraints']['numbers'][0]['number'],'3.5')

    def test_orphan_merge_is_not_invented(self):
        p=make_docx(self.root/'a.docx','<w:tbl>'+tr(tc('','<w:vMerge/>'))+'</w:tbl>')
        t=read_docx(p,self.assets)['tables'][0]
        self.assertIn('orphan_vertical_merge',t['issues'])

    def test_nested_table_has_own_topology_without_duplicate_text(self):
        nested='<w:tbl>'+tr(tc('nested'))+'</w:tbl>'
        outer='<w:tbl>'+tr('<w:tc>'+paragraph('parent')+nested+'</w:tc>')+'</w:tbl>'
        r=read_docx(make_docx(self.root/'a.docx',outer),self.assets)
        self.assertEqual(len(r['tables']),2)
        self.assertEqual(sorted(c['exact_text'] for t in r['tables'] for c in t['cells']),['nested','parent'])

    def test_blank_dash_no_and_na_are_distinct(self):
        vals=[constraints(x) for x in ('','—','нет','не применимо')]
        self.assertEqual(len({json.dumps(x,sort_keys=True) for x in vals}),4)

    def test_overlap_and_gap_block_table(self):
        t=finalize(dict(id='x',rows=2,columns=2,cells=[cell('a',1,1,'1',colspan=2),cell('b',1,2,'2')],header_rows=[1],header_basis='expert'))
        self.assertIn('overlapping_cells',t['issues']);self.assertIn('grid_gaps',t['issues'])

    def test_shifted_numbers_or_sign_are_critical_even_with_equal_text_bag(self):
        def t(a,b):return finalize(dict(id='x',rows=1,columns=2,cells=[cell('a',1,1,a),cell('b',1,2,b)],header_rows=[1],header_basis='expert'))
        self.assertTrue(all(d['critical'] for d in compare_tables(t('≤ 3','≥ 6'),t('≥ 6','≤ 3'))))

    def test_continuation_requires_caption_headers_and_context(self):
        def t(i,caption,head):return finalize(dict(id=str(i),page=i,rows=2,columns=1,cells=[cell('h',1,1,head),cell('d',2,1,'5')],
            header_rows=[1],header_basis='expert',caption=caption,heading_path=['Приложение А']))
        a=t(1,'Таблица 1','Время');b=t(2,'Продолжение таблицы 1','Время');continuations([a,b])
        self.assertEqual(b['continuation_of'],'1')
        c=t(3,'Продолжение таблицы 1','Объем');continuations([b,c]);self.assertNotIn('continuation_of',c)

    def test_row_bundles_cover_each_data_cell_once(self):
        t=finalize(dict(id='x',rows=5,columns=1,cells=[cell(str(i),i,1,'sample'*10) for i in range(1,6)],header_rows=[1],header_basis='expert'))
        rows=list(row_bundles(t,100));ids=[c['id'] for b in rows for c in b['cells']]
        self.assertEqual(ids,['2','3','4','5'])
        self.assertTrue(all(b['headers'][0]['id']=='1' for b in rows))

    def test_numbering_multilevel_and_override(self):
        root=ET.fromstring(f'<w:numbering xmlns:w="{W[1:-1]}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl></w:abstractNum><w:num w:numId="2"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="4"/></w:lvlOverride></w:num></w:numbering>')
        n=Numbering(root)
        self.assertEqual(n.label({'num':'2','ilvl':'0'})[0],'4.')
        self.assertEqual(n.label({'num':'2','ilvl':'1'})[0],'4.1')
        self.assertEqual(n.label({'num':'2','ilvl':'0'})[0],'5.')
        self.assertEqual(n.label({'num':'2','ilvl':'1'})[0],'5.1')

    def test_corrupt_region_cache_is_not_reused(self):
        c=Cache(self.root,{'version':1});k=c.key('test',{'page':1});c.put(k,{'text':'≤ 3'})
        p=self.root/'cache'/(k+'.json');v=json.loads(p.read_text());v['value']['text']='≥ 3';atomic_json(p,v)
        with self.assertRaises(ValueError):c.get(k)

    def test_numbered_notes_in_adjacent_continuation_keep_full_conditions(self):
        numbering=f'<w:numbering xmlns:w="{W[1:-1]}"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1)"/></w:lvl></w:abstractNum><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>'
        marked='<w:tc>'+paragraph('Да')+'<w:p><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>1)</w:t></w:r></w:p></w:tc>'
        numbered='<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr><w:r><w:t>Только при изменении состава.</w:t></w:r></w:p>'
        first='<w:tbl>'+tr(tc('Условие'),True)+tr(marked)+'</w:tbl>'
        header='<w:tbl>'+tr(tc('Условие'),True)+'</w:tbl>'
        notes='<w:tbl>'+tr('<w:tc>'+numbered+paragraph('Исключение: настройка без изменения состава.')+'</w:tc>')+'</w:tbl>'
        d=read_docx(make_docx(self.root/'notes.docx',paragraph('Таблица 2')+first+paragraph('Окончание таблицы 2')+header+notes,numbering),self.assets)
        c=d['tables'][0]['cells'][1]
        self.assertEqual(c['note_refs'],['t3/r1/c1'])
        self.assertIn('Исключение:',c['note_bindings'][0]['exact_text'])
        self.assertFalse(any('unresolved_note' in x for x in d['tables'][0]['issues']))
        packet=list(row_bundles(d['tables'][0],20))[0]
        self.assertIn('t3/r1/c1',packet['note_refs'])
        self.assertIn('Исключение:',packet['note_bindings'][0]['exact_text'])
        self.assertTrue(packet['oversized'])

    def test_missing_original_invalidates_completed_cache(self):
        p=make_docx(self.root/'a.docx',paragraph('Источник'))
        with patch('knowledge_v2.structure.tools_signature',return_value={'test':'fixed'}):
            _,run=parse_structure(p,self.root/'runs',ocr=False)
            (run/'original.docx').unlink()
            with self.assertRaisesRegex(ValueError,'artifact corrupted or missing'):parse_structure(p,self.root/'runs',ocr=False)

    def test_parse_reuse_and_source_change_are_separate_runs(self):
        p=make_docx(self.root/'a.docx',paragraph('СТО РЖД 1')+paragraph('Исходный текст.'))
        with patch('knowledge_v2.structure.tools_signature',return_value={'test':'fixed'}):
            a,pa=parse_structure(p,self.root/'runs',ocr=False)
            b,pb=parse_structure(p,self.root/'runs',ocr=False)
            self.assertEqual(a,b);self.assertEqual(pa,pb)
            make_docx(p,paragraph('Изменённый текст.'))
            c,pc=parse_structure(p,self.root/'runs',ocr=False)
            self.assertNotEqual(pa,pc);self.assertTrue((pa/'result.json').exists())

    def test_external_link_not_fetched(self):
        p=self.root/'a.docx'
        with ZipFile(p,'w') as z:
            z.writestr('word/document.xml',f'<w:document xmlns:w="{W[1:-1]}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:hyperlink r:id="r1"/></w:p></w:body></w:document>')
            z.writestr('word/_rels/document.xml.rels','<Relationships><Relationship Id="r1" Target="https://example.invalid/file" TargetMode="External"/></Relationships>')
        r=read_docx(p,self.assets);self.assertIn('External relationship not fetched',[x['reason'] for x in r['coverage']])

    def test_small_word_grid_offsets_do_not_attach_value_to_neighbour_header(self):
        t=finalize(dict(id='x',rows=2,columns=5,grid_widths=[1000,30,1000,30,1000],header_rows=[1],header_basis='ooxml_explicit',
          cells=[cell('h1',1,1,'Создание',colspan=2),cell('h2',1,3,'Модификация'),cell('h3',1,4,'Адаптация',colspan=2),
                 cell('v1',2,1,'Да'),cell('v2',2,2,'Нет',colspan=3),cell('v3',2,5,'Да')]))
        self.assertEqual(t['cells'][4]['header_refs'],['h2'])
        self.assertEqual(t['cells'][4]['header_sliver_refs'],['h1','h3'])

    def test_reparse_keeps_legacy_records_and_is_idempotent(self):
        import io,uuid
        from knowledge_v2.store import KnowledgeStore
        from knowledge_v2.ingest import ingest,sha256
        from knowledge_v2.structure_runtime import reparse_source
        from knowledge_v2.norms import fragments_from_store
        p=make_docx(self.root/'a.docx',paragraph('Первоначальный текст.'))
        store=KnowledgeStore(self.root/'data');sid=str(uuid.uuid4());source=str(uuid.uuid4())
        store.register_set(sid,'scope',{'name':'test'})
        ingest(store,sid,source,'a.docx',sha256(p),io.BytesIO(p.read_bytes()))
        before=fragments_from_store(store,sid,source)
        with patch('knowledge_v2.structure.tools_signature',return_value={'test':'fixed'}):
            first=reparse_source(store,sid,source,ocr=False);n=store.counts()['records']
            second=reparse_source(store,sid,source,ocr=False)
        self.assertEqual(first,second);self.assertEqual(n,store.counts()['records'])
        self.assertEqual(before,fragments_from_store(store,sid,source))
        with self.assertRaises(ValueError):reparse_source(store,'other',source,ocr=False)


if __name__=='__main__':unittest.main()
