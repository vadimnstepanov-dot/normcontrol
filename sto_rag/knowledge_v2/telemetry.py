"""Small atomic production counter file; no document text or additional inference."""
import math,time
from .structure import atomic_json


def publish(directory,timings,stage):
    def number(key):
        value=timings.get(key)
        return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None
    fields={name:number(key) for name,key in (
        ('prompt_n','prompt_n'),('predicted_n','predicted_n'),
        ('prefill_tps','prompt_per_second'),('generation_tps','predicted_per_second'))}
    for name,count,milliseconds in (('prefill_tps','prompt_n','prompt_ms'),('generation_tps','predicted_n','predicted_ms')):
        if fields[name] is None and number(count) is not None and number(milliseconds):
            fields[name]=number(count)*1000/number(milliseconds)
    if fields['generation_tps'] is None and fields['prefill_tps'] is None:return
    atomic_json(directory/'llm-telemetry.json',dict(source='knowledge-v2',ended=time.time(),stage=stage,**fields))
    # Counters contain no document text, credentials or paths. Windows reads
    # this file over WSL UNC; don't inherit NamedTemporaryFile's private mode.
    (directory/'llm-telemetry.json').chmod(0o644)
