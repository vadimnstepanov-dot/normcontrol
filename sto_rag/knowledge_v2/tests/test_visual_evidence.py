import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch,Mock
from knowledge_v2.visual_evidence import validate_visual,render_visuals
from knowledge_v2.structure import Cache


class VisualTests(unittest.TestCase):
    def test_dangling_relation_and_invalid_box_rejected(self):
        value=dict(kind='diagram',nodes=[dict(id='A',text='A',bbox=[.1,.1,.2,.2])],relations=[],constraints=[],confidence=.9)
        validate_visual(value)
        with self.assertRaises(ValueError):validate_visual(dict(value,relations=[dict(source='A',target='B',direction='forward',bbox=[0,0,1,1])]))
        with self.assertRaises(ValueError):validate_visual(dict(value,nodes=[dict(id='A',text='A',bbox=[.5,.2,.1,.6])]))

    def test_all_visio_pages_rendered_and_cached_without_rewriting_occurrences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'assets').mkdir();(root/'assets/a.bin').write_bytes(b'test')
            visual=lambda i:dict(id=i,locator=i,sha256='same',original='assets/a.bin',kind='embedded_object',progid='Visio.Drawing',issues=[])
            r=dict(visuals=[visual('p1'),visual('p2')],blocks=[]);cache=Cache(root,{})
            def render(pdf,page,png,**kw):png.write_bytes(b'image')
            with patch('knowledge_v2.structure.office_convert',return_value=root/'x.pdf') as convert,patch('pypdf.PdfReader',return_value=Mock(pages=[1,2,3])),patch('knowledge_v2.structure_pdf.render_page',side_effect=render) as renderer:
                render_visuals(r,root,cache,False,lambda:False)
            self.assertEqual(convert.call_count,1);self.assertEqual(renderer.call_count,3)
            self.assertEqual([v['id'] for v in r['visuals']],['p1','p2'])
            self.assertEqual([p['number'] for p in r['visuals'][1]['pages']],[1,2,3])

    def test_embedded_preview_uses_explicit_link_and_does_not_ocr_twice(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'assets').mkdir();Image.new('RGB',(30,30)).save(root/'assets/a.png')
            r=dict(blocks=[],visuals=[dict(id='doc',locator='p1',kind='embedded_object',method='embedded-docx-xml',state='read'),
                dict(id='im',locator='p1',kind='image',sha256='a',original='assets/a.png',preview_of='doc')])
            with patch('knowledge_v2.document_ocr.RuledOCR.analyze') as ocr:render_visuals(r,root,Cache(root,{}),True,lambda:False)
            self.assertFalse(ocr.called);self.assertEqual(r['visuals'][1]['role'],'embedded_document_preview')
            self.assertEqual(r['visuals'][1]['content_evidence'],'doc');self.assertEqual(r['blocks'],[])

    def test_arrow_crop_keeps_original_and_maps_back_to_source(self):
        from PIL import Image
        from knowledge_v2.visual_relations import review_arrows
        class Client:
            signature='arrow-fixture';calls=0
            def complete(self,policy,data,schema,image=None):
                self.calls+=1
                return dict(value=dict(direction='target_to_source',arrow_bbox=[.1,.2,.2,.3],evidence='visible tip',confidence=.95),seconds=1,usage={})
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'assets').mkdir();Image.new('RGB',(500,300)).save(root/'assets/a.png')
            page=dict(number=1,render='assets/a.png',interpretation=dict(value=dict(kind='diagram',nodes=[
                dict(id='a',text='Server',bbox=[.2,.2,.4,.4]),dict(id='b',text='Client',bbox=[.6,.2,.8,.4])],
                relations=[dict(source='a',target='b',direction='undirected',label='flow',bbox=[.4,.2,.6,.4])],confidence=.9)))
            client=Client();cache=Cache(root,{})
            review_arrows(page,root,cache,client,lambda:False)
            self.assertEqual(page['interpretation']['value']['relations'][0]['direction'],'undirected')
            candidate=page['arrow_reviews'][0]['candidate'];self.assertEqual((candidate['source'],candidate['target']),('b','a'))
            self.assertTrue(all(0<=x<=1 for x in candidate['bbox']));self.assertFalse(candidate['expert_approved'])
            self.assertFalse(candidate['eligible_for_requirement_extraction'])
            self.assertFalse(page['arrow_reviews'][0]['automatic_acceptance'])
            review_arrows(page,root,cache,client,lambda:False);self.assertEqual(client.calls,1)

if __name__=='__main__':unittest.main()
