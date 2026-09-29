"""Presentation names derived from evidence-backed requisites; originals stay intact."""
import re

STANDARD = re.compile(r'^(СТО\s+РЖД\s+[\d]+(?:\.[\d]+)+)(?:\s*[–—-]\s*(\d{4}))?', re.I)

def clean(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = re.sub(r'\s*·\s*утв\.\s*[^·]+$', '', text, flags=re.I)
    return re.sub(r'\s*\([^()]+\s+от\s+\d{2}\.\d{2}\.\d{4}\)\s*$', '', text).strip()

def names(identification, filename):
    fields = identification.get('fields', {})
    def value(key): return (fields.get(key, {}).get('value') or '').strip()
    short, full = clean(value('short_title')), clean(value('full_title'))
    match = STANDARD.match(short) or STANDARD.match(full)
    if match:
        identifier = re.sub(r'\s+', ' ', match[1])
        version = identifier + ('–' + match[2] if match[2] else '')
        title = (short if STANDARD.match(short) else full)[match.end():].strip(' ·.–—-')
        if not title:
            title = full
        # The series header repeats the subject; keep the particular standard title.
        title = re.sub(r'^Автоматизированные системы и программные средства ОАО\s*[«"]РЖД[»"]\.\s*', '', title, flags=re.I)
        abbreviated = re.sub(r'автоматизированн(?:ых|ые) систем(?:ы)? и программн(?:ых|ые) средств(?:а)?', 'АС (ПС)', title, flags=re.I)
        short = identifier + (' · ' + abbreviated if abbreviated else '')
        full_title = full or title
        full_match = STANDARD.match(full_title)
        if full_match:
            full_title = full_title[full_match.end():].strip(' ·.–—-')
            if not match[2] and full_match[2]: version = identifier + '–' + full_match[2]
        full_title = re.sub(r'^Автоматизированные системы и программные средства ОАО\s*[«"]РЖД[»"]\.\s*', '', full_title, flags=re.I)
        full = version + (' · ' + full_title if full_title else '')
    else:
        short = short or full or filename
        short = re.sub(r'автоматизированн(?:ых|ые) систем(?:ы)? и программн(?:ых|ые) средств(?:а)?', 'АС (ПС)', short, flags=re.I)
        full = full or clean(value('short_title')) or filename
    number, date = value('approval_document_number'), value('approval_document_date')
    if number:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): date = date[8:10]+'.'+date[5:7]+'.'+date[:4]
        full += ' (' + number + (' от ' + date if date else '') + ')'
    if value('approval_date'): full += ' · утв. ' + value('approval_date')
    return short, full
