"""Complete title inventory. Missing retrieval hits never constitute an absence proof."""
import re
import difflib
from .common import digest

def title_key(text):
    text=re.sub(r'^\d+(?:\.\d+)*[.)]?\s+','',text.strip())
    return ' '.join(re.findall(r'[а-яёa-z0-9]+',text.casefold().replace('ё','е')))

def inspect(doc,cat):
    profile=next((p for p in cat['profiles'] if p['id']==doc['profile']['profile_id']),None)
    rule=next((c for c in cat['cards'] if '04.001.1' in c['document_name'] and c['clause']=='7.1.11' and not c['appendix']),None)
    coverage=[];findings=[]
    if not profile or not rule:return findings,[{'state':'unknown','reason':'Нет надёжно определённого шаблона или источника правила структуры'}]
    headings={title_key(h['title']):h for h in doc['headings']};all_titles={title_key(b['text']):b for b in doc['blocks'] if not b.get('toc')};byloc={b['locator']:b for b in doc['blocks']}
    for expected in profile.get('expected_sections',[]):
        key=title_key(expected['title']);entry={'requirement_id':rule['requirement_id'],'template_section':expected['number'],'expected_title':expected['title'],'document':doc['id']}
        if key in headings:coverage.append({**entry,'state':'checked','reason':'Заголовок найден в полном дереве','locator':headings[key]['locator']});continue
        if re.search(r'[<>]|при необходимости|при наличии',expected['title'],re.I):coverage.append({**entry,'state':'unknown','reason':'Шаблон содержит условие или переменную; буквальное сравнение неприменимо'});continue
        if key in all_titles:
            coverage.append({**entry,'state':'insufficient','reason':'Текст заголовка есть в теле, но не распознан как структурный заголовок','locator':all_titles[key]['locator']});continue
        similar=difflib.get_close_matches(key,list(headings),n=1,cutoff=.58)
        anchor=byloc.get(headings[similar[0]]['locator']) if similar else next((b for b in doc['blocks'] if b.get('is_heading')),None)
        coverage.append({**entry,'state':'insufficient','reason':'Точное название не найдено в полном дереве и полном тексте; требуется проверка применимости/переименования'})
        if not anchor:continue
        findings.append({'category':'структура СТО','severity':'major','kind':'violation','issue':'Не найдено название раздела шаблона: '+expected['title'],'explanation':'Проверен полный перечень заголовков и полный текст документа. Точное название «'+expected['title']+'» не обнаружено. Это кандидат на нарушение структуры; применимость и возможное переименование требуют перепроверки.','suggestion':'Сопоставить структуру с шаблоном, восстановить обязательный раздел либо объяснить допустимое исключение.','evidence':[{'document':doc['id'],'locator':anchor['locator'],'quote':anchor['text']}],'requirement_id':rule['requirement_id'],'search_query':expected['title'],'template_source':{**expected,'source_id':profile['source_id'],'appendix':profile['appendix']},'absence_proof':{'kind':'exact_title_inventory','structure_hash':doc['structure_hash'],'expected':expected['title'],'full_text_digest':digest([b['text'] for b in doc['blocks']]),'semantic_absence_proven':False}})
    return findings,coverage
