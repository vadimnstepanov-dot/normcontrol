import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from zipfile import ZipFile

from knowledge_v2.ingest import inspect, ingest, parse, sha256
from knowledge_v2.store import KnowledgeStore


def docx(path,paragraph='Правило должно выполняться.',header='Параметр',image=False):
    drawing='<w:drawing/>' if image else ''
    body=f'''<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>1 Требования</w:t></w:r></w:p>
    <w:p><w:r><w:t>{paragraph}</w:t></w:r>{drawing}</w:p>
    <w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>24 часа</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'''
    xml=f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    with ZipFile(path,'w') as z:z.writestr('word/document.xml',xml)
    return path


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=KnowledgeStore(Path(self.tmp.name)/'data')
        self.set_id=str(uuid.uuid4());self.store.register_set(self.set_id,'scope',{'name':'New'})

    def test_docx_exact_quotes_headers_and_unreadable_image(self):
        path=docx(Path(self.tmp.name)/'source.docx',image=True)
        parsed=parse(path)
        self.assertEqual([b['locator'] for b in parsed['blocks']],['p1','p2','t1/r1/c1','t1/r2/c1'])
        self.assertEqual(parsed['blocks'][3]['header_path'],['Параметр'])
        self.assertEqual(parsed['blocks'][1]['exact_text'],'Правило должно выполняться.')
        self.assertTrue(any(x['state']=='unreadable' for x in parsed['coverage']))
        self.assertEqual(parsed['classification']['type'],'unknown')

    def test_idempotent_ingest_revision_and_context_invalidation(self):
        first=docx(Path(self.tmp.name)/'first.docx')
        sid=str(uuid.uuid4());content=first.read_bytes();h=sha256(first)
        result=ingest(self.store,self.set_id,sid,'first.docx',h,io.BytesIO(content))
        self.assertEqual(result['fragment_count'],4)
        self.assertEqual(ingest(self.store,self.set_id,sid,'first.docx',h,io.BytesIO(content)),result)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],5)
        second=docx(Path(self.tmp.name)/'second.docx',paragraph='Иной срок.',header='Параметр')
        new=ingest(self.store,self.set_id,str(uuid.uuid4()),'second.docx',sha256(second),io.BytesIO(second.read_bytes()),sid)
        self.assertGreater(new['reuse']['invalidated_contexts'],0)
        self.assertLess(new['reuse']['unchanged_contexts'],4)

    def test_rejects_traversal_entity_macro_and_checksum(self):
        path=docx(Path(self.tmp.name)/'bad.docx')
        with self.assertRaises(ValueError):inspect(path,'../bad.docx')
        with self.assertRaises(ValueError):ingest(self.store,self.set_id,str(uuid.uuid4()),'bad.docx','a'*64,io.BytesIO(path.read_bytes()))
        with ZipFile(path,'w') as z:z.writestr('word/document.xml','<!DOCTYPE foo><w:document/>')
        with self.assertRaises(ValueError):inspect(path,'bad.docx')
        with ZipFile(path,'w') as z:
            z.writestr('word/document.xml','<w:document/>');z.writestr('word/vbaProject.bin',b'evil')
        with self.assertRaises(ValueError):inspect(path,'bad.docx')

    def test_pdf_unreadable_pages_are_explicit_when_parser_available(self):
        try:from pypdf import PdfWriter
        except ImportError:self.skipTest('pypdf not installed in this interpreter')
        path=Path(self.tmp.name)/'scan.pdf';writer=PdfWriter();writer.add_blank_page(width=300,height=300)
        with path.open('wb') as f:writer.write(f)
        parsed=parse(path)
        self.assertEqual(parsed['counts']['pages'],1)
        self.assertEqual(parsed['coverage'][0]['state'],'unreadable')

    def test_pdf_text_layer_keeps_page_locator(self):
        try:from reportlab.pdfgen import canvas
        except ImportError:self.skipTest('reportlab is not installed')
        path=Path(self.tmp.name)/'text.pdf';c=canvas.Canvas(str(path));c.drawString(40,800,'Applicable requirement');c.save()
        parsed=parse(path)
        self.assertEqual(parsed['blocks'][0]['page'],1)
        self.assertIn('Applicable requirement',parsed['blocks'][0]['exact_text'])
        self.assertFalse(any(x['state']=='unreadable' for x in parsed['coverage']))

    def test_doc_adapter_preserves_original_format_and_parses_converted_copy(self):
        path=Path(self.tmp.name)/'legacy.doc';path.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'+b'1'*32)
        def fake_run(command,**kwargs):
            docx(Path(command[command.index('--outdir')+1])/'source.docx')
        with patch('knowledge_v2.ingest.shutil.which',return_value='soffice'),patch('knowledge_v2.ingest.subprocess.run',side_effect=fake_run):
            parsed=parse(path)
        self.assertEqual(parsed['kind'],'.doc')
        self.assertEqual(parsed['blocks'][1]['exact_text'],'Правило должно выполняться.')
