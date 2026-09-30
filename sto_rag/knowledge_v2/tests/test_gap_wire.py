import copy,unittest
from knowledge_v2.gap_wire import pack,unpack,VERSION

class GapWireTests(unittest.TestCase):
    def roundtrip(self,gaps):
        original=copy.deepcopy(gaps);packed=pack(gaps)
        self.assertEqual(unpack(packed),original)
        self.assertEqual(gaps,original)
        return packed

    def test_numbering_run_preserves_every_number_in_original_order(self):
        gaps=[dict(locator=f'p{n}/numbering',state='needs_review',reason_ref='G2') for n in [3,7,6,7,100]]
        value=self.roundtrip(gaps)
        self.assertEqual(value['encoding'],VERSION)
        self.assertEqual(value['groups'][0]['locators'],dict(prefix='p',numbers=[3,7,6,7,100],suffix='/numbering'))

    def test_mixed_visual_gaps_keep_order_and_unknown_metadata(self):
        gaps=[dict(locator='p3/numbering',state='needs_review',reason_ref='G2'),
              dict(locator='p4/image1',state='unreadable',reason_ref='G3',evidence={'a':[1,None]}),
              dict(locator='p5/numbering',state='needs_review',reason_ref='G2')]
        value=self.roundtrip(gaps)
        self.assertEqual(len(value['groups']),3)
        value['groups'][1]['common']['evidence']['a'].append(2)
        self.assertEqual(gaps[1]['evidence']['a'],[1,None])

    def test_leading_zero_labels_are_not_normalized(self):
        gaps=[dict(locator=f'p{n}/numbering',state='needs_review') for n in ['001','002','003']]
        self.assertIsInstance(self.roundtrip(gaps)['groups'][0]['locators'],list)

    def test_missing_and_null_fields_remain_distinct(self):
        gaps=[dict(locator='p1'),dict(locator='p2',state=None),dict(locator='p3',state='')]
        self.assertEqual(len(self.roundtrip(gaps)['groups']),3)

    def test_unsupported_records_are_preserved_without_partial_conversion(self):
        for gaps in ([],[{'state':'unreadable'}],[{'locator':None}],[{'locator':'p1'},'unknown']):
            self.assertEqual(self.roundtrip(gaps),gaps)

    def test_table_and_image_locators_with_multiple_numbers_are_preserved(self):
        gaps=[dict(locator=loc,reason_ref='G1') for loc in ['p3/image1','t2/r3/c4','p12/image2']]
        self.assertIsInstance(self.roundtrip(gaps)['groups'][0]['locators'],list)
