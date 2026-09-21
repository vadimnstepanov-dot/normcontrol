import copy
import json
import tempfile
import unittest
from pathlib import Path
from nc5.checks import compatible,validate_evidence,decimal
from nc5.documents import split_blocks,relationships,inspect_file
from nc5.store import Store,LRU
from nc5.catalog import kind

class Core(unittest.TestCase):
    def test_constraints(self):
        a=dict(entity='users',parameter='latency',unit='s',scope='response',environment='prod',conditions='peak',time_basis='calendar',value='6',operator='<=')
        b={**a,'value':'15'};self.assertTrue(compatible(a,b))
        self.assertFalse(compatible(a,{**b,'operator':'>='}))
        self.assertIsNone(compatible(a,{**b,'environment':'test'}))
        self.assertIsNone(compatible(a,{**b,'time_basis':'working'}))
    def test_exact_duplicated_quotes(self):
        docs=[{'id':'d','blocks':[{'locator':'p1','text':'Снова текст','address':'1'},{'locator':'p2','text':'Снова текст','address':'2'}]}]
        self.assertEqual(validate_evidence([{'document':'d','locator':'p2','quote':'Снова текст'}],docs)[0]['address'],'2')
        with self.assertRaises(ValueError):validate_evidence([{'document':'d','locator':'p2','quote':'Другой текст'}],docs)
    def test_split_no_loss(self):
        text='Один длинный абзац ' * 1000;b={'text':text,'offset':0,'locator':'p1','document':'d','table':{'row':1,'column_name':'Назначение'}}
        parts=split_blocks([b]);self.assertEqual(''.join(x[0]['text'] for x in parts),text);self.assertEqual(parts[1][0]['offset'],len(parts[0][0]['text']));self.assertEqual(parts[1][0]['table'],b['table'])
    def test_normative_note_and_example(self):
        self.assertEqual(kind('Примечание. Не допускается удалять журнал.'),'обязательное требование')
        self.assertEqual(kind('Система должна хранить журнал.',True),'пример')
        self.assertEqual(kind('Если данные есть, их необходимо описать.'),'условное требование')
        self.assertEqual(kind('Следует описать механизм. Рекомендуется привести схему.'),'смешанный нормативный фрагмент')
        self.assertEqual(kind('Рекомендуется использовать таблицу.'),'рекомендация')
    def test_decimal(self):self.assertEqual(decimal('4023,65')+decimal('402,36'),decimal('4426,01'))
    def test_mixed_script_field_and_legitimate_compound_name(self):
        from nc5.checks import deterministic
        doc={'id':'d','blocks':[{'locator':'p1','text':'сontractKey','table_context':{'table':1,'row':2,'column':1,'column_name':'Имя атрибута'}},
            {'locator':'p2','text':'ВесeMars'}]}
        fs=deterministic(doc)
        self.assertEqual(len(fs),1);self.assertEqual(fs[0]['evidence'][0]['locator'],'p1')
    def test_llm_cannot_confirm_compatible_upper_bounds(self):
        from nc5.quality_gate import assess
        f={'issue':'Противоречие в максимальном времени','explanation':'Разные пределы','evidence':[{'quote':'Максимальное время одной операции — 2 секунды.'},{'quote':'Максимальное время одной операции — 11 секунд.'}]}
        self.assertEqual(assess(f)[0],'question')
        f['evidence'][1]['quote']='Фактическое измеренное время одной операции — 11 секунд.'
        self.assertIsNone(assess(f)[0])
    def test_grammar_routing_is_not_auto_confirmation(self):
        from nc5.checks import route_finding
        value={'category':'оформление','kind':'style','issue':'Согласование числа глагола','explanation':'Нарушение согласования подлежащего и сказуемого.'}
        routed=route_finding(value,'language');self.assertEqual(routed['kind'],'violation');self.assertEqual(routed['category'],'грамотность');self.assertNotIn('status',routed)
    def test_same_operands_are_one_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'db');j=s.create({});f={'category':'арифметика','issue':'One title','evidence':[{'document':'d','locator':'p1','quote':'3'}]}
            a=s.finding(j,f,'confirmed');b=s.finding(j,{**f,'issue':'Another title'})
            self.assertEqual(a,b);self.assertEqual(len(s.findings(j)),1)
    def test_lru(self):
        l=LRU(20);l.put('a',{'x':'123'});l.put('b',{'x':'456'});self.assertLessEqual(l.size,20);self.assertIsNone(l.get('a'))
    def test_finding_disposition_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'s.db');j=s.create({});value=s.set_disposition(j,'f1','in_work')
            self.assertEqual(value['state'],'in_work');self.assertEqual(s.dispositions(j)['f1']['state'],'in_work')
            with self.assertRaises(ValueError):s.set_disposition(j,'f1','disputed','нет')
    def test_recovery_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'s.db');j=s.create({});s.update(j,'running');a=s.add(j,'language',{'x':1});self.assertEqual(a,s.add(j,'language',{'x':1}));t=s.claim(j,'language');self.assertIsNone(s.claim(j,'language'));self.assertTrue(s.finish(t,{}));self.assertFalse(s.finish(t,{}));s.recover();self.assertEqual(s.tasks(j)[0]['state'],'done')
    def test_relationships_exact(self):
        docs=[{'id':str(i),'titles':['Одна точная система'] if i<3 else ['Похожая система'],'codes':[]} for i in range(4)]
        r=relationships(docs);self.assertEqual(len(r['groups']),1);self.assertEqual(r['unlinked'],['3'])

if __name__=='__main__':unittest.main()
