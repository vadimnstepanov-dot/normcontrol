"""Human report addresses. Word-verified labels; never guess automatic numbering."""
from collections import defaultdict, deque, Counter
from pathlib import Path
from zipfile import ZipFile
import json
import re
import subprocess
import os

from extract import Numbering, clean
from word_compact import package, N, Q

HERE = Path(__file__).resolve().parent
NOTE = ('Адреса относятся к исходной версии документа. Абзацы считаются с 1 после указанного '
        'заголовка до следующего заголовка; пустые строки и ячейки таблиц не учитываются. '
        'Каждый элемент списка считается абзацем. Для таблиц отдельно указаны порядковый номер '
        'таблицы после заголовка, строка, ячейка и абзац в ячейке. У ненумерованного подраздела '
        'указаны номер родительского пункта и название подраздела. Цитата служит ориентиром для поиска.')

def verified_rows(path, digest):
    cache = HERE/'data'/'heading_labels'/(digest+'.json')
    if not cache.exists() and os.name == 'nt':
        # Explicitly numbered/plain headings need no Word process, which matters for large corpora.
        import xml.etree.ElementTree as ET
        with ZipFile(path) as z:
            numbering=Numbering(z);root=ET.fromstring(z.read('word/document.xml'))
            needs_word=any(int(pr.get('outline','9'))<9 and pr.get('num') not in (None,'0') for pr in (numbering.properties(p) for p in root.findall('.//w:body//w:p',N)))
        if not needs_word:return []
        cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(['powershell', '-NoProfile', '-File', str(HERE/'export_heading_labels.ps1'),
                        '-Document', str(path), '-OutputPath', str(cache)],
                       timeout=180, check=True, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError,subprocess.SubprocessError):
            # Missing Word must not prevent text review. build_locations retains explicit
            # numbers or a heading path; it never substitutes guessed automatic labels.
            return []
    if cache.exists():
        data = json.loads(cache.read_text(encoding='utf-8-sig'))
        if data.get('sha256') == digest:
            return data['paragraphs']
    return []

def build_locations(d, rows):
    with ZipFile(d['path']) as z:
        numbering = Numbering(z)
    headings = {}
    for p, r in zip(d['paragraphs'], d['records']):
        level = int(numbering.properties(p).get('outline', '9'))
        if level < 9 and clean(r['text']) and not r['table']:
            headings[r['id']] = (clean(r['text']), level)
    expected = Counter(headings.values())
    labels = defaultdict(deque)
    for row in rows:
        labels[(clean(row['text']), int(row['outline']))].append(row['label'])
    # Repeated headings are mapped only when occurrence counts agree.
    labels = {k: v for k, v in labels.items() if len(v) == expected[k]}
    table_info = {}
    for ti, table in enumerate(d['root'].findall('.//w:tbl', N), 1):
        for ri, row in enumerate(table.findall('w:tr', N), 1):
            for ci, cell in enumerate(row.findall('w:tc', N), 1):
                count = 0
                for p in cell.findall('w:p', N):
                    from word_compact import plain
                    if clean(plain(p)): count += 1
                    table_info[id(p)] = (ti, ri, ci, count)
    stack = []; count = 0; section_tables = {}; result = {}
    for p, r in zip(d['paragraphs'], d['records']):
        rid = r['id']; raw = clean(r['text'])
        heading = rid in headings
        if heading:
            title, level = headings[rid]
            while stack and stack[-1]['level'] >= level: stack.pop()
            label = labels[headings[rid]].popleft() if headings[rid] in labels else ''
            explicit = re.match(r'^((?:[А-ЯA-Z]\.)?\d+(?:\.\d+)*\.?)(?:\s+)(.+)$', title)
            if not label and explicit: label, title = explicit[1], explicit[2]
            stack.append({'level': level, 'label': label.rstrip('.'), 'title': title})
            count = 0; section_tables = {}
        elif raw and not r['table']: count += 1
        numbered = next((i for i in range(len(stack)-1, -1, -1) if stack[i]['label']), None)
        visible = stack[numbered:] if numbered is not None else stack
        address = '; подраздел '.join(
            ('пункт '+h['label']+' ' if h['label'] else '')+'«'+h['title']+'»' for h in visible)
        if not address: address = 'Начальная часть документа до первого заголовка'
        if heading: address += '; заголовок'
        elif id(p) in table_info:
            ti, ri, ci, pi = table_info[id(p)]
            section_tables.setdefault(ti, len(section_tables)+1)
            address += f'; таблица {section_tables[ti]} после заголовка; строка {ri}; ячейка {ci}; абзац {pi}'
        else: address += f'; абзац {count} после заголовка' if stack else f'; абзац {count}'
        result[rid] = address
    return result

def attach_locations(report):
    warnings = report.setdefault('warnings', [])
    try:
        d = package(report['document'])
        if d['sha256'] != report['sha256']: raise ValueError('Исходный документ изменён; адреса не пересчитаны')
        try:
            rows = verified_rows(d['path'], d['sha256'])
        except Exception:
            rows = []
            warnings.append('Не удалось прочитать автоматические номера Word: адреса используют названия заголовков.')
        locations = build_locations(d, rows)
        for finding in report['findings']:
            for evidence in finding['evidence']:
                evidence['location'] = locations.get(evidence['locator'], 'Вне основного текста; найдите приведённую цитату поиском Word')
        report['location_note'] = NOTE
        report['location_map'] = locations
    except (OSError, ValueError) as exc:
        warnings.append(str(exc))
    return report

def render_report(result):
    locations = result.get('location_map', {})
    def human(text):
        return re.sub(r'\bp\d+\b', lambda m: locations.get(m[0], m[0]), text)
    lines = ['# Нормоконтроль', 'Документ: '+result['document'], 'Статус: '+result['status'],
             'Охват: '+json.dumps(result['coverage'], ensure_ascii=False), result.get('location_note', '')]
    for i, f in enumerate(result['findings'], 1):
        lines += [f"## НК-{i:03}: {f['category']}", human(f['issue']), 'Обоснование: '+human(f['reason'])]
        for e in f['evidence']:
            lines += ['Место: '+e.get('location', 'Адрес не восстановлен; используйте поиск по цитате'), '> '+e['quote']]
        if f['source']:
            s = f['source']
            lines += [f"СТО: {s['document']}; пункт: {s['clause']}; файл: {s['file']}", '> '+s['quote']]
        lines += ['Предложение: '+human(f['recommendation']), '']
    lines += ['## Ограничения'] + ['- '+str(w) for w in result['warnings']]
    lines += ['## Незавершённые части', json.dumps(result['errors'], ensure_ascii=False, indent=2)]
    return '\n\n'.join(lines)
