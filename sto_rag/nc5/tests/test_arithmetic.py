import unittest
from nc5.checks import deterministic

def table(columns):
    return {'id':'d','blocks':[{'document':'d','locator':'p'+str(i),'text':text,'table_context':{'table':1,'row':2,'column':i,'column_name':label}} for i,(label,text) in enumerate(columns,1)]}

class Arithmetic(unittest.TestCase):
    def test_vat_basis_and_column_groups(self):
        columns=[('Документ / Количество','3'),('Документ / Цена с НДС, руб.','15,50'),('Документ / Сумма без НДС, руб.','42,27'),('Документ / Сумма НДС, руб.','4,23'),('Документ / Сумма всего с НДС, руб.','46,50')]
        self.assertEqual(deterministic(table(columns)),[])
        wrong=columns[:-1]+[(columns[-1][0],'15,50')];f=deterministic(table(wrong));self.assertEqual(len(f),1)
        self.assertEqual({e['locator'] for e in f[0]['evidence']},{'p1','p2','p5'})
        other=[('Учёт / Количество','2'),('Учёт / Цена без НДС, руб.','20'),('Учёт / Сумма без НДС, руб.','40')]
        self.assertEqual(len(deterministic(table(wrong+other))),1)
    def test_mixed_tax_basis_is_not_multiplied(self):
        self.assertEqual(deterministic(table([('Количество','3'),('Цена без НДС','10'),('Сумма с НДС','36')])),[])
    def test_currency_and_scale(self):
        self.assertEqual(deterministic(table([('Количество','2'),('Цена, руб.','1500'),('Сумма, тыс. руб.','3')])),[])
        self.assertEqual(deterministic(table([('Количество','2'),('Цена, USD','1500'),('Сумма, руб.','3')])),[])
