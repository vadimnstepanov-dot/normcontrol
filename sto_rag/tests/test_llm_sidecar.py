import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

import llm_sidecar
from llm_sidecar import Degradation,THRESHOLD,wait_for_checkpoint,resume
from nc5.store import Store


class SidecarTests(unittest.TestCase):
    def test_slot_activity_uses_actual_server_processing_state(self):
        with patch.object(llm_sidecar.urllib.request,'urlopen',return_value=io.BytesIO(b'[{"is_processing":false},{"is_processing":true}]')):
            self.assertIs(llm_sidecar.slot_activity(),True)
        with patch.object(llm_sidecar.urllib.request,'urlopen',return_value=io.BytesIO(b'[{"is_processing":false}]')):
            self.assertIs(llm_sidecar.slot_activity(),False)
        with patch.object(llm_sidecar.urllib.request,'urlopen',return_value=io.BytesIO(b'[]')):
            self.assertIsNone(llm_sidecar.slot_activity())

    def test_sampling_interval_and_manual_stop_start_keep_checkpoint(self):
        self.assertEqual(llm_sidecar.INTERVAL,60)
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'review.sqlite3')
            jid=store.create({'test':True})
            with patch.object(llm_sidecar,'STATE',Path(folder)/'control.json'), \
                 patch.object(llm_sidecar,'backend',return_value=True), \
                 patch.object(llm_sidecar,'stop_backend'), \
                 patch.object(llm_sidecar,'start_backend'):
                paused=llm_sidecar.control('stop',store,[])
                self.assertEqual(paused,[jid])
                self.assertEqual(store.job(jid)['state'],'paused')
                self.assertEqual(llm_sidecar.control('start',store,paused),[])
                self.assertEqual(store.job(jid)['state'],'running')

    def test_degradation_requires_both_rates_and_sustained_comparable_work(self):
        detector=Degradation()
        now=10000
        healthy={'stage':'sto','prompt_n':12000,'predicted_n':300,'prefill_tps':1000,'generation_tps':60}
        for i in range(5):self.assertFalse(detector.observe(healthy,now+i))
        # One slow request and a different-sized stage must never restart a model.
        slow={**healthy,'prefill_tps':800,'generation_tps':48}
        self.assertFalse(detector.observe(slow,now+20))
        self.assertFalse(detector.observe({**slow,'stage':'logic'},now+21))
        for i in range(2):self.assertFalse(detector.observe(slow,now+22+i))
        self.assertTrue(detector.observe(slow,now+24))
        detector.restarted(now+25)
        self.assertFalse(detector.observe(slow,now+26))
        self.assertEqual(THRESHOLD,.85)

    def test_checkpoint_pauses_and_resumes_same_job(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'review.sqlite3')
            jid=store.create({'test':True})
            paused=wait_for_checkpoint(store,timeout=2)
            self.assertEqual(paused,[jid])
            self.assertEqual(store.job(jid)['state'],'paused')
            resume(store,paused)
            self.assertEqual(store.job(jid)['state'],'running')
            self.assertEqual(len(store.jobs()),1)


if __name__=='__main__':unittest.main()
