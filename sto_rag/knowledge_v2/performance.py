"""Text-free, best-effort wall-clock measurements for both review engines.

Nested phases report inclusive and exclusive seconds. Server prefill/decode are
numeric observations inside HTTP time, never additional wall-clock phases.
Each resume appends a new session; unfinished/crashed sessions stay visible.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

_active = ContextVar('review_performance', default=None)
_stack = ContextVar('review_performance_stack', default=())


def path_for(directory, job):
    # Never use an untrusted job identifier as a path or write it into telemetry.
    key = hashlib.sha256(str(job).encode()).hexdigest()
    return Path(directory)/'performance'/(key+'.jsonl')


class Recorder:
    def __init__(self, path):
        self.path = Path(path)
        self.session = uuid.uuid4().hex
        self.stream = None
        self.failed = False
        self.started = time.perf_counter()
        self.milestones = set()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = self.path.open('a', encoding='utf8', buffering=1)
        except OSError:
            self.failed = True

    def write(self, kind, **values):
        if self.stream is None:
            return
        try:
            self.stream.write(json.dumps(dict(schema=1, session=self.session, kind=kind, **values),
                                         ensure_ascii=True, allow_nan=False)+'\n')
        except (OSError, ValueError):
            self.failed = True

    def close(self):
        if self.stream:
            try:
                self.stream.close()
            except OSError:
                self.failed = True


@contextmanager
def session(path):
    recorder = Recorder(path)
    token = _active.set(recorder)
    stack_token = _stack.set(())
    start = recorder.started
    recorder.write('start', timestamp=time.time())
    status = 'ok'
    try:
        yield recorder
    except BaseException:
        status = 'error'
        raise
    finally:
        recorder.write('end', seconds=time.perf_counter()-start, status=status,
                       write_failed=recorder.failed)
        recorder.close()
        _stack.reset(stack_token)
        _active.reset(token)


@contextmanager
def span(name):
    recorder = _active.get()
    if recorder is None:
        yield
        return
    parents = _stack.get()
    frame = {'children': 0.0}
    token = _stack.set((*parents, frame))
    start = time.perf_counter()
    status = 'ok'
    try:
        yield
    except BaseException:
        status = 'error'
        raise
    finally:
        seconds = time.perf_counter()-start
        if parents:
            parents[-1]['children'] += seconds
        recorder.write('span', name=name, seconds=seconds,
                       exclusive_seconds=max(0.0, seconds-frame['children']), status=status)
        _stack.reset(token)


def measured(name):
    """Names are code-owned constants, never document text or model output."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


def observe(name, value=1):
    recorder = _active.get()
    if recorder and isinstance(value, (int, float)) and math.isfinite(value):
        recorder.write('observation', name=name, value=value)


def milestone(name):
    recorder = _active.get()
    if recorder and name not in recorder.milestones:
        recorder.milestones.add(name)
        observe(name, time.perf_counter()-recorder.started)


def stage_measured(prefix, stages):
    def decorate(fn):
        @wraps(fn)
        def wrapper(self, payload, *args, **kwargs):
            stage = payload.get('stage')
            with span(prefix+'.'+(stage if stage in stages else 'other')):
                return fn(self, payload, *args, **kwargs)
        return wrapper
    return decorate


def http_measured(fn):
    @wraps(fn)
    def wrapper(self, path, *args, **kwargs):
        kind = {'/apply-template': 'template', '/tokenize': 'tokenize',
                '/props': 'metadata', '/v1/models': 'metadata',
                '/v1/chat/completions': 'generation'}.get(path, 'other')
        with span('http.'+kind):
            result = fn(self, path, *args, **kwargs)
            if kind == 'generation' and isinstance(result, dict):
                for key in ('prompt_tokens', 'completion_tokens'):
                    if key in result.get('usage', {}):
                        observe('model.'+key, result['usage'][key])
                for key in ('prompt_ms', 'predicted_ms', 'prompt_n', 'predicted_n'):
                    if key in result.get('timings', {}):
                        observe('server.'+key, result['timings'][key])
            return result
    return wrapper


def summary(path):
    result = dict(schema=1, sessions=0, completed_sessions=0, wall_seconds=0.0,
                  phases={}, observations={}, incomplete=False)
    started, ended = set(), set()
    try:
        stream = Path(path).open(encoding='utf8')
    except FileNotFoundError:
        result['available'] = False
        return result
    except OSError:
        result.update(available=False, incomplete=True)
        return result
    result['available'] = True
    try:
        with stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row['kind'] == 'start':
                        started.add(row['session'])
                    elif row['kind'] == 'end':
                        ended.add(row['session'])
                        result['wall_seconds'] += row['seconds']
                        result['incomplete'] |= row.get('write_failed', False)
                    elif row['kind'] == 'span':
                        phase = result['phases'].setdefault(row['name'], dict(calls=0, errors=0,
                            seconds=0.0, exclusive_seconds=0.0))
                        phase['calls'] += 1
                        phase['errors'] += row['status'] != 'ok'
                        phase['seconds'] += row['seconds']
                        phase['exclusive_seconds'] += row['exclusive_seconds']
                    elif row['kind'] == 'observation':
                        observation = result['observations'].setdefault(row['name'], dict(count=0, sum=0))
                        observation['count'] += 1
                        observation['sum'] += row['value']
                except (ValueError, KeyError, TypeError):
                    result['incomplete'] = True
    except OSError:
        result['incomplete'] = True
    result.update(sessions=len(started), completed_sessions=len(ended),
                  incomplete=result['incomplete'] or started != ended)
    return result


def profiled(engine):
    """Always collect local timings, regardless of optional content logging."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(owner, identity, *args, **kwargs):
            if engine == 'nc5':
                path = path_for(Path(owner.store.path).parent, identity)
            else:
                path = path_for(owner.store.directory, identity['payload']['job_id'])
            current = _active.get()
            if current is not None and current.path == path:
                return fn(owner, identity, *args, **kwargs)
            with session(path), span(engine+'.total'):
                return fn(owner, identity, *args, **kwargs)
        return wrapper
    return decorate


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Summarize local text-free performance measurements')
    parser.add_argument('path', type=Path)
    parser.add_argument('--job', help='Treat path as the engine data directory and locate this job')
    args = parser.parse_args()
    print(json.dumps(summary(path_for(args.path,args.job) if args.job else args.path), indent=2, ensure_ascii=False))
