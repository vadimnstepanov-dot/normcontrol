"""Shared glossary identity: no similarity-based merging or model conflict arbitration."""
import re
import unicodedata

KINDS={'term':'Термин и определение','symbol':'Обозначение','abbreviation':'Сокращение'}


def normalized(text):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',text).strip()).casefold()


def identity(card):
    term=str(card.get('term','')).strip()
    kind=card.get('glossary_kind') or 'term'
    if not term or len(term)>500 or kind not in KINDS:raise ValueError('Glossary name/type required')
    return kind+':'+normalized(term),kind,term


def frozen_active(selection, loaded):
    """Validate a pinned, area-wide expert choice before indexing definitions."""
    choices=selection.get('glossary')
    if choices is None:return None  # Old releases retain their pinned behavior.
    groups={}
    for row in loaded:
        if row['card'].get('entity_type')!='definition' or row['item']['status'] in ('rejected','superseded'):continue
        try:key,_,_=identity(row['card'])
        except ValueError:continue  # Unnamed records stay candidates, outside the active glossary.
        groups.setdefault(key,set()).add(row['item']['id'])
    if not isinstance(choices,list):raise ValueError('Invalid glossary selection')
    active=set();seen=set()
    for choice in choices:
        if set(choice)!={'key','active','revision'} or choice['key'] in seen or type(choice['revision']) is not int or choice['revision']<1:raise ValueError('Invalid glossary choice')
        if choice['key'] not in groups or choice['active'] not in groups[choice['key']]:raise ValueError('Glossary choice outside pinned source material')
        seen.add(choice['key']);active.add(choice['active'])
    if seen!=set(groups):raise ValueError('Missing glossary choice')
    return active
