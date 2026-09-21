"""Build structural review units from one DOCX without legacy review services."""
import copy
import re
from zipfile import ZipFile

from document_locations import build_locations, verified_rows
from extract import Numbering, clean
from sections import plan
from word_compact import package, N, Q


def units_for_document(path):
    d = package(path)
    legacy = plan(path)
    rows = verified_rows(path, d['sha256'])
    addresses = build_locations(d, rows)
    with ZipFile(path) as archive:
        numbering = Numbering(archive)
    blocks = [block for group in legacy['groups'] for block in group]
    by_id = {block['locator']: block for block in blocks}
    paragraph_cells = {
        id(paragraph): cell
        for cell in d['root'].iter(Q + 'tc')
        for paragraph in cell.findall('w:p', N)
    }
    headings = []
    stack = []
    body = []
    table_headers = {}
    table_titles = {}
    caption = ''
    for paragraph, record in zip(d['paragraphs'], d['records']):
        block = copy.deepcopy(by_id[record['id']])
        block['text'] = record['text']
        block['offset'] = 0
        properties = numbering.properties(paragraph)
        level = int(properties.get('outline', '9'))
        if level < 9 and not record['table'] and clean(record['text']):
            while stack and stack[-1]['level'] >= level:
                stack.pop()
            stack.append({
                'level': level,
                'title': clean(record['text']),
                'locator': record['id'],
                'address': addresses[record['id']],
            })
            headings.append(dict(stack[-1]))
            caption = ''
        block['heading_path'] = [item['title'] for item in stack]
        block['section_title'] = ' / '.join(block['heading_path']) or 'Начало документа'
        block['section'] = stack[-1]['locator'] if stack else 'front'
        block['address'] = addresses[record['id']]
        block['is_heading'] = level < 9 and not record['table']
        if re.match(r'^\s*Таблица\s+[\dА-Я]', record['text']):
            caption = clean(record['text'])
        formatting = block['format']
        if record['table']:
            table = formatting['table']
            table_titles.setdefault(table, caption)
            if formatting['row'] == 1:
                table_headers.setdefault(table, []).append(record['text'])
            cell = paragraph_cells.get(id(paragraph))
            if cell is not None:
                span = cell.find('w:tcPr/w:gridSpan', N)
                merge = cell.find('w:tcPr/w:vMerge', N)
                formatting['column_span'] = int(span.get(Q + 'val', '1')) if span is not None else 1
                if merge is not None:
                    formatting['vertical_merge'] = merge.get(Q + 'val', 'continue')
        body.append(block)
    for block in body:
        table = block['format'].get('table')
        if table:
            block['table_context'] = {
                'title': table_titles[table],
                'headers': table_headers.get(table, []),
                'table': table,
                'row': block['format']['row'],
                'cell': block['format']['cell'],
            }
    units = []
    for block in body:
        if not block['text'].strip():
            continue
        key = ('row', block['format']['table'], block['format']['row']) if 'table' in block['format'] else ('paragraph', block['locator'])
        if units and units[-1]['key'] == key:
            units[-1]['blocks'].append(block)
        else:
            units.append({'key': key, 'section': block['section'], 'blocks': [block]})
    for block in blocks:
        if not re.fullmatch(r'p\d+', block['locator']) and block['text'].strip():
            units.append({'key': ('extra', block['locator']), 'section': block['section'], 'blocks': [block]})
    front = '\n'.join(block['text'] for block in body[:180])
    if re.search(r'частно[её]\s+техническо[её]\s+задани[её]|\bЧТЗ\b', front, re.I):
        kind, appendix = 'ЧТЗ', 'Б'
    elif re.search(r'техническо[её]\s+задани[её]', front, re.I):
        kind, appendix = 'ТЗ', 'А'
    else:
        kind, appendix = 'не определён', None
    profile = {
        'type': kind,
        'template_appendix': appendix,
        'title_excerpt': front[:2200],
        'heading_count': len(headings),
        'headings': headings,
    }
    return d, legacy, profile, units
