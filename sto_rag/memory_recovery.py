"""Bounded recovery for idle RAM stalls and confirmed model OOM crashes."""
import json,time
from pathlib import Path

class MemoryRecovery:
 def __init__(self,state,coordinator,log,grace=300,cooldown=1800):
  self.state=Path(state);self.coordinator=coordinator;self.log=Path(log)
  self.grace=grace;self.cooldown=cooldown;self.since=None
 def observe(self,available_mb,processing,online,stopped=False,now=None):
  now=time.time() if now is None else now
  if stopped or processing is not False:self.since=None;return None
  snapshot=getattr(self.coordinator,'resource_snapshot',None)
  if not snapshot:return None
  sample=snapshot();tickets=sample['queue']
  if any(t['state']=='running' and t['resource']=='gpu' for t in tickets):self.since=None;return None
  waiting=[t for t in tickets if t['state']=='waiting']
  if not waiting:self.since=None;return None
  reason=None;event=None
  if not online and self.log.exists():
   stamp=self.log.stat().st_mtime
   if 0<=now-stamp<600:
    with self.log.open('rb') as stream:
     stream.seek(max(0,self.log.stat().st_size-16384));tail=stream.read().decode('utf8','replace').lower()
    if any(p in tail for p in ('out of memory','cudaerrormemoryallocation','failed to allocate','cannot allocate memory')):
     reason='Подтверждённая ошибка выделения памяти модели';event=stamp
  from pipeline import system_reserve_mb
  reserved=sum(t.get('ram_mb',0) for t in tickets if t['state']=='running')
  reserve=system_reserve_mb(sample['total_mb'])
  pressure=online and any(available_mb-reserved-t.get('ram_mb',512)<reserve for t in waiting)
  if pressure:
   if self.since is None:self.since=now
   if now-self.since>=self.grace:reason='Ожидание памяти более пяти минут при простаивающей модели'
  else:self.since=None
  if not reason:return None
  try:saved=json.loads(self.state.read_text(encoding='utf8'))
  except FileNotFoundError:saved={}
  attempts=[t for t in saved.get('attempts',[]) if 0<=now-t<3600]
  if attempts and now-attempts[-1]<self.cooldown or len(attempts)>=2 or event is not None and saved.get('oom_event')==event:return None
  # Record attempts before executing, so a failed restart cannot loop.
  from nc5.common import write
  write(self.state,{'attempts':attempts+[now],'oom_event':event,'reason':reason})
  self.since=None
  return reason
