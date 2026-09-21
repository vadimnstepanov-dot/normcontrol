import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from nc5.conversion import OLE_SIGNATURE,inspect_legacy,prepare_word


class LegacyWordConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source=Path(self.tmp.name)/'legacy.doc';self.source.write_bytes(OLE_SIGNATURE+b'legacy-word-fixture')

    def test_binary_signature_is_required(self):
        self.assertEqual(inspect_legacy(self.source),self.source.resolve())
        self.source.write_bytes(b'not-word')
        with self.assertRaisesRegex(ValueError,'двоичным документом Word'):inspect_legacy(self.source)

    def test_conversion_result_is_inspected_and_cached(self):
        with tempfile.TemporaryDirectory() as cache:
            def convert(command,**kwargs):
                destination=Path(command[command.index('-Destination')+1]);Document().save(destination)
            with patch('nc5.conversion.DATA',Path(cache)),patch('nc5.conversion.subprocess.run',side_effect=convert) as run:
                first=prepare_word(self.source);second=prepare_word(self.source)
            self.assertEqual(first,second);self.assertEqual(run.call_count,1);self.assertTrue(first.exists())


if __name__=='__main__':unittest.main()
