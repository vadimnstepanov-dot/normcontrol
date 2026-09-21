"""DOCX extraction with OOXML list numbering and source locators. Standard library only."""
from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
import re
import xml.etree.ElementTree as ET

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W = '{' + NS['w'] + '}'

def val(el, path, default=''):
    e = el.find(path, NS) if el is not None else None
    return e.get(W + 'val', default) if e is not None else default

def clean(s):
    return re.sub(r'\s+', ' ', s.replace('\u00ad', '').replace('\xa0', ' ').replace('\x07','').replace('\x01','')).strip()

def text(el):
    return clean(''.join((x.text or '') if x.tag == W+'t' else ' '
                        for x in el.iter() if x.tag in (W+'t', W+'tab', W+'br')))

class Numbering:
    def __init__(self, z):
        self.styles, self.nums, self.counts = {}, {}, defaultdict(dict)
        if 'word/styles.xml' in z.namelist():
            self.styles = {x.get(W+'styleId'): x for x in ET.fromstring(z.read('word/styles.xml')).findall('w:style', NS)}
        if 'word/numbering.xml' not in z.namelist():
            return
        r = ET.fromstring(z.read('word/numbering.xml'))
        abstracts = {x.get(W+'abstractNumId'): {int(l.get(W+'ilvl')): l for l in x.findall('w:lvl', NS)} for x in r.findall('w:abstractNum', NS)}
        for n in r.findall('w:num', NS):
            levels = dict(abstracts.get(val(n, 'w:abstractNumId'), {}))
            starts = {}
            for o in n.findall('w:lvlOverride', NS):
                i = int(o.get(W+'ilvl'))
                if o.find('w:lvl', NS) is not None:
                    levels[i] = o.find('w:lvl', NS)
                if val(o, 'w:startOverride'):
                    starts[i] = int(val(o, 'w:startOverride'))
            self.nums[n.get(W+'numId')] = (levels, starts)

    def properties(self, p):
        sid = val(p, 'w:pPr/w:pStyle')
        chain, seen = [], set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            s = self.styles[sid]
            chain.insert(0, s.find('w:pPr', NS))
            sid = val(s, 'w:basedOn')
        chain.append(p.find('w:pPr', NS))
        props = {}
        for pr in chain:
            for k, path in [('num', 'w:numPr/w:numId'), ('level', 'w:numPr/w:ilvl'), ('outline', 'w:outlineLvl')]:
                v = val(pr, path)
                if v != '':
                    props[k] = v
        return props

    def label(self, p):
        props = self.properties(p)
        num, i = props.get('num'), int(props.get('level', '0'))
        if not num or num == '0' or num not in self.nums:
            return '', props, ''
        levels, starts = self.nums[num]
        if i not in levels:
            return '', props, 'Неизвестный уровень нумерации'
        counts = self.counts[num]
        start = lambda j: starts.get(j, int(val(levels.get(j), 'w:start', '1')))
        counts[i] = counts.get(i, start(i)-1) + 1
        for j in list(counts):
            restart = int(val(levels.get(j), 'w:lvlRestart', str(j)))
            if j > i and restart != 0 and i <= restart-1:
                del counts[j]
        fmt = val(levels[i], 'w:numFmt', 'decimal')
        template = val(levels[i], 'w:lvlText', '%'+str(i+1))
        warning = ''
        def sub(m):
            nonlocal warning
            j = int(m[1])-1
            n = counts.get(j, start(j))
            f = val(levels.get(j), 'w:numFmt', 'decimal')
            if f in ('decimal', 'decimalZero'):
                return str(n).zfill(2) if f == 'decimalZero' else str(n)
            if f in ('russianUpper', 'russianLower', 'upperLetter', 'lowerLetter'):
                alphabet = 'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ' if f.startswith('russian') else 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                label = alphabet[(n-1) % len(alphabet)]
                return label.lower() if f.endswith('Lower') or f == 'lowerLetter' else label
            warning = 'Нестандартная нумерация: '+f
            return str(n)
        return re.sub(r'%([1-9])', sub, template), props, warning

