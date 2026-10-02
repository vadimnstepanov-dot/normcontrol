"""Opt-in release of WSL filesystem cache when live reviews wait for host RAM."""
import json,os,re,subprocess,time
from pathlib import Path

class Reclaimer:
 def __init__(self,policy,coordinator,interval=600):
  self.policy=Path(policy);self.coordinator=coordinator;self.interval=interval;self.last=-float('inf')
 def check(self,available_mb,processing):
  if os.name!='nt' or processing is not False or time.monotonic()-self.last<self.interval:return None
  try:policy=json.loads(self.policy.read_text(encoding='utf8'))
  except (OSError,ValueError):return None
  distro=policy.get('wsl_cache_reclaim_distro')
  if not isinstance(distro,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',distro):return None
  snapshot=getattr(self.coordinator,'resource_snapshot',None)
  if snapshot:
   value=snapshot();tickets=value['queue'];total_mb=value['total_mb']
  else:
   with self.coordinator.db() as db:
    tickets=[dict(r) for r in db.execute('SELECT state,ram_mb,resource FROM tickets WHERE expires>?',(time.time(),))]
   total_mb=self.coordinator.memory_probe()[0]
  if any(r['state']=='running' and r['resource']=='gpu' for r in tickets):return None
  reserved=sum(r['ram_mb'] for r in tickets if r['state']=='running')
  from pipeline import system_reserve_mb
  reserve=system_reserve_mb(total_mb)
  if not any(r['state']=='waiting' and available_mb-reserved-r['ram_mb']<reserve for r in tickets):return None
  self.last=time.monotonic()
  # Only clean filesystem caches are reclaimed. Processes, model and swap stay intact.
  command="awk '/^Cached:|^SReclaimable:/ {n+=$2} END {if(n<524288) exit 1}' /proc/meminfo && sync && echo 3 > /proc/sys/vm/drop_caches"
  try:result=subprocess.run(['wsl.exe','-d',distro,'-u','root','--','sh','-c',command],capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  except (OSError,subprocess.TimeoutExpired):return False
  return result.returncode==0
