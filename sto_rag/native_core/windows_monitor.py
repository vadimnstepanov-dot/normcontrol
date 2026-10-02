"""Keep Windows hardware telemetry, route ownership and model control to Linux."""
import json,sys,time,math
from pathlib import Path
from .transport import Transport,secret

def gpu_idle(sample):
    return not any(row['state']=='running' and row['resource']=='gpu' for row in sample['queue'])

def wait_gpu(maintenance,timeout=360):
    deadline=time.monotonic()+timeout
    while not gpu_idle(maintenance('resources',{})):
        if time.monotonic()>deadline:raise TimeoutError('GPU ticket still owns model; model remains running')
        time.sleep(.5)

def configure(path):
    import pipeline,llm_sidecar as sidecar
    from .queue_client import RemoteCoordinator
    cfg=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    transport=Transport(cfg['endpoint'],secret(cfg['queue_token_file']),cfg['certificate'])
    headers={'X-Core-Control-Token':secret(cfg['host_token_file'])}
    def maintenance(route,value):return transport.json('/control/native/'+route,value,headers=headers,timeout=380)
    coordinator=RemoteCoordinator(transport)
    coordinator.resource_snapshot=lambda:maintenance('resources',{})
    pipeline.host=lambda:coordinator
    state_path=sidecar.DATA/'linux-model-control.json'
    def control(action,store,remembered):
        if action not in ('start','stop','restart'):raise ValueError('Model action')
        with sidecar.SleepInhibitor():
            marker=json.loads(state_path.read_text()) if state_path.exists() else maintenance('pause',{})
            sidecar.write(state_path,marker)
            try:
                # Profile switching/token counting also own GPU tickets; they may
                # be between inference leases when the native checkpoint clears.
                wait_gpu(maintenance)
                with sidecar.Lease(sidecar.DATA/'model.lock'):
                    if action in ('stop','restart'):sidecar.stop_backend()
                    if action=='stop':return marker['jobs']
                    sidecar.start_backend()
                    if not sidecar.ready():raise RuntimeError('Model not ready; Linux remains paused')
                maintenance('resume',{'token':marker['token']});state_path.unlink(missing_ok=True)
                sidecar.save_state(resume_jobs=[],pause_tokens={},phase='Готово')
                return []
            except Exception:
                sidecar.save_state(phase='Прогресс Linux сохранён; ошибка управления моделью');raise
    sidecar.control=control
    def timing(db,after):
        try:row=maintenance('timing',{'after':after})
        except (OSError,ValueError):return None
        if not row:return None
        value=row['result'];metrics=value.get('metrics',{})
        raw=metrics.get('timings',{})
        if value.get('cached'):return {'ended':row['ended']}
        out={'ended':row['ended'],'source':'nc5-linux','stage':row['stage']}
        try:
            for key,field in (('prompt_n','prompt_n'),('predicted_n','predicted_n'),('generation_tps','predicted_per_second'),('prefill_tps','prompt_per_second')):
                value=float(raw.get(field) or 0)
                if not math.isfinite(value) or value<0:raise ValueError('Model timing')
                out[key]=value
        except (TypeError,ValueError):return {'ended':row['ended']}
        return out
    sidecar.latest_timing=timing
    return sidecar

if __name__=='__main__':
    sidecar=configure(sys.argv[1])
    with sidecar.Lease(sidecar.DATA/'llm-sidecar.lock'):sidecar.main(sys.argv[2])
