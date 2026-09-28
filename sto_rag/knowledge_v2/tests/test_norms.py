import copy
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from zipfile import ZipFile
from knowledge_v2.norms import extract, citation
from knowledge_v2.norm_validation import provenance, audit_coverage
from knowledge_v2.applicability import evaluate, validate_expression, select_requirements, match_profiles
from knowledge_v2.ingest import parse, ingest, sha256
from knowledge_v2.norm_runtime import analyze_source,projection_chunks
from knowledge_v2.store import KnowledgeStore
from .test_ingest import docx
from knowledge_v2.norm_context import build_packet, ContextTooLarge

SID=str(uuid.uuid5(uuid.NAMESPACE_URL,'synthetic-regulation'))


def block(loc,text,**kw):
    return dict(locator=loc,exact_text=text,search_text=text.lower(),id=str(uuid.uuid5(uuid.UUID(SID),loc)),
                context_hash='context',source_sha256='a'*64,kind='paragraph',**kw)


def run(*blocks):return extract(SID,{'sha256':'a'*64},list(blocks))


class NormTests(unittest.TestCase):
    def test_analysis_transfer_chunks_are_byte_bounded_without_losing_cards(self):
        entries=[dict(id=str(i),locator=f'p{i}',state='needs_review',validation={},
                      citations=[{'quote':'Норма '*3000}]) for i in range(17)]
        chunks=projection_chunks(entries,max_bytes=80_000)
        self.assertGreater(len(chunks),1)
        self.assertEqual([entry for chunk in chunks for entry in chunk],entries)
        self.assertTrue(all(len(json.dumps(chunk,ensure_ascii=False).encode())<80_000 for chunk in chunks))

    def test_modalities_examples_and_exceptions(self):
        r=run(block('p1','Отчёт должен содержать дату.'),block('p2','Рекомендуется указать автора.'),
              block('p3','Допускается электронная подпись.'),block('p4','Не допускается удаление журнала.'),
              block('p5','Пример — Система должна отвечать за секунду.'),
              block('p6','Если есть обмен, система должна вести журнал, кроме тестового режима.'))
        self.assertEqual([c['modality'] for c in r['cards'][:4]],['mandatory','recommended','permitted','prohibited'])
        self.assertEqual(r['cards'][4]['state'],'example')
        c=r['cards'][5];self.assertEqual(c['state'],'needs_review')
        self.assertTrue(c['conditions']);self.assertTrue(c['exceptions'])
        self.assertEqual(c['validation']['applicability']['result'],'unknown')

    def test_adjacent_modal_words_and_normative_heading(self):
        c=run(block('p1','Обязательно должно быть приведено описание.'),
              block('p2','Документ должен иметь подпись.',is_heading=True))['cards']
        self.assertEqual(len(c[0]['obligations']),1)
        self.assertTrue(all(x['validation']['provenance']['status']=='verified' for x in c))
        bad=copy.deepcopy(c[0]);bad['obligations'][0]['object']='Выдуманный объект'
        self.assertIn('object_not_in_quote',provenance(bad,{'p1':block('p1','Обязательно должно быть приведено описание.')})['errors'])

    def test_quoted_fields_and_parent_enumeration(self):
        r=run(block('p1','Таблица должна содержать следующие поля: «Код», «Название», «Дата».'),
              block('p2','Документ должен содержать:'),block('p3','— реквизиты;'),block('p4','— подпись.'))
        self.assertEqual([a['object'] for a in r['cards'][0]['obligations']],['Код','Название','Дата'])
        parent=r['cards'][1];self.assertEqual(len(parent['child_requirements']),2)
        self.assertEqual(len([d for d in parent['dependencies'] if d['relation']=='enumeration_item']),2)
        self.assertTrue(any(d['target']=='p2' for d in r['cards'][2]['dependencies']))

    def test_independent_provenance_and_coverage(self):
        b=block('p1','Журнал должен сохраняться.');r=run(b);c=copy.deepcopy(r['cards'][0])
        c['citations'][0]['quote']='Подменённое требование'
        self.assertEqual(provenance(c,{'p1':b})['status'],'failed')
        self.assertTrue(audit_coverage({'p1':b},[],[{'locator':'p1','state':'context'}])['gaps'])
        c=copy.deepcopy(r['cards'][0]);c['operation']='eval'
        self.assertIn('unsafe_operation',provenance(c,{'p1':b})['errors'])

    def test_unknown_and_positive_decisions_require_verified_evidence(self):
        expr={'fact':{'name':'stage','in':['design']}}
        fact={'stage':{'value':'operation','evidence':[{'source':'doc','locator':'p1'}]}}
        self.assertEqual(evaluate(expr,{})['result'],'unknown')
        self.assertEqual(evaluate(expr,fact)['result'],'unknown')
        self.assertEqual(evaluate(expr,fact,lambda *args:True)['result'],'not_applicable')
        fact['stage']['value']='design'
        self.assertEqual(evaluate(expr,fact,lambda *args:True)['result'],'applicable')
        fact['stage']['value']=[]
        self.assertEqual(evaluate(expr,fact,lambda *args:True)['result'],'unknown')
        with self.assertRaises(ValueError):validate_expression({'python':'os.system()'})

    def test_foreign_profiles_inherit_common_without_siblings(self):
        r=run(block('p1','Общие требования',is_heading=True,heading_refs=['p1']),
              block('p2','Документ должен иметь подпись.',heading_refs=['p1']),
              block('p3','Шаблон документа «План миссии»',is_heading=True,heading_refs=['p3']),
              block('p4','Приводится перечень целей.',heading_refs=['p3']),
              block('p5','Шаблон документа «Протокол испытаний»',is_heading=True,heading_refs=['p5']),
              block('p6','Описывается результат.',heading_refs=['p5']))
        p=next(p for p in r['profiles'] if p['name']=='План миссии')
        self.assertEqual({c['locator'] for c in select_requirements(r['profiles'],r['cards'],p['id'])},{'p2','p4'})
        own=next(c for c in r['cards'] if c['locator']=='p4')
        facts={'selected_sources':{'value':[SID],'evidence':[{'source':'selection','locator':'set'}]},
               'document_type':{'value':'Протокол испытаний','evidence':[{'source':'doc','locator':'title'}]}}
        self.assertEqual(evaluate(own['applicability'],facts,lambda *args:True)['result'],'not_applicable')

    def test_table_matrix_cannot_be_published_as_unconditional(self):
        b=block('t1/r4/c4','Да',table=1,row=4,column=4,heading_refs=['p1'],heading_path=['Состав комплекта'])
        b['kind']='table_cell'
        r=run(block('p1','Состав комплекта',is_heading=True,heading_refs=['p1']),
              block('p2','Примечание — Если иное не указано в договоре.',heading_refs=['p1']),b)
        c=next(c for c in r['cards'] if c['locator']==b['locator'])
        self.assertEqual(c['category'],'document_set');self.assertEqual(c['state'],'needs_review')
        self.assertEqual(c['modality'],'unknown');self.assertTrue(any(d['relation']=='note' for d in c['dependencies']))

    def test_unresolved_reference_never_validated(self):
        r=run(block('p1','Отчёт должен соответствовать пункту 99.2.'))
        c=r['cards'][0];self.assertEqual(c['state'],'needs_review')
        self.assertIn('unresolved_dependency',c['validation']['completeness']['reasons'])

    def test_overlapping_profile_dag_keeps_common_and_deduplicates(self):
        def profile(pid,name,parents):
            return dict(id=pid,name=name,version=1,basis=[{'source':'approved_policy'}],inherits=parents,
                        expression={'fact':{'name':name,'in':[True]}})
        profiles=[profile('org','organization',[]),profile('sto','standard',['org']),profile('oit','oit',['sto'])]
        cards=[{'id':'a','effective_profile_id':'org','state':'needs_review'},
               {'id':'b','profile_ids':['sto','oit'],'state':'needs_review'},
               {'id':'c','effective_profile_id':'oit','state':'needs_review'}]
        self.assertEqual(len(select_requirements(profiles,cards,['org','sto','oit'])),3)
        facts={k:{'value':True,'evidence':[{'source':'document','locator':'title'}]} for k in ['organization','standard','oit']}
        self.assertTrue(all(r['result']=='applicable' for r in match_profiles(profiles,facts,lambda *args:True).values()))
        del facts['organization']
        self.assertTrue(all(r['result']=='unknown' for r in match_profiles(profiles,facts,lambda *args:True).values()))

    def test_packet_deduplicates_shared_evidence_and_never_truncates(self):
        blocks=[block('p1','Документ должен содержать:'),block('p2','— подпись;'),block('p3','— дату.')]
        cards=run(*blocks)['cards'];fragments={b['locator']:b for b in blocks}
        packet=build_packet(cards,fragments,lambda text:200,8192)
        self.assertEqual(len(packet['packet']['evidence']),3)
        with self.assertRaises(ContextTooLarge):build_packet(cards,fragments,lambda text:9000,8192)
        del fragments['p1']
        with self.assertRaises(ValueError):build_packet(cards[1:],fragments,lambda text:200,8192)

    def test_durable_analysis_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            store=KnowledgeStore(Path(temp)/'data');set_id=str(uuid.uuid4())
            store.register_set(set_id,'scope',{})
            path=docx(Path(temp)/'source.docx')
            ingest(store,set_id,SID,path.name,sha256(path),io.BytesIO(path.read_bytes()))
            one=analyze_source(store,set_id,SID);count=store.counts()
            two=analyze_source(store,set_id,SID)
            self.assertEqual(one,two);self.assertEqual(count,store.counts())
            self.assertIsNone(one['summary']['violation_count'])
            self.assertEqual(one['coverage_audit']['gaps'],[])
            self.assertEqual(one['summary']['provenance'].get('failed',0),0)

    def test_inherited_headings_siblings_and_appendix_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'structure.docx'
            ns='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
            def paragraph(text,style):return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
            body=paragraph('Общие положения','root')+paragraph('Шаблон документа «Отчёт»','app')+paragraph('1 Первая глава','child')+paragraph('2 Вторая глава','child')
            styles='<w:style w:styleId="root"><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style><w:style w:styleId="app"><w:pPr><w:outlineLvl w:val="4"/></w:pPr></w:style><w:style w:styleId="base"><w:pPr><w:outlineLvl w:val="8"/></w:pPr></w:style><w:style w:styleId="child"><w:basedOn w:val="base"/></w:style>'
            with ZipFile(p,'w') as z:
                z.writestr('word/document.xml',f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
                z.writestr('word/styles.xml',f'<w:styles xmlns:w="{ns}">{styles}</w:styles>')
            blocks=parse(p)['blocks']
            self.assertEqual(blocks[-1]['heading_refs'],['p2','p4'])


if __name__=='__main__':unittest.main()