def extract_docx(path):
    with ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 200_000_000:
            raise ValueError('Слишком большой распакованный DOCX (более 200 МБ)')
        root = ET.fromstring(z.read('word/document.xml'))
        numbering = Numbering(z)
        verified_labels = defaultdict(deque)
        sidecar = Path(__file__).parent/'data'/'word_numbering'/(Path(path).stem+'.json')
        verified = False
        if sidecar.exists():
            cache = json.loads(sidecar.read_text(encoding='utf-8-sig'))
            if cache.get('sha256') == hashlib.sha256(Path(path).read_bytes()).hexdigest():
                verified = True
                for row in cache['paragraphs']:
                    verified_labels[clean(row['text'])].append(row['label'])
        headers = ' '.join(text(ET.fromstring(z.read(n))) for n in z.namelist() if re.match(r'word/header\d+\.xml$', n))
        match = re.search(r'СТО\s*РЖД\s*\d+(?:\.\d+)+(?:\s*[–—-]\s*\d{4})?', headers)
        standard = clean(match[0]) if match else ''
        body = root.find('w:body', NS)
        blocks, warnings = [], []
        paragraph = table = 0
        clause, heading, appendix, caption, appendix_title = '', '', '', '', ''
        def paragraph_block(p, in_table=False):
            nonlocal paragraph, clause, heading, appendix, caption, appendix_title
            paragraph += 1
            raw = text(p)
            label, props, warning = numbering.label(p)
            if verified:
                if verified_labels[raw]:
                    label = verified_labels[raw].popleft()
                    warning = ''
                elif raw:
                    label = ''
                    warning = 'Абзац не сопоставлен с нумерацией Word; номер не назначен'
            else:
                # Never expose guessed Word labels as documentary evidence.
                label = ''
            if warning:
                warnings.append(f'Абзац {paragraph}: {warning}')
            if not raw and not label:
                return None
            rendered = clean(label+' '+raw) if label else raw
            explicit = re.match(r'^([А-ЯA-Z]?\.?\d+(?:\.\d+)*)(?:\.)?\s+\S', raw)
            is_outline = int(props.get('outline', '9')) < 9
            if not in_table:
                app = re.match(r'^Приложение\s+([А-ЯA-Z])(?:\s|$)', rendered, re.I)
                if app:
                    appendix = 'Приложение '+app[1].upper()
                    appendix_title = rendered
                    clause, heading, caption = appendix, rendered, ''
                elif explicit:
                    clause = explicit[1].rstrip('.')
                    if appendix and not clause.startswith(appendix.split()[-1]):
                        clause = appendix+', '+clause
                    if is_outline:
                        heading = rendered
                    caption = ''
                elif label and re.match(r'^\d+(?:\.\d+)*\.?$', label) and (is_outline or '.' in label.rstrip('.')):
                    clause = (appendix+', ' if appendix else '')+label.rstrip('.')
                    heading, caption = rendered, ''
                elif is_outline and not label and raw:
                    heading = raw
                cap = re.match(r'^(?:Продолжение\s+|Окончание\s+)?[Тт]аблиц[аы]\s+([А-ЯA-Z]?\.?\d+(?:\.\d+)*)', raw)
                if cap:
                    caption = 'Таблица '+cap[1]
            return {'text': rendered, 'raw_text': raw, 'clause': clause, 'heading': heading,
                    'appendix': appendix, 'appendix_title': appendix_title, 'locator': f'абзац {paragraph}', 'paragraph': paragraph,
                    'kind': 'paragraph', 'numbering': 'automatic' if label else 'literal'}
        def walk(container):
            nonlocal table, caption
            for child in container:
                if child.tag == W+'p':
                    b = paragraph_block(child)
                    if b:
                        blocks.append(b)
                elif child.tag == W+'tbl':
                    table += 1
                    table_id = table
                    table_label = caption or f'Таблица без номера (порядковая {table_id})'
                    header = ''
                    merged = {}
                    for ri, row in enumerate(child.findall('w:tr', NS), 1):
                        cells, col = [], 0
                        first_p = paragraph+1
                        for cell in row.findall('w:tc', NS):
                            parts = [paragraph_block(p, True) for p in cell.findall('.//w:p', NS)]
                            cell_text = ' / '.join(b['text'] for b in parts if b)
                            vm = cell.find('w:tcPr/w:vMerge', NS)
                            if vm is not None and vm.get(W+'val') != 'restart':
                                cell_text = cell_text or merged.get(col, '')
                            elif vm is not None:
                                merged[col] = cell_text
                            else:
                                merged.pop(col, None)
                            cells.append(cell_text)
                            col += int(val(cell, 'w:tcPr/w:gridSpan', '1'))
                        row_text = ' | '.join(cells)
                        if ri == 1:
                            header = row_text
                        blocks.append({'text': table_label+'\n'+(('Столбцы: '+header+'\n') if ri>1 else '')+row_text,
                                       'raw_text': row_text, 'clause': clause, 'heading': heading,
                                       'appendix': appendix, 'appendix_title': appendix_title, 'locator': f'{table_label}, строка {ri}; абзац {first_p}',
                                       'paragraph': first_p, 'kind': 'table_row', 'table': table_id, 'row': ri,
                                       'numbering': 'table'})
                    caption = ''
                elif child.tag not in (W+'sectPr', W+'del'):
                    walk(child)
        walk(body)
        for name in ('footnotes', 'endnotes'):
            part = 'word/'+name+'.xml'
            if part in z.namelist():
                for note in ET.fromstring(z.read(part)):
                    if int(note.get(W+'id', '-1')) > 0 and text(note):
                        blocks.append({'text': text(note), 'raw_text': text(note), 'clause': '', 'heading': '', 'appendix': '',
                                       'locator': f'{name} {note.get(W+"id")}', 'paragraph': 0, 'kind': 'note', 'numbering': 'literal'})
        images = len(root.findall('.//w:drawing', NS))+len(root.findall('.//w:pict', NS))
        if images:
            warnings.append(f'Графические объекты: {images}. Текст изображений и визуальное оформление не проверены.')
        if root.findall('.//w:del', NS) or root.findall('.//w:ins', NS):
            warnings.append('Есть исправления Word; извлечена версия с вставками, без удаленного текста.')
        if not verified:
            warnings.append('Нумерация Word не сверена: автоматические номера списков не включены в текст. Места указаны по абзацам.')
        return {'standard': standard, 'blocks': blocks, 'warnings': warnings, 'numbering_verified': verified,
                'paragraphs': paragraph, 'tables': table, 'images': images}

def read_document(path):
    path = Path(path)
    if path.suffix.lower() == '.docx':
        return extract_docx(path)
    if path.suffix.lower() in ('.txt', '.md'):
        content = path.read_text(encoding='utf-8-sig')
        return {'standard': '', 'warnings': [], 'blocks': [
            {'text': s, 'raw_text': s, 'clause': '', 'heading': '', 'appendix': '', 'locator': f'строка {i}',
             'paragraph': i, 'kind': 'paragraph', 'numbering': 'literal'}
            for i, s in enumerate(content.splitlines(), 1) if s.strip()]}
    raise ValueError('Поддерживаются DOCX, TXT и MD. Старый DOC сохраните как DOCX.')
