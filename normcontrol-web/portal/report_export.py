"""Small, dependency-free Office exports for the full review register."""

from collections import Counter
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


TYPE_LABELS = {
    'sto': 'СТО',
    'grammar': 'Грамотность',
    'formatting': 'Оформление',
    'logic': 'Техническая логика',
    'cross': 'Межраздельная логика',
    'inter': 'Междокументная логика',
    'arithmetic': 'Арифметика',
    'other': 'Прочее',
}
STATUS_LABELS = {
    'confirmed': 'Подтверждено', 'candidate': 'Кандидат',
    'verifying': 'На перепроверке', 'question': 'Вопрос',
    'style': 'Редакторское предложение', 'rejected': 'Снято',
}
STAGE_LABELS = {
    'language': 'Грамотность', 'logic': 'Техническая логика',
    'sto': 'Требования СТО', 'cross': 'Связи разделов',
    'inter': 'Связи документов', 'verify': 'Перепроверка',
    'system': 'Конвейер',
}


def finding_type(category):
    value = str(category or '').casefold()
    if 'сто' in value or value in ('sto', 'нормативное нарушение'):
        return 'sto'
    if 'грамот' in value or 'граммат' in value or value in ('language', 'grammar'):
        return 'grammar'
    if 'оформлен' in value:
        return 'formatting'
    if 'междокумент' in value:
        return 'inter'
    if 'межраздел' in value:
        return 'cross'
    if 'арифмет' in value or 'числов' in value:
        return 'arithmetic'
    if 'логик' in value or value == 'logic':
        return 'logic'
    return 'other'


def task_errors(run):
    errors = []
    seen = set()
    sources = [(raw, True) for raw in (run.snapshot or {}).get('task_errors') or []]
    sources.extend((raw, False) for raw in (run.report or {}).get('tasks') or [])
    for raw, known_error in sources:
        if not isinstance(raw, dict) or raw.get('state', 'failed' if known_error else '') != 'failed':
            continue
        item = {
            'id': str(raw.get('id') or raw.get('task_id') or 'ошибка'),
            'stage': str(raw.get('stage') or 'system'),
            'state': str(raw.get('state') or 'failed'),
            'attempts': raw.get('attempts') or 0,
            'error': str(raw.get('error') or 'Причина не записана'),
        }
        item['stage_label'] = STAGE_LABELS.get(item['stage'], item['stage'])
        key = (item['id'], item['stage'], item['error'])
        if key not in seen:
            errors.append(item)
            seen.add(key)
    fatal = (run.snapshot or {}).get('fatal_error')
    if fatal and not any(e['error'] == fatal for e in errors):
        errors.append({'id': 'fatal', 'stage': 'system', 'stage_label': STAGE_LABELS['system'], 'state': 'failed', 'attempts': 1, 'error': str(fatal)})
    return errors


def _text(value, limit=32767):
    value = '' if value is None else str(value)
    value = ''.join(ch for ch in value if ch in '\t\n\r' or 32 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD or 0x10000 <= ord(ch) <= 0x10FFFF)
    return value[:limit - 1] + '…' if len(value) > limit else value


def _xml(value, limit=32767):
    return escape(_text(value, limit), {'"': '&quot;'})


def _location(finding, documents):
    evidence = finding.get('evidence') or []
    names = []
    places = []
    quotes = []
    for item in evidence:
        document = str(item.get('document') or '')
        name = documents.get(document, document)
        if name and name not in names:
            names.append(name)
        address = item.get('address') or item.get('locator') or ''
        if address and address not in places:
            places.append(address)
        if item.get('quote'):
            quotes.append(item['quote'])
    return '; '.join(names), '; '.join(places), '\n\n'.join(quotes)


def _finding_rows(findings, documents):
    rows = []
    for index, finding in enumerate(findings, 1):
        name, address, quote = _location(finding, documents)
        source = finding.get('source') or {}
        disposition = finding.get('human_disposition') or {}
        rows.append([
            index, finding.get('id', ''), STATUS_LABELS.get(finding.get('status'), finding.get('status', '')),
            TYPE_LABELS[finding_type(finding.get('category'))], finding.get('category', ''),
            finding.get('severity', ''), name, address, finding.get('issue', ''),
            finding.get('explanation', ''), quote, finding.get('suggestion', ''),
            ' · '.join(str(v) for v in (source.get('document_name'), source.get('clause')) if v),
            source.get('source_quote', ''), disposition.get('label', 'Не рассмотрено'),
            disposition.get('comment', ''),
        ])
    return rows


def _summary_rows(batch, run, findings, errors, limitations, filters):
    counts = Counter(f.get('status', '') for f in findings)
    rows = [
        ['Пакет', batch.name], ['Состояние проверки', batch.get_status_display()],
        ['Фильтры выгрузки', filters or 'Все записи'],
        ['Замечаний в выгрузке', len(findings)], ['Ошибок задач в выгрузке', len(errors)],
        ['Подтверждено', counts['confirmed']], ['Кандидаты и перепроверка', counts['candidate'] + counts['verifying']],
        ['Вопросы', counts['question']], ['Редакторские предложения', counts['style']],
        ['Снято', counts['rejected']], ['Непроверенных областей и ограничений', len(limitations)],
        ['Примечание', 'Незавершённая проверка и ноль замечаний не подтверждают соответствие.'],
    ]
    rows.extend([f'Ограничение {index}', value] for index, value in enumerate(limitations, 1))
    return rows


