"""Low-overhead desktop telemetry and checkpoint-safe llama.cpp control.

Runs beside (not inside) nc5 so updating it cannot invalidate a live job's
implementation hash. The VPS receives only bounded counters, never paths or text.
"""
import json
import math
import os
import sqlite3
import statistics
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import re
from pathlib import Path

import psutil

from nc5.common import DATA,write
from nc5.runtime import BusyError,Lease,SleepInhibitor
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
        return False  # 503 also means loading: never resume a review before HTTP 200.
    except (OSError,ValueError):return False


def slot_activity():
    """Read llama.cpp slot state once per telemetry interval, without generating tokens."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:'+MODEL_PORT+'/slots',timeout=2) as response:
            slots=json.load(response)
        if not isinstance(slots,list) or not slots:return None
        states=[slot.get('is_processing') for slot in slots if isinstance(slot,dict)]
        if not states or not all(isinstance(state,bool) for state in states):return None
        return any(states)
    except (OSError,ValueError,TypeError):return None


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
        return {'source':'nc5','ended':ended,'stage':stage,'prompt_n':prompt_n,'predicted_n':predicted_n,
            'prefill_tps':round(prefill,2),'generation_tps':round(generation,2)}
    except (ValueError,TypeError,KeyError):return {'ended':ended}


def model_label(process):
    """Display the model filename prefix through its parameter count, never its path."""
    if not process:return ''
    try:
        args=process.cmdline();name=''
        for i,arg in enumerate(args):
            if arg in ('-m','--model') and i+1<len(args):name=args[i+1];break
            if arg.startswith('--model='):name=arg.split('=',1)[1];break
        name=re.split(r'[/\\]',name)[-1]
        match=re.search(r'\d+(?:\.\d+)?[bB](?=[^A-Za-z0-9]|$)',name)
        return (name[:match.end()] if match else name.removesuffix('.gguf'))[:100]
    except (psutil.Error,OSError):return ''


def latest_v2_timing(last_ended):
    # Reading one atomic file avoids scanning large canonical task JSON or
    # launching WSL/Python for every minute sample.
    configured=os.getenv('NORMCONTROL_KNOWLEDGE_TELEMETRY')
    if configured:path=Path(configured)
    elif os.name=='nt':
        distro=os.getenv('NORMCONTROL_KNOWLEDGE_DISTRO','NormControl')
        path=Path('\\\\wsl.localhost\\'+distro+'\\opt\\normcontrol\\data\\llm-telemetry.json')
    else:path=Path(os.getenv('NORMCONTROL_KNOWLEDGE_DATA','/data'))/'llm-telemetry.json'
    try:
        try:
            if path.stat().st_size>4096:return None
            text=path.read_text(encoding='utf8')
        except PermissionError:
            if os.name!='nt' or configured:raise
            # Container-owned mode-0600 files can be unreadable over Windows UNC.
            # One bounded read per minute; never scan the DB or query the model.
            result=subprocess.run(['wsl.exe','-d',distro,'--','python3','-c',
                "from pathlib import Path; p=Path('/opt/normcontrol/data/llm-telemetry.json'); print(p.read_text(encoding='utf-8') if p.stat().st_size<=4096 else '')"],
                capture_output=True,text=True,encoding='utf-8',timeout=5,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode or len(result.stdout)>4096:return None
            text=result.stdout
        item=json.loads(text)
        if item.get('source')!='knowledge-v2' or not isinstance(item.get('stage'),str):return None
        for key in ('ended','prompt_n','predicted_n','prefill_tps','generation_tps'):
            value=item.get(key)
            if value is None and key!='ended':continue
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:return None
        if item['ended']<=last_ended:return None
        return {k:item.get(k) for k in ('source','ended','stage','prompt_n','predicted_n','prefill_tps','generation_tps')}
    except (OSError,ValueError,TypeError,subprocess.SubprocessError):return None


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


def save_state(**changes):
    previous=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    write(STATE,{**previous,**changes})


def pause_jobs(store,remembered):
    """Persist ownership before the transaction; never adopt a user's paused job."""
    previous=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    tokens=previous.get('pause_tokens',{})
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        rows=connection.execute("SELECT id FROM jobs WHERE state IN ('running','preparing')").fetchall()
        stamp=time.time()
        for row in rows:
            if row[0] not in remembered:remembered.append(row[0])
            tokens[row[0]]=stamp
        save_state(resume_jobs=remembered,pause_tokens=tokens)
        for row in rows:
            connection.execute("UPDATE jobs SET state='paused',updated=? WHERE id=?",(stamp,row[0]))
            store.event(row[0],'llm_control_pause',{},connection)


