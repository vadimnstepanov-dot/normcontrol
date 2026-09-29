from types import SimpleNamespace
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from django.test import SimpleTestCase
from portal.report_export import make_docx,make_xlsx

class TemplateExportTests(SimpleTestCase):
    def test_template_table_present_and_xml_valid_in_both_exports(self):
        batch=SimpleNamespace(name='Пример',get_status_display=lambda:'Завершено')
        run=SimpleNamespace(report={'template_comparison':{'documents':[{'document_name':'ОИТ','rows':[{'template_element':'1 Общие положения','document_element':'Раздел 1','result':'Заголовок найден','content_result':'Требует проверки'}]}]}})
        for fn,needle in [(make_xlsx,'xl/worksheets/sheet2.xml'),(make_docx,'word/document.xml')]:
            data=fn(batch,run,[],[],[],'')
            with ZipFile(BytesIO(data)) as z:
                for name in z.namelist():
                    if name.endswith(('.xml','.rels')):ET.fromstring(z.read(name))
                text=z.read(needle).decode()
                for title in ['Элемент шаблона','Элемент документа','Результат проверки структуры','Требует проверки']:self.assertIn(title,text)
        with ZipFile(BytesIO(make_xlsx(batch,run,[],[],[],''))) as z:
            self.assertIn('Шаблон СТО',z.read('xl/workbook.xml').decode())
            self.assertIn('rId5',z.read('xl/_rels/workbook.xml.rels').decode())
