import json,tempfile,unittest,zipfile
from pathlib import Path
from nc5.maintenance import backup,restore
from nc5.store import Store

class BackupTests(unittest.TestCase):
    def test_restore_queue_hashes_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';store=Store(data/'review.sqlite3');jid=store.create({'scope':'test'})
            source=data/'jobs'/jid/'source.txt';source.parent.mkdir(parents=True);source.write_text('Исходные данные',encoding='utf-8')
            archive=root/'copy.zip';backup(archive,data);restored=root/'restored';restore(archive,restored)
            self.assertEqual(Store(restored/'review.sqlite3').job(jid)['data'],{'scope':'test'})
            self.assertEqual((restored/source.relative_to(data)).read_bytes(),source.read_bytes())
            with self.assertRaises(ValueError):restore(archive,restored)
            with zipfile.ZipFile(root/'unsafe.zip','w') as z:z.writestr('backup-manifest.json',json.dumps({'version':1,'files':{'../outside':'bad'}}))
            with self.assertRaises(ValueError):restore(root/'unsafe.zip',root/'target')