def wait_for_checkpoint(store,timeout=900,lease=None,remembered=None):
    """Pause current jobs, wait for the in-flight answer to be saved and engine lease freed."""
    ids=remembered if remembered is not None else []
    owned_lease=lease or Lease(str(store.path)+'.worker.lock')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        # Preparation can update its state while winding down. Reapply until the
        # engine releases its lease, then keep that lease through the whole restart.
        pause_jobs(store,ids)
        try:
            owned_lease.__enter__()
            pause_jobs(store,ids)
            if lease is None:owned_lease.__exit__()
            return ids
        except BusyError:pass
        time.sleep(2)
    raise TimeoutError('Не удалось дождаться контрольной точки; модель не остановлена')


def stop_backend():
    process=backend()
    if not process:return
    # The existing context-guard runner notices backend exit and closes its
    # own child. Do not kill the user's terminal or unrelated Python workers.
    runner=process.parent()
    if runner and not any(Path(arg).name=='runner.py' for arg in runner.cmdline()):runner=None
    if os.name=='nt':
        helper=Path(__file__).with_name('llm_console_signal.py')
        result=subprocess.run([sys.executable,str(helper),str(process.pid)],timeout=10,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),capture_output=True,text=True)
        if result.returncode:raise RuntimeError('Нет изолированной консоли LLM для штатной остановки; проверка сохранена на паузе')
    else:process.send_signal(signal.SIGINT)
    try:process.wait(60)
    except psutil.TimeoutExpired:raise RuntimeError('LLM не завершила штатную остановку; принудительное завершение отменено')
    if runner and runner.is_running():
        try:runner.wait(15)
        except psutil.TimeoutExpired:raise RuntimeError('Context Guard не завершился')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and backend():time.sleep(.5)
    if backend():raise RuntimeError('Сервер модели не остановился')
    # Process exit releases its RAM/CUDA allocations. Do not flush unrelated
    # applications' memory or reset the GPU. Also wait for the serving port.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',int(MODEL_PORT)))==0:
            raise RuntimeError('Порт LLM ещё занят; запуск второго экземпляра отменён')


