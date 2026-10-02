"""Bound model output and recover overflow without changing the review rules.

Completed tasks and cached responses stay immutable. Overflow is never parsed as
a valid result; every smaller task still passes normal finding verification.
"""
import time
import uuid

VERSION = 'bounded-output-v1'
MAX_SPLIT_DEPTH = 6
CONCISE = '''Ответ содержит только итоговый JSON. В explanation и reason укажи краткое
обоснование результата; не включай внутренний разбор, самопроверку, перебор формулировок
и повторения. Если ошибка не доказана, не выдавай её как violation. Перечисли все
доказанные дефекты, сохрани существенные условия и точные исходные цитаты.
Для факта выбирай краткую точную цитату, достаточную для значения и его условий;
не перечисляй одинаковые цитаты из соседних ячеек без необходимости.'''


def compact_schema(schema):
    # Never cap source quotations or the number of findings/obligations. This bounds
    # explanations (where the model looped), rather than cutting checked material.
    limits = {'explanation': 900, 'reason': 900, 'issue': 240}
    def visit(node):
        if isinstance(node, list):
            return [visit(value) for value in node]
        if not isinstance(node, dict):
            return node
        # The core shares STR/EVIDENCE dicts across fields. Copy by occurrence;
        # deepcopy alone preserves aliases and would also constrain quotations.
        value = {key: visit(item) for key, item in node.items()}
        for key, field in value.get('properties', {}).items():
            if key in limits and field.get('type') == 'string':
                field['maxLength'] = limits[key]
        return value
    return visit(schema)


def install():
    from nc5 import model, engine
    if getattr(model, '_native_output_guard', None) == VERSION:
        return
    original_budget = model.output_budget
    original_request = model.Client.request
    original_http = model.Client.http
    original_execute = engine.Engine.execute

    def budget(config, payload):
        value = original_budget(config, payload)
        if payload['stage'] == 'language':
            value = max(value, min(2048, config['output']))
        return value

    def request(client, payload):
        value = original_request(client, payload)
        value['messages'][0]['content'] += '\n' + CONCISE
        value['response_format']['json_schema']['schema'] = compact_schema(value['response_format']['json_schema']['schema'])
        return value

    def http(client, path, payload=None, timeout=None):
        response = original_http(client, path, payload, timeout)
        if path == '/v1/chat/completions':
            choice = (response.get('choices') or [{}])[0]
            if choice.get('finish_reason') == 'length':
                from nc5.common import DATA, write
                task = getattr(client, '_output_guard_task', {})
                directory = DATA/'output-overflow'/task.get('job', 'unassigned')
                write(directory/(str(time.time_ns())+'-'+uuid.uuid4().hex+'.json'),
                      {'task': task, 'response': response, 'max_tokens': payload.get('max_tokens'),
                       'adapter': VERSION})
        return response

    def execute(instance, task):
        instance.client._output_guard_task = {'job': task['job'], 'id': task['id'], 'stage': task['stage']}
        try:
            result = original_execute(instance, task)
            recover_overflow(instance, task)
            return result
        finally:
            instance.client._output_guard_task = {}

    model.output_budget = budget
    engine.output_budget = budget
    model.Client.request = request
    model.Client.http = http
    engine.Engine.execute = execute
    model._native_output_guard = VERSION


def split_payload(instance, task):
    from nc5.documents import split_blocks
    payload = task['payload']
    depth = max(int(payload.get('retry_split', 0)), int(payload.get('output_split_depth', 0)))
    if payload.get('images') or depth >= MAX_SPLIT_DEPTH:
        raise ValueError('Достигнут предел безопасного разбиения ответа')
    candidates, rules = payload.get('candidates', []), payload.get('requirements', [])
    if len(candidates) > 1:
        middle = (len(candidates)+1)//2
        parts = [{**payload, 'candidates': values} for values in (candidates[:middle], candidates[middle:])]
    elif len(rules) > 1:
        middle = (len(rules)+1)//2
        parts = [instance.rule_subset(task['job'], payload, values) for values in (rules[:middle], rules[middle:])]
    elif task['stage'] != 'verify' and payload.get('blocks'):
        parts = [{**payload, 'blocks': values, 'scope': 'split_subset',
                  'source_inventory': {'document_complete': False, 'complete_sections': []}}
                 for values in split_blocks(payload['blocks'])]
    else:
        raise ValueError('Нет безопасной границы разбиения ответа')
    # Keep the core's original two splits exhausted; this adapter owns the extra
    # bounded levels, with explicit ancestry rather than resetting that counter.
    return [{**part, 'retry_split': max(2, int(payload.get('retry_split', 0))),
             'output_split_depth': depth+1, 'output_parent_task': task['id'],
             '_output_budget': instance.config['output']} for part in parts]


def recover_overflow(instance, task):
    from nc5.common import dumps
    from pipeline import QueuePaused
    with instance.store.connect() as connection:
        row = connection.execute('SELECT state,result,error FROM tasks WHERE id=? AND job=?',
                                 (task['id'], task['job'])).fetchone()
    if not row or row['state'] != 'failed' or row['error'] != 'Незавершённый ответ: length':
        return False
    try:
        parts = split_payload(instance, task)
    except ValueError as error:
        instance.store.event(task['job'], 'output_split_limit', {'task': task['id'], 'reason': str(error)})
        return False
    # Record ancestry in the transaction AFTER children are durable. If paused
    # during counting, keep the parent pending so resume cannot omit remaining work.
    try:
        data = instance.store.job(task['job'])['data']
        for part in parts:
            instance.enqueue_bounded(task['job'], task['stage'], part, data)
    except QueuePaused:
        with instance.store.connect() as connection:
            connection.execute("UPDATE tasks SET state='pending' WHERE id=? AND state='failed'", (task['id'],))
        return False
    with instance.store.connect() as connection:
        # enqueue_bounded can further split for INPUT overflow. Associate all direct
        # descendants actually created, and never mark an unscheduled parent split.
        import json
        children = [r['id'] for r in connection.execute('SELECT id,payload FROM tasks WHERE job=? AND stage=?',
                    (task['job'], task['stage'])) if json.loads(r['payload']).get('output_parent_task') == task['id']]
        if not children:
            return False
        diagnostic = json.loads(row['result'] or '{}')
        diagnostic.update(split=True, output_guard=VERSION, children=children)
        connection.execute("UPDATE tasks SET state='split',result=?,error=NULL WHERE id=? AND state='failed'",
                           (dumps(diagnostic), task['id']))
        instance.store.event(task['job'], 'output_overflow_split',
                             {'task': task['id'], 'children': children, 'adapter': VERSION}, connection)
    return True
