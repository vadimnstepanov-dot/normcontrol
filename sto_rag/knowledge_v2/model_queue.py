"""FIFO inference turns shared by v2 checks and requested review suggestions."""
from contextlib import contextmanager
import time
import uuid
import os
from .store import checksum


@contextmanager
def model_turn(store,client):
    ticket=str(uuid.uuid4())
    # Text and Vision endpoints can address the same GPU; serialize by resource,
    # not URL spelling (gateway vs loopback). Multi-GPU deployments may override.
    model=checksum(getattr(client,'resource_key',os.getenv('KNOWLEDGE_MODEL_RESOURCE','gpu:primary')))
    timeout=max(30,float(getattr(client,'timeout',300)))
    with store.connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS model_tickets(id TEXT PRIMARY KEY,model TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL)')
        db.execute('INSERT INTO model_tickets VALUES(?,?,?,?,?)',(ticket,model,time.time(),time.time()+timeout+60,'waiting'))
    deadline=time.monotonic()+timeout
    try:
        while True:
            with store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM model_tickets WHERE expires<?',(time.time(),))
                busy=db.execute("SELECT 1 FROM model_tickets WHERE model=? AND state='running'",(model,)).fetchone()
                first=db.execute("SELECT id FROM model_tickets WHERE model=? AND state='waiting' ORDER BY created,id LIMIT 1",(model,)).fetchone()
                if not busy and first and first['id']==ticket:
                    db.execute("UPDATE model_tickets SET state='running',expires=? WHERE id=?",(time.time()+timeout+60,ticket))
                    break
            if time.monotonic()>=deadline:raise TimeoutError('Model turn queue timed out; retry from durable cursor')
            time.sleep(.2)
        yield
    finally:
        with store.connection() as db:db.execute('DELETE FROM model_tickets WHERE id=?',(ticket,))
