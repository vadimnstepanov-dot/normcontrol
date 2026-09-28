import tempfile
import unittest
import io
import json
import urllib.error
import os
import subprocess
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

import llm_sidecar
from llm_sidecar import Degradation,THRESHOLD,wait_for_checkpoint,resume
from nc5.store import Store


class SidecarTests(unittest.TestCase):
    def test_model_label_stops_at_parameter_count_without_exposing_paths(self):
        from unittest.mock import Mock
        for filename,wanted in [('Qwen3.8-27B-UD-Q4_K_S.gguf','Qwen3.8-27B'),
                                ('Swift-Qwen3.8-27B-Abliterated.i1.gguf','Swift-Qwen3.8-27B'),
                                ('Qwen3.8-9b-Distill.gguf','Qwen3.8-9b')]:
            p=Mock();p.cmdline.return_value=['llama-server','-m','C:\\private\\'+filename,'--mmproj','vision.gguf']
            self.assertEqual(llm_sidecar.model_label(p),wanted)

    def test_v2_production_counter_reads_only_new_finite_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'timing.json'
            sample=dict(source='knowledge-v2',stage='check',ended=123,prompt_n=100,predicted_n=30,prefill_tps=900,generation_tps=60)
            path.write_text(json.dumps(sample))
            with patch.dict(os.environ,{'NORMCONTROL_KNOWLEDGE_TELEMETRY':str(path)}):
                self.assertEqual(llm_sidecar.latest_v2_timing(122),sample)
                self.assertIsNone(llm_sidecar.latest_v2_timing(123))
                path.write_text(json.dumps(dict(sample,generation_tps=float('nan'))))
                self.assertIsNone(llm_sidecar.latest_v2_timing(0))

    @unittest.skipUnless(os.name=='nt','Windows console integration')
    def test_windows_graceful_shutdown_isolated_console(self):
        with tempfile.TemporaryDirectory() as folder:
            ready=Path(folder)/'ready';stopped=Path(folder)/'stopped'
            # Tool hosts can pass their ignored Ctrl+C attribute to children.
            # Enable Ctrl+C in this isolated fake server before testing delivery.
            code="import signal,time,sys,ctypes;from pathlib import Path\nctypes.windll.kernel32.SetConsoleCtrlHandler(None,False)\ndef stop(*a):\n Path(sys.argv[2]).write_text('graceful');sys.exit(0)\nsignal.signal(signal.SIGINT,stop)\nPath(sys.argv[1]).write_text('ready')\nwhile True:time.sleep(.1)"
            startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
            process=subprocess.Popen([sys._base_executable,'-c',code,str(ready),str(stopped)],creationflags=subprocess.CREATE_NEW_CONSOLE,startupinfo=startup)
            try:
                deadline=time.monotonic()+10
                while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue(ready.exists())
                result=subprocess.run([sys.executable,str(Path(llm_sidecar.__file__).with_name('llm_console_signal.py')),str(process.pid)],creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
                self.assertEqual(result.returncode,0)
                self.assertEqual(process.wait(10),0)
                self.assertEqual(stopped.read_text(),'graceful')
            finally:
                if process.poll() is None:process.terminate();process.wait(5)

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
                 patch.object(llm_sidecar,'start_backend'), \
                 patch.object(llm_sidecar,'ready',return_value=True):
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
            with patch.object(llm_sidecar,'STATE',Path(folder)/'control.json'):
                paused=wait_for_checkpoint(store,timeout=2)
                self.assertEqual(paused,[jid])
                self.assertEqual(store.job(jid)['state'],'paused')
                resume(store,paused)
            self.assertEqual(store.job(jid)['state'],'running')
            self.assertEqual(len(store.jobs()),1)

    def test_loading_is_not_ready(self):
        error=urllib.error.HTTPError('http://localhost',503,'loading',{},None)
        with patch.object(llm_sidecar,'backend',return_value=True),patch.object(llm_sidecar.urllib.request,'urlopen',side_effect=error):
            self.assertFalse(llm_sidecar.ready())

    def test_restart_holds_engine_lease_until_ready_and_preserves_results(self):
        from nc5.runtime import Lease,BusyError
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'review.sqlite3');jid=store.create({'test':True})
            store.update(jid,'running');tid=store.add(jid,'sto',{'number':1})
            task=store.claim(jid,'sto');store.finish(task,{'evidence':'saved'})
            pending=store.add(jid,'sto',{'number':2})
            manual=store.create({});store.update(manual,'paused')
            def assert_locked():
                with self.assertRaises(BusyError):
                    with Lease(store.path+'.worker.lock'):pass
                self.assertEqual(store.job(jid)['state'],'paused')
            with patch.object(llm_sidecar,'STATE',Path(folder)/'control.json'),patch.object(llm_sidecar,'stop_backend',side_effect=assert_locked),patch.object(llm_sidecar,'start_backend',side_effect=assert_locked),patch.object(llm_sidecar,'ready',return_value=True):
                self.assertEqual(llm_sidecar.control('restart',store,[]),[])
            self.assertEqual(store.job(jid)['state'],'running')
            self.assertEqual(store.job(manual)['state'],'paused')
            self.assertEqual([(t['id'],t['state']) for t in store.tasks(jid)],[(tid,'done'),(pending,'pending')])
            self.assertEqual(store.tasks(jid)[0]['result'],{'evidence':'saved'})
            with Lease(store.path+'.worker.lock'):pass

    def test_failed_start_keeps_owned_pause_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'review.sqlite3');jid=store.create({})
            state=Path(folder)/'control.json'
            with patch.object(llm_sidecar,'STATE',state),patch.object(llm_sidecar,'stop_backend'),patch.object(llm_sidecar,'start_backend',side_effect=TimeoutError('loading')):
                with self.assertRaises(TimeoutError):llm_sidecar.control('restart',store,[])
            self.assertEqual(store.job(jid)['state'],'paused')
            remembered=json.loads(state.read_text())['resume_jobs']
            self.assertEqual(remembered,[jid])
            with patch.object(llm_sidecar,'STATE',state),patch.object(llm_sidecar,'start_backend'),patch.object(llm_sidecar,'ready',return_value=True):
                llm_sidecar.control('start',store,remembered)
            self.assertEqual(store.job(jid)['state'],'running')

    def test_inflight_answer_commits_before_backend_stop(self):
        from nc5.runtime import Lease
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'review.sqlite3');jid=store.create({});store.update(jid,'running')
            store.add(jid,'sto',{'part':1});task=store.claim(jid,'sto')
            acquired=threading.Event();committed=threading.Event()
            def complete_answer():
                with Lease(store.path+'.worker.lock'):
                    acquired.set()
                    deadline=time.monotonic()+10
                    while store.job(jid)['state']!='paused' and time.monotonic()<deadline:time.sleep(.01)
                    store.finish(task,{'text':'complete answer'})
                    committed.set()
            worker=threading.Thread(target=complete_answer);worker.start();self.assertTrue(acquired.wait(5))
            def stop():
                self.assertTrue(committed.is_set())
                self.assertEqual(store.tasks(jid)[0]['result'],{'text':'complete answer'})
            try:
                with patch.object(llm_sidecar,'STATE',Path(folder)/'control.json'),patch.object(llm_sidecar,'stop_backend',side_effect=stop),patch.object(llm_sidecar,'start_backend'),patch.object(llm_sidecar,'ready',return_value=True):
                    llm_sidecar.control('restart',store,[])
                self.assertEqual(store.job(jid)['state'],'running')
                self.assertEqual(store.tasks(jid)[0]['attempts'],1)
            finally:worker.join(12)

    def test_user_pause_or_cancel_during_restart_is_not_overridden(self):
        for action in ('paused','cancelled'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder)/'review.sqlite3');jid=store.create({})
                with patch.object(llm_sidecar,'STATE',Path(folder)/'control.json'),patch.object(llm_sidecar,'stop_backend'),patch.object(llm_sidecar,'start_backend',side_effect=lambda:store.update(jid,action)),patch.object(llm_sidecar,'ready',return_value=True):
                    llm_sidecar.control('restart',store,[])
                self.assertEqual(store.job(jid)['state'],action)

    def test_checkpoint_timeout_never_stops_model(self):
        from nc5.runtime import Lease
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'review.sqlite3');jid=store.create({})
            real_wait=llm_sidecar.wait_for_checkpoint
            def immediate(*args,**kwargs):return real_wait(*args,timeout=.01,**kwargs)
            with patch.object(llm_sidecar,'STATE',Path(folder)/'control.json'),patch.object(llm_sidecar,'wait_for_checkpoint',side_effect=immediate),patch.object(llm_sidecar.time,'sleep'),patch.object(llm_sidecar,'stop_backend') as stop,Lease(store.path+'.worker.lock'):
                with self.assertRaises(TimeoutError):llm_sidecar.control('restart',store,[])
                stop.assert_not_called()
            self.assertEqual(store.job(jid)['state'],'paused')


if __name__=='__main__':unittest.main()
