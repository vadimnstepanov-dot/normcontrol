"""Low-overhead desktop telemetry and checkpoint-safe llama.cpp control.

Runs beside (not inside) nc5 so updating it cannot invalidate a live job's
implementation hash. The VPS receives only bounded counters, never paths or text.
"""
import json
import math
import os
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

from nc5.common import DATA,write
from nc5.runtime import BusyError,Lease
from nc5.store import Store

ROOT=Path(__file__).resolve().parents[1]
MODEL_EXE=Path(os.getenv('NORMCONTROL_LLAMA_EXE','C:/LM/llama/llama-server.exe'))
LAUNCHER=Path(os.getenv('NORMCONTROL_LLAMA_LAUNCHER','C:/LM/llama/start_llama.bat'))
MODEL_PORT=os.getenv('NORMCONTROL_LLAMA_PORT','8082')
STATE=DATA/'llm-sidecar-state.json'
INTERVAL=60
THRESHOLD=.85  # 15% sustained loss against comparable production requests
COOLDOWN=3600


def credentials(envfile):
    allowed={}
    for line in Path(envfile).read_text(encoding='utf-8-sig').splitlines():
        key,separator,value=line.partition('=')
        if separator and key in ('NORMCONTROL_WORKER_TOKEN','NORMCONTROL_PORTAL_URL'):allowed[key]=value.strip()
    url=allowed.get('NORMCONTROL_PORTAL_URL','').rstrip('/')
    if not url.startswith(('https://','http://127.0.0.1:','http://localhost:')):raise ValueError('HTTPS required')
    if len(allowed.get('NORMCONTROL_WORKER_TOKEN',''))<32:raise ValueError('Worker token missing')
    return url,allowed['NORMCONTROL_WORKER_TOKEN']


def backend():
    for process in psutil.process_iter(['name','exe','cmdline']):
        try:
            if (process.info['name'] or '').casefold() not in ('llama-server.exe','llama-server'):continue
            if Path(process.info['exe'] or '').resolve()!=MODEL_EXE.resolve():continue
            args=process.info['cmdline'] or []
            if any(args[i]=='--port' and i+1<len(args) and args[i+1]==MODEL_PORT for i in range(len(args))):return process
        except (psutil.AccessDenied,psutil.NoSuchProcess,OSError):continue
    return None


def ready():
    if not backend():return False
    try:
        with urllib.request.urlopen('http://127.0.0.1:'+MODEL_PORT+'/health',timeout=2) as response:
            return json.load(response).get('status')=='ok'
    except urllib.error.HTTPError as error:
        return error.code==503  # llama.cpp may be busy while serving a request
    except (OSError,ValueError):return False


def gpu():
    try:
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        raw=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,memory.total,utilization.gpu',
            '--format=csv,noheader,nounits'],timeout=4,creationflags=flags,text=True)
        used,total,percent=(float(x.strip()) for x in raw.splitlines()[0].split(',')[:3])
        return used,total,percent
    except (OSError,ValueError,subprocess.SubprocessError,IndexError):return None,None,None


def latest_timing(db,last_ended):
    if not db.exists():return None
    with sqlite3.connect(db,timeout=2) as connection:
        row=connection.execute("SELECT ended,stage,result FROM tasks WHERE state='done' AND ended>? AND result IS NOT NULL ORDER BY ended DESC LIMIT 1",(last_ended,)).fetchone()
    if not row:return None
    ended,stage,raw=row
    try:
        result=json.loads(raw)
        if result.get('cached'):return {'ended':ended}
        timing=result.get('metrics',{}).get('timings',{})
        prompt_n=float(timing.get('prompt_n') or 0)
        predicted_n=float(timing.get('predicted_n') or 0)
        prefill=float(timing.get('prompt_per_second') or 0)
        generation=float(timing.get('predicted_per_second') or 0)
        if not all(math.isfinite(x) for x in (prompt_n,predicted_n,prefill,generation)):
            return {'ended':ended}
        return {'ended':ended,'stage':stage,'prompt_n':prompt_n,'predicted_n':predicted_n,
            'prefill_tps':round(prefill,2),'generation_tps':round(generation,2)}
    except (ValueError,TypeError,KeyError):return {'ended':ended}


