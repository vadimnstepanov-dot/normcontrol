import tempfile
import unittest
import subprocess
import sys
import time
from pathlib import Path
from nc5.runtime import Lease,BusyError,SleepInhibitor,ES_CONTINUOUS,ES_SYSTEM_REQUIRED
from nc5.store import Store

class RuntimeTests(unittest.TestCase):
    def test_sleep_inhibitor_holds_and_releases_on_worker_thread(self):
        calls=[]
        class Kernel:
            def SetThreadExecutionState(self,flags):calls.append(flags);return 1
        request=SleepInhibitor();request.kernel32=Kernel()
        with request:self.assertTrue(request.held)
        self.assertEqual(calls,[ES_CONTINUOUS|ES_SYSTEM_REQUIRED,ES_CONTINUOUS])
    def test_process_death_preserves_commits_and_rolls_back_partial_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=root/'state.sqlite3';ready=root/'ready'
            child=r'''
import sys,time
from pathlib import Path
from nc5.store import Store
from nc5.runtime import Lease
db,ready=sys.argv[1:]
s=Store(db);j=s.create({});s.update(j,'running')
with Lease(db+'.worker.lock'):
    s.add(j,'logic',{'part':1});s.add(j,'logic',{'part':2})
    t=s.claim(j,'logic');s.cache('committed-response',{'raw':{'findings':[]}});s.finish(t,{'saved':True})
    s.claim(j,'logic')
    with s.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE jobs SET state='completed' WHERE id=?",(j,))
        Path(ready).write_text(j)
        time.sleep(30)
'''
            p=subprocess.Popen([sys.executable,'-c',child,str(db),str(ready)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                until=time.monotonic()+10
                while not ready.exists() and p.poll() is None and time.monotonic()<until:time.sleep(.05)
                self.assertTrue(ready.exists(),'Child did not reach the uncommitted transaction')
                jid=ready.read_text();p.kill();p.wait(timeout=5)
                s=Store(db)
                self.assertEqual(s.job(jid)['state'],'running')
                with Lease(str(db)+'.worker.lock'):s.recover(jid)
                self.assertEqual([t['state'] for t in s.tasks(jid)],['done','pending'])
                self.assertIsNotNone(s.cached('committed-response'))
                self.assertEqual(s.job(jid)['state'],'paused')
            finally:
                if p.poll() is None:p.kill()
                p.communicate(timeout=5)

    def test_lock_releases_and_recovery_is_scoped(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'worker.lock'
            with Lease(path):
                with self.assertRaises(BusyError):
                    with Lease(path):pass
            with Lease(path):pass
            store=Store(Path(d)/'state.sqlite3');a=store.create({});b=store.create({})
            for jid in (a,b):store.update(jid,'running');store.add(jid,'logic',{});store.claim(jid,'logic')
            store.recover(a)
            self.assertEqual(store.job(a)['state'],'paused');self.assertEqual(store.tasks(a)[0]['state'],'pending')
            self.assertEqual(store.job(b)['state'],'running');self.assertEqual(store.tasks(b)[0]['state'],'running')
    def test_duplicate_runner_does_not_pause_owner(self):
        from nc5.engine import Engine
        from nc5.common import DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'state.sqlite3');jid=store.create({});store.update(jid,'running')
            with Lease(store.path+'.worker.lock'):
                Engine(dict(DEFAULT_CONFIG),store).run(jid)
            self.assertEqual(store.job(jid)['state'],'running')
