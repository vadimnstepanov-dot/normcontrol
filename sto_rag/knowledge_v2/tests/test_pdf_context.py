import unittest
from knowledge_v2.pdf_context import adjacent_notes
from knowledge_v2.tables import cell,finalize


class Page:
    width=500;height=800;page_number=1
    def __init__(self,label='Примечание — Допустимо только при резервировании.'):
        self.words=[dict(text=label,x0=50,x1=400,top=210,bottom=222),
                    dict(text='Исключение: аварийный режим.',x0=50,x1=350,top=225,bottom=237),
                    dict(text='2 Следующий раздел',x0=50,x1=300,top=260,bottom=272)]
    def extract_words(self):return self.words


class PDFContextTests(unittest.TestCase):
    def table(self):return finalize(dict(id='page/1/table/1',rows=2,columns=1,header_rows=[1],header_basis='expert',bbox=[.1,.1,.9,.25],
        cells=[cell('a',1,1,'Header'),cell('b',2,1,'≤ 3')]))
    def test_note_includes_exception_not_next_section(self):
        t=self.table();notes=adjacent_notes(Page(),t,[t])
        self.assertEqual(len(notes),1);self.assertIn('Исключение',notes[0]['exact_text']);self.assertNotIn('Следующий',notes[0]['exact_text'])
        self.assertEqual(t['cells'][1]['note_bindings'][0]['exact_text'],notes[0]['exact_text'])
    def test_arbitrary_paragraph_is_not_table_note(self):
        t=self.table();self.assertEqual(adjacent_notes(Page('Требования другого раздела'),t,[t]),[])
    def test_distant_note_is_not_linked(self):
        p=Page();p.words[0]['top']=245;t=self.table();self.assertEqual(adjacent_notes(p,t,[t]),[])

if __name__=='__main__':unittest.main()