def start_backend(profile=None):
    if ready():return
    if backend():
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            if ready():return
            time.sleep(2)
        raise TimeoutError('Модель уже запущена, но не готова; второй экземпляр не создаётся')
    if not LAUNCHER.exists():raise FileNotFoundError('Не найден локальный скрипт запуска модели')
    command=['cmd.exe','/c',str(LAUNCHER)] if os.name=='nt' else [str(LAUNCHER)]
    environment=os.environ.copy()
    if profile is not None:
        if profile not in ('text','vision'):raise ValueError('Model profile')
        environment['NORMCONTROL_LLM_PROFILE']=profile
    subprocess.Popen(command,cwd=str(LAUNCHER.parent),env=environment,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        if ready():return
        time.sleep(2)
    raise TimeoutError('Модель не запустилась за 3 минуты')


def ensure_profile(profile):
    """Called only by the authenticated gateway at an exclusive v2 turn boundary."""
    if profile not in ('text','vision'):raise ValueError('Model profile')
    def matches():
        p=backend()
        return p and ('--mmproj' in p.cmdline())==(profile=='vision') and ready()
    if matches():return {'profile':profile,'ready':True,'changed':False}
    store=Store()
    # Older jobs pin their model profile. They must finish or be explicitly paused
    # and revised by their owner; the profile adapter cannot silently change them.
    with store.connect() as db:
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','preparing') LIMIT 1").fetchone():
            raise BusyError('Legacy review owns the model')
    with Lease(str(store.path)+'.worker.lock'),Lease(DATA/'model.lock'),SleepInhibitor():
        if matches():return {'profile':profile,'ready':True,'changed':False}
        if backend() and slot_activity() is not False:raise BusyError('Model request still in flight')
        stop_backend();start_backend(profile)
        if not matches():raise RuntimeError('Launcher did not activate requested model profile')
        return {'profile':profile,'ready':True,'changed':True}


def resume(store,ids):
    previous=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    tokens=previous.get('pause_tokens',{})
    for jid in ids:
        with store.connect() as connection:
            token=tokens.get(jid)
            if token is None:continue
            # task_finished can update jobs.updated after our pause: use the
            # explicit state event to distinguish this from a later user pause.
            last=connection.execute("SELECT kind,data FROM events WHERE job=? AND (kind='llm_control_pause' OR (kind='state' AND json_extract(data,'$.state') IS NOT NULL)) ORDER BY seq DESC LIMIT 1",(jid,)).fetchone()
            if not last or last['kind']!='llm_control_pause':continue
            result=connection.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='paused'",(time.time(),jid))
            if result.rowcount:store.event(jid,'llm_control_resume',{},connection)


def control(action,store,remembered):
    if action not in ('start','stop','restart'):raise ValueError('Invalid model action')
    remembered=list(remembered)
    lease=Lease(str(store.path)+'.worker.lock')
    with SleepInhibitor():
        try:
            save_state(phase='Ожидание сохранения текущей части')
            wait_for_checkpoint(store,lease=lease,remembered=remembered)
            if action in ('stop','restart'):
                save_state(phase='Штатная остановка LLM')
                stop_backend()
            if action=='stop':
                save_state(phase='LLM выключена; проверка на паузе')
                return remembered
            save_state(phase='Загрузка LLM')
            start_backend()
            if not ready():raise RuntimeError('LLM не подтвердила готовность; проверка остаётся на паузе')
            save_state(phase='Возобновление проверки')
            resume(store,remembered)
            save_state(resume_jobs=[],pause_tokens={},phase='Готово')
            return []
        except Exception:
            save_state(phase='Ошибка управления; прогресс сохранён')
            raise
        finally:lease.__exit__()


def execute_model_command(command,store,remembered):
    if command.get('automatic') and command.get('action')=='start':
        # Queue wake-up must not resume any check paused by its owner or operator.
        with Lease(DATA/'model.lock'),SleepInhibitor():
            save_state(phase='Автоматическая загрузка LLM для очереди')
            start_backend()
            if not ready():raise RuntimeError('Модель не готова после автоматического запуска')
            save_state(phase='Готово')
        return list(remembered)
    return control(command['action'],store,remembered)


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
    last_timing=None;last_pid=None;last_v2_ended=0;degradation=Degradation()
    psutil.cpu_percent(interval=None)
    note='';pending_command=previous.get('operation')
    while True:
        started=time.monotonic();process=backend()
        pid=process.pid if process else None
        if pid!=last_pid:
            degradation.restarted(time.time());last_pid=pid;last_timing=None
        timing=latest_timing(db,last_ended)
        if timing:
            last_ended=timing['ended']
            if 'generation_tps' in timing:last_timing=timing
        v2_timing=latest_v2_timing(last_v2_ended)
        if v2_timing:
            last_v2_ended=v2_timing['ended']
            if not last_timing or v2_timing['ended']>last_timing['ended']:last_timing=v2_timing
        used,total,utilization=gpu()
        memory=psutil.virtual_memory()
        profile='vision' if process and '--mmproj' in process.cmdline() else 'text' if process else ''
        current_timing=last_timing if last_timing and time.time()-last_timing['ended']<300 else None
        sample={'online':ready(),'processing':slot_activity() if process else False,
            'uptime_seconds':round(max(0,time.time()-process.create_time())) if process else None,
            'cpu_percent':psutil.cpu_percent(interval=None),'ram_used_mb':round(memory.used/1048576),
            'ram_total_mb':round(memory.total/1048576),'vram_used_mb':used,'vram_total_mb':total,
            'gpu_percent':utilization,'generation_tps':current_timing.get('generation_tps') if current_timing else None,
            'prefill_tps':current_timing.get('prefill_tps') if current_timing else None,
            'vision':profile=='vision','profile':profile,'note':note,'model_label':model_label(process),
            'timing_source':current_timing.get('source','nc5') if current_timing else '',
            'timing_at':current_timing['ended'] if current_timing else None}
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
                    if automatic:degradation.last_restart=time.time()
                    save_state(operation=command)
                    try:
                        remembered=execute_model_command(command,store,remembered)
                        degradation.restarted(time.time())
                        note='Автоперезапуск после устойчивой деградации 15%' if automatic else 'Команда '+command['action']+' выполнена'
                        success=True
                    except Exception as error:
                        note=type(error).__name__+': '+str(error)[:110]
                        remembered=json.loads(STATE.read_text(encoding='utf-8')).get('resume_jobs',remembered) if STATE.exists() else remembered
                    last_command={'id':command['id'],'success':success,'message':note}
                    save_state(resume_jobs=remembered,last_command=last_command,operation=None)
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


if __name__=='__main__':
    with Lease(DATA/'llm-sidecar.lock'):
        main(sys.argv[1])
