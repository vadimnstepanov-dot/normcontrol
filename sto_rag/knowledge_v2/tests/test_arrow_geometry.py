import importlib.util
import copy
import tempfile
import unittest
from pathlib import Path
from knowledge_v2.arrow_geometry import verify_relations

@unittest.skipUnless(importlib.util.find_spec('cv2'),'OpenCV is provided by document/worker Docker runtime')
class ArrowTests(unittest.TestCase):
    def draw(self,direction,head=True):
        import cv2,numpy as np
        im=np.full((200,400,3),245,np.uint8)
        a=(70,100);b=(330,100)
        cv2.rectangle(im,(20,60),(70,140),(0,0,0),1);cv2.rectangle(im,(330,60),(380,140),(0,0,0),1)
        cv2.line(im,a,b,(0,0,0),1)
        if head:
            points=[[330,100],[321,95],[321,105]] if direction=='right' else [[70,100],[79,95],[79,105]]
            cv2.fillPoly(im,[np.array(points)],(0,0,0))
        return im

    def page(self):return dict(interpretation=dict(value=dict(kind='diagram',nodes=[
        dict(id='a',text='Any label',bbox=[.05,.3,.175,.7]),dict(id='b',text='Other label',bbox=[.825,.3,.95,.7])],
        relations=[dict(source='a',target='b',direction='undirected',label='',bbox=[.175,.45,.825,.55])])))

    def test_bidirectional_orientation_and_plain_line(self):
        import cv2
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.png'
            for orientation,expected in [('right',('a','b')),('left',('b','a'))]:
                cv2.imwrite(str(path),self.draw(orientation));p=self.page();result=verify_relations(p,path)
                self.assertEqual(len(result),1)
                self.assertEqual((result[0]['candidate']['source'],result[0]['candidate']['target']),expected)
                self.assertFalse(result[0]['candidate']['expert_approved'])
            cv2.imwrite(str(path),self.draw('right',False))
            self.assertEqual(verify_relations(self.page(),path),[])

    def test_rotation_does_not_invert_direction(self):
        import cv2
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rotated.png';im=self.draw('right');page=self.page()
            for _ in range(4):
                cv2.imwrite(str(path),im);result=verify_relations(copy.deepcopy(page),path)
                self.assertEqual(len(result),1)
                self.assertEqual((result[0]['candidate']['source'],result[0]['candidate']['target']),('a','b'))
                im=cv2.rotate(im,cv2.ROTATE_90_CLOCKWISE)
                for item in page['interpretation']['value']['nodes']+page['interpretation']['value']['relations']:
                    a,b,c,d=item['bbox'];item['bbox']=[1-d,a,1-b,c]

if __name__=='__main__':unittest.main()