def _xlsx_sheet(rows, widths, with_filter=False):
    body = []
    for row_number, row in enumerate(rows, 1):
        cells = []
        for column_number, value in enumerate(row, 1):
            index = column_number
            letters = ''
            while index:
                index, remainder = divmod(index - 1, 26)
                letters = chr(65 + remainder) + letters
            cells.append(f'<c r="{letters}{row_number}" s="{1 if row_number == 1 else 0}" t="inlineStr"><is><t xml:space="preserve">{_xml(value)}</t></is></c>')
        body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths, 1))
    last = max(1, len(rows))
    filter_xml = f'<autoFilter ref="A1:P{last}"/>' if with_filter else ''
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<cols>{columns}</cols><sheetData>{"".join(body)}</sheetData>{filter_xml}</worksheet>')


def make_xlsx(batch, run, findings, errors, limitations, filters):
    documents = {str(d.get('id')): d.get('name', '') for d in (run.report or {}).get('documents', []) if isinstance(d, dict)}
    sheets = [
        ('Сводка', [['Показатель', 'Значение']] + _summary_rows(batch, run, findings, errors, limitations, filters), [43, 100], False),
        ('Замечания', [['№', 'ID', 'Статус', 'Тип', 'Категория', 'Важность', 'Документ', 'Место', 'Замечание', 'Обоснование', 'Цитата', 'Предложение', 'Основание СТО', 'Цитата СТО', 'Решение', 'Комментарий']] + _finding_rows(findings, documents), [7, 24, 20, 24, 26, 16, 35, 35, 56, 65, 65, 65, 44, 65, 24, 50], True),
        ('Ошибки задач', [['ID', 'Этап', 'Состояние', 'Попыток', 'Причина']] + [[e['id'], STAGE_LABELS.get(e['stage'], e['stage']), e['state'], e['attempts'], e['error']] for e in errors], [28, 28, 18, 12, 100], False),
    ]
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, 4))
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + overrides + '</Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="{_xml(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _, _, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, 4)) + '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF18354B"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i, (_, rows, widths, filtered) in enumerate(sheets, 1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml', _xlsx_sheet(rows, widths, filtered))
    return output.getvalue()


def _paragraph(value, heading=False):
    style = '<w:pPr><w:pStyle w:val="Heading2"/><w:keepNext/></w:pPr>' if heading else ''
    bold = '<w:rPr><w:b/></w:rPr>' if heading else ''
    return f'<w:p>{style}<w:r>{bold}<w:t xml:space="preserve">{_xml(value, 1_000_000)}</w:t></w:r></w:p>'


def make_docx(batch, run, findings, errors, limitations, filters):
    documents = {str(d.get('id')): d.get('name', '') for d in (run.report or {}).get('documents', []) if isinstance(d, dict)}
    parts = [_paragraph('Отчёт нормоконтроля — реестр замечаний и ошибок', True)]
    parts.extend(_paragraph(f'{label}: {value}') for label, value in _summary_rows(batch, run, findings, errors, limitations, filters))
    parts.append(_paragraph('Реестр замечаний', True))
    if not findings:
        parts.append(_paragraph('Записей по выбранным фильтрам нет.'))
    for row in _finding_rows(findings, documents):
        parts.append(_paragraph(f'{row[0]}. {row[8]}', True))
        meta=' · '.join(str(value) for value in (f'ID {row[1]}',row[2],row[3],row[4],row[5]) if value)
        parts.append(_paragraph(meta))
        if row[6] or row[7]:parts.append(_paragraph(f'Документ: {row[6] or "не указан"}. Место: {row[7] or "уточняется"}'))
        for label, value in (('Обоснование',row[9]),('Цитата',row[10]),('Предложение',row[11]),('Основание СТО',row[12]),('Цитата СТО',row[13]),('Решение специалиста',row[14]),('Комментарий специалиста',row[15])):
            if value:parts.append(_paragraph(f'{label}: {value}'))
    parts.append(_paragraph('Ошибки задач', True))
    if not errors:
        parts.append(_paragraph('Ошибок задач не зафиксировано.'))
    for index, error in enumerate(errors, 1):
        parts.append(_paragraph(f'{index}. {STAGE_LABELS.get(error["stage"], error["stage"])} — {error["id"]}', True))
        parts.append(_paragraph(f'Попыток: {error["attempts"]}. Причина: {error["error"]}'))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + ''.join(parts) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>')
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', document)
        archive.writestr('word/styles.xml', '<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="Times New Roman"/><w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style><w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:b/><w:color w:val="000000"/><w:sz w:val="26"/></w:rPr></w:style></w:styles>')
    return output.getvalue()
