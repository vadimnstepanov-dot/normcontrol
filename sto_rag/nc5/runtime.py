"""OS-backed leases release on process death; database heartbeats are diagnostic only."""
import os
import ctypes
from pathlib import Path
from .common import digest

class BusyError(RuntimeError):pass

class Lease:
    def __init__(self,path):self.path=Path(path);self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        f=self.path.open('a+b');f.seek(0,2)
        if not f.tell():f.write(b'0');f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            f.close();raise BusyError('Обработчик уже занят в другом процессе')
        self.file=f;return self
    def __exit__(self,*args):
        if self.file:self.file.close();self.file=None

ES_CONTINUOUS=0x80000000
ES_SYSTEM_REQUIRED=0x00000001

class SleepInhibitor:
    """Hold Windows system sleep for the lifetime of the calling worker thread."""
    def __init__(self):self.held=False;self.kernel32=ctypes.windll.kernel32 if os.name=='nt' else None
    def __enter__(self):
        if self.kernel32:
            if not self.kernel32.SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED):raise ctypes.WinError()
            self.held=True
        return self
    def pulse(self):
        """Reset the system idle timer once; suitable for a UI heartbeat."""
        if not self.kernel32:return False
        if not self.kernel32.SetThreadExecutionState(ES_SYSTEM_REQUIRED):raise ctypes.WinError()
        return True
    def __exit__(self,*args):
        if self.held and self.kernel32:self.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.held=False

def wake_once():return SleepInhibitor().pulse()

def implementation_hash():
    folder=Path(__file__).parent
    return digest({p.name:digest(p.read_bytes()) for p in sorted(folder.glob('*.py')) if not p.name.startswith('benchmark_') and p.name not in ('evaluate.py','fixtures.py','scale_test.py','maintenance.py')})