def bucket(item):
    # Compare only same stage and input-size band of about 25%.
    return item['stage'],int(math.log(max(item['prompt_n'],1),1.25))


class Degradation:
    def __init__(self):
        self.baselines={};self.recent={};self.last_restart=0
    def observe(self,item,now):
        if not item or item.get('prompt_n',0)<200 or item.get('predicted_n',0)<96:return False
        key=bucket(item); pair=(item['prefill_tps'],item['generation_tps'])
        if min(pair)<=0:return False
        reference=self.baselines.setdefault(key,[])
        if len(reference)<5:
            reference.append(pair)
            return False
        history=self.recent.setdefault(key,[])
        history.append(pair)
        del history[:-4]
        if len(history)<4 or now-self.last_restart<COOLDOWN:return False
        base_prefill=statistics.median(x[0] for x in reference)
        base_generation=statistics.median(x[1] for x in reference)
        current_prefill=statistics.median(x[0] for x in history)
        current_generation=statistics.median(x[1] for x in history)
        # Both rates must fall: short outputs, cache reuse and complex schemas
        # otherwise produce false alarms in a heterogeneous document pipeline.
        return current_prefill<base_prefill*THRESHOLD and current_generation<base_generation*THRESHOLD
    def restarted(self,now):
        self.baselines.clear();self.recent.clear();self.last_restart=now


def wait_for_checkpoint(store,timeout=900):
    """Pause current jobs, wait for the in-flight answer to be saved and engine lease freed."""
    with store.connect() as connection:
        ids=[row[0] for row in connection.execute("SELECT id FROM jobs WHERE state IN ('running','preparing')")]
    for jid in ids:store.update(jid,'paused')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        with store.connect() as connection:
            busy=connection.execute("SELECT count(*) FROM tasks WHERE state='running' AND job IN (SELECT id FROM jobs WHERE state='paused')").fetchone()[0]
        if not busy:
            try:
                with Lease(str(store.path)+'.worker.lock'):return ids
            except BusyError:pass
        time.sleep(2)
    for jid in ids:
        if store.job(jid)['state']=='paused':store.update(jid,'running')
    raise TimeoutError('Не удалось дождаться контрольной точки; модель не остановлена')


def stop_backend():
    process=backend()
    if not process:return
    # The existing context-guard runner notices backend exit and closes its
    # own child. Do not kill the user's terminal or unrelated Python workers.
    runner=process.parent()
    process.terminate()
    try:process.wait(20)
    except psutil.TimeoutExpired:process.kill();process.wait(10)
    if runner and runner.is_running():
        try:runner.wait(15)
        except psutil.TimeoutExpired:raise RuntimeError('Context Guard не завершился')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and backend():time.sleep(.5)
    if backend():raise RuntimeError('Сервер модели не остановился')


