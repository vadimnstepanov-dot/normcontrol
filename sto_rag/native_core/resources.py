import time
from pathlib import Path

LINUX_RESERVE_MB=512

def linux_memory():
    values={line.split(':')[0]:int(line.split()[1])//1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    return values['MemTotal'],values['MemAvailable']

def container_available():
    """The process budget includes reclaimable file pages, never its VM's total RAM."""
    total,available=linux_memory()
    root=Path('/sys/fs/cgroup')
    try:
        cap=(root/'memory.max').read_text().strip()
        if cap=='max':return total,available
        limit=int(cap);current=int((root/'memory.current').read_text())
        stats=dict(line.split() for line in (root/'memory.stat').read_text().splitlines())
        reclaim=int(stats.get('inactive_file',0))
        return limit//1048576,min(available,max(0,limit-current+reclaim)//1048576)
    except (OSError,ValueError):return total,available

def effective_available(host_available,linux_available,host_reserve=2048,linux_reserve=LINUX_RESERVE_MB):
    return min(host_available,host_reserve+max(0,linux_available-linux_reserve))

class HostProbe:
    def __init__(self,transport,ttl=1):self.transport=transport;self.ttl=ttl;self.last=0;self.sample=None
    def snapshot(self):
        if time.monotonic()-self.last>self.ttl or self.sample is None:
            value=self.transport.json('/host/resources',timeout=5)
            if not all(isinstance(value.get(key),int) and value[key]>=0 for key in ('total_mb','available_mb')):raise ValueError('Invalid host RAM measurement')
            self.sample=value;self.last=time.monotonic()
        linux_total,linux_available=linux_memory()
        return {**self.sample,'linux_total_mb':linux_total,'linux_available_mb':linux_available,'linux_reserve_mb':LINUX_RESERVE_MB}
    def memory(self):
        value=self.snapshot()
        return value['total_mb'],effective_available(value['available_mb'],value['linux_available_mb'])
