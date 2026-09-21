import tempfile
import unittest
from pathlib import Path
from nc5.common import write
from nc5.evaluate import evaluate


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.ref=self.root/'ref.json';self.report=self.root/'report.json';self.judgment=self.root/'judgment.json'
        write(self.ref,{'source_sha256':'same','findings':[
            {'id':'R1','status':'confirmed','category':'logic','severity':'major','evidence':[{'locator':'a','quote':'A'},{'locator':'b','quote':'B'}]},
            {'id':'R2','status':'confirmed','category':'language','severity':'minor','evidence':[{'locator':'c','quote':'C'}]}]})
        write(self.report,{'documents':[{'sha256':'same'}],'findings':[
            {'id':'F1','status':'confirmed','issue':'first','evidence':[{'locator':'a','quote':'A'}]},
            {'id':'F2','status':'confirmed','issue':'second','evidence':[{'locator':'b','quote':'B'}]}],'metrics':{}})

    def tearDown(self):self.temp.cleanup()

    def test_composite_match_keeps_reference_denominator(self):
        write(self.judgment,{'reference':{'R1':{'status':'found','findings':['F1','F2'],'comment':'Both independent parts verified'},'R2':{'status':'partial','comment':'Wrong explanation'}},
            'issued':{'F1':{'verdict':'correct','comment':'Verified'},'F2':{'verdict':'partially_correct','comment':'Extra unsupported assertion'}}})
        result=evaluate(self.ref,self.report,self.root/'out.json',self.judgment)
        self.assertEqual(result['found'],1);self.assertEqual(result['recall'],.5)
        self.assertEqual(result['precision'],.5);self.assertEqual(result['by_severity']['major']['recall'],1)

    def test_locator_overlap_does_not_count_as_semantic_detection(self):
        result=evaluate(self.ref,self.report,self.root/'out.json')
        self.assertIsNone(result['recall']);self.assertEqual(result['found'],0)
        self.assertTrue(result['rows'][0]['candidates'])

    def test_non_issued_match_rejected(self):
        write(self.judgment,{'reference':{'R1':{'status':'found','findings':['not-issued'],'comment':'Invalid'}}})
        with self.assertRaises(ValueError):evaluate(self.ref,self.report,self.root/'out.json',self.judgment)


if __name__=='__main__':unittest.main()
