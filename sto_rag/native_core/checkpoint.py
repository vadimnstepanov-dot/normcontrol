import json,time,uuid
from pathlib import Path
from nc5.common import DATA,write
from nc5.store import Store
from nc5.runtime import Lease,BusyError

FILE=DATA/'core-model-checkpoint.json'

def pause(reason='model',path=None,job_ids=None):
    path=Path(path) if path is not None else FILE
    store=Store();token=uuid.uuid4().hex
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        ids=[row[0] for row in connection.execute("SELECT id FROM jobs WHERE state IN ('running','preparing')") if job_ids is None or row[0] in job_ids]
        write(path,{'token':token,'jobs':ids,'reason':reason})
        for job in ids:
            connection.execute("UPDATE jobs SET state='paused',updated=? WHERE id=?",(time.time(),job))
            store.event(job,'core_control_pause',{'token':token},connection)
    return token,ids

def wait(timeout=360):
    deadline=time.monotonic()+timeout
    while True:
        try:
            with Lease(str(Store().path)+'.worker.lock'),Lease(DATA/'model.lock'):return
        except BusyError:
            if time.monotonic()>deadline:raise TimeoutError('Linux checkpoint wait')
            time.sleep(.5)

def resume(token,path=None):
    path=Path(path) if path is not None else FILE
    state=json.loads(path.read_text())
    if state['token']!=token:raise ValueError('Checkpoint ownership changed')
    store=Store();resumed=[]
    with store.connect() as connection:
        for job in state['jobs']:
            last=connection.execute("SELECT kind,data FROM events WHERE job=? AND (kind='core_control_pause' OR (kind='state' AND json_extract(data,'$.state') IS NOT NULL)) ORDER BY seq DESC LIMIT 1",(job,)).fetchone()
            if not last or last['kind']!='core_control_pause' or json.loads(last['data']).get('token')!=token:continue
            result=connection.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='paused'",(time.time(),job))
            if result.rowcount:store.event(job,'core_control_resume',{'token':token},connection);resumed.append(job)
    path.unlink(missing_ok=True)
    return resumed