def start_backend():
    if ready():return
    if backend():
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            if ready():return
            time.sleep(2)
        raise TimeoutError('Модель уже запущена, но не готова; второй экземпляр не создаётся')
    if not LAUNCHER.exists():raise FileNotFoundError('Не найден локальный скрипт запуска модели')
    command=['cmd.exe','/c',str(LAUNCHER)] if os.name=='nt' else [str(LAUNCHER)]
    subprocess.Popen(command,cwd=str(LAUNCHER.parent),
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        if ready():return
        time.sleep(2)
    raise TimeoutError('Модель не запустилась за 3 минуты')


def resume(store,ids):
    for jid in ids:
        if store.job(jid)['state']=='paused':store.update(jid,'running')


def control(action,store,remembered):
    if action not in ('start','stop','restart'):raise ValueError('Invalid model action')
    if action=='start':
        start_backend();resume(store,remembered)
        return []
    paused=wait_for_checkpoint(store) if backend() else []
    remembered=list(dict.fromkeys(remembered+paused))
    persisted=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    write(STATE,{**persisted,'resume_jobs':remembered})
    stop_backend()
    if action=='stop':return remembered
    start_backend();resume(store,remembered)
    return []


def main(envfile):
    url,token=credentials(envfile)
    endpoint=url+'/worker/llm/telemetry/'
    store=Store();db=Path(store.path)
    # Small index avoids scanning potentially huge JSON result rows every minute.
    with sqlite3.connect(db,timeout=10) as connection:
        connection.execute('CREATE INDEX IF NOT EXISTS tasks_telemetry_ended ON tasks(ended)')
    previous=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    remembered=previous.get('resume_jobs',[])
    last_command=previous.get('last_command',{})
    with sqlite3.connect(db,timeout=2) as connection:
        last_ended=connection.execute('SELECT coalesce(max(ended),0) FROM tasks').fetchone()[0]
    last_timing=None;last_pid=None;degradation=Degradation()
    note='';pending_command=None
    while True:
        started=time.monotonic();process=backend()
        pid=process.pid if process else None
        if pid!=last_pid:
            degradation.restarted(time.time());last_pid=pid;last_timing=None
        timing=latest_timing(db,last_ended)
        if timing:
            last_ended=timing['ended']
            if 'generation_tps' in timing:last_timing=timing
        used,total,utilization=gpu()
        profile='vision' if process and '--mmproj' in process.cmdline() else 'text' if process else ''
        current_timing=last_timing if last_timing and time.time()-last_timing['ended']<300 else None
        sample={'online':ready(),'vram_used_mb':used,'vram_total_mb':total,
            'gpu_percent':utilization,'generation_tps':current_timing.get('generation_tps') if current_timing else None,
            'prefill_tps':current_timing.get('prefill_tps') if current_timing else None,
            'vision':profile=='vision','profile':profile,'note':note}
        payload={'sample':sample}
        try:
            req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers={
                'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=15) as response:reply=json.load(response)
            command=pending_command or reply.get('command');pending_command=None
            automatic=not command and sample['online'] and timing and degradation.observe(timing,time.time())
            if automatic:command={'action':'restart','id':'auto-'+str(int(time.time()))}
            if command and command.get('action') in ('start','stop','restart'):
                if command['id']==last_command.get('id'):
                    success=last_command['success'];note=last_command['message']
                else:
                    success=False
                    try:
                        remembered=control(command['action'],store,remembered)
                        degradation.restarted(time.time())
                        note='Автоперезапуск после устойчивой деградации 15%' if automatic else 'Команда '+command['action']+' выполнена'
                        success=True
                    except Exception as error:
                        note=type(error).__name__+': '+str(error)[:110]
                        remembered=json.loads(STATE.read_text(encoding='utf-8')).get('resume_jobs',remembered) if STATE.exists() else remembered
                    last_command={'id':command['id'],'success':success,'message':note}
                    write(STATE,{'resume_jobs':remembered,'last_command':last_command})
                # Acknowledge immediately; a failed command remains visible to admin.
                payload={'sample':{**sample,'online':ready(),'note':note},'ack':command['id'],
                    'success':success,'message':note}
                req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers={
                    'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=15):pass
        except (OSError,ValueError,psutil.Error) as error:
            note='Мониторинг: '+type(error).__name__
        while time.monotonic()-started<INTERVAL:
            time.sleep(min(10,max(1,INTERVAL-(time.monotonic()-started))))
            try:
                req=urllib.request.Request(url+'/worker/llm/command/',headers={'Authorization':'Bearer '+token})
                with urllib.request.urlopen(req,timeout=5) as response:pending_command=json.load(response).get('command')
                if pending_command:break
            except (OSError,ValueError):pass


if __name__=='__main__':main(sys.argv[1])
