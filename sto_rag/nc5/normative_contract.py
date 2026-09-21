"""Source-grounded obligations, bounded dependencies and verifiable decision scopes.

Never infer whole-document absence from lexical retrieval or a model assertion.
"""
import copy
import re
from collections import Counter, defaultdict
from .common import digest

CONTRACT_VERSION = 1
NORM = re.compile(r'\b(?:долж[а-яё]+|необходимо\b|следует\b|указыва[а-яё]+|привод[а-яё]+|описыва[а-яё]+|содерж(?:ит|ат|атся|аться)|включа[а-яё]+|предусматр[а-яё]+|запрещ[а-яё]+|не допуска[а-яё]+|формулиру[а-яё]+|перечисл[а-яё]+)', re.I)
CONDITION = re.compile(r'\bесли\b|при наличии|при необходимости|при условии|в случае|за исключением|не распространя|по согласован', re.I)
REFERENCE = re.compile(r'(?<!\w)(?P<kind>(?i:таблиц[а-яё]*|табл\.|пункт[а-яё]*)|п\.)\s*(?P<number>(?:[А-ЯA-Z]\.)?\d+(?:\.\d+)*)')


def obligations(card):
    """Split explicit list members, retaining the literal introductory condition."""
    text = card['source_quote']
    lines = text.splitlines()
    items = [line.strip() for line in lines if re.match(r'^\s*(?:[-–—]|[а-яa-z]\))\s+', line)]
    if len(items) < 2:
        items = [text]
        for match in re.finditer(r'\(([^()]+,[^()]+)\)',text):
            if re.search(r'например|и пр|и др|или|в т\.ч',match[1],re.I):continue
            parts=[p.strip() for p in match[1].split(',')]
            if all(re.fullmatch(r'[а-яё-]+',p,re.I) for p in parts):items+=parts
    return [{'id': digest([card['requirement_id'], i, item])[:20], 'quote': item,
             'context': text if len(items) > 1 else '', 'requires_evidence': True}
            for i, item in enumerate(items)]


def enrich_catalog(cat, blocks_by_source):
    """Keep original source quotes/IDs; attach dependencies and explicit uncertainty."""
    cat = copy.deepcopy(cat)
    sources={s['source_id']:s for s in cat.get('sources',[])}
    for card in cat['cards']:
        blocks = blocks_by_source.get(card['source_id'], [])
        card['obligations'] = obligations(card)
        refs = []; unresolved = []; document_refs=[]; visited = set()
        def walk(text, depth=0, source_id=None, appendix=None):
            source_id=source_id or card['source_id']
            appendix=card.get('appendix','') if appendix is None else appendix
            for match in REFERENCE.finditer(text):
                key = ('table' if match['kind'].lower().startswith('табл') else 'clause', match['number'].upper())
                if key[0]=='clause' and re.search(r'наименование.{0,90}ссылк.{0,45}пункт.{0,60}описани',text,re.I):
                    document_refs.append({'reference':match[0],'quote':text,'relation':'template_document_reference','requires_document_link_check':True})
                    continue
                # A named external standard must not resolve to the same-numbered local clause.
                sentence = re.split(r'[;\n]', text[max(0, match.start()-100):match.end()+140])
                nearby = ' '.join(sentence)
                external = bool(re.search(r'(?:ГОСТ|СТО)\s+[\w.]+', nearby)) and 'настоящ' not in nearby.lower()
                # A table explicitly defined in this text is local even when the
                # surrounding sentence cites another standard as its legal basis.
                if key[0]=='table' and re.search(r'(?im)^\s*Таблица\s+'+re.escape(key[1])+r'\s*[–—-]',text):external=False
                target_source=source_id
                target_appendix=appendix
                if external:
                    codes=set(re.findall(r'04\.001\.\d+',nearby))
                    targets=[s for s in sources.values() if any(code in s.get('standard','') for code in codes)]
                    if len(codes)!=1 or len(targets)!=1:
                        unresolved.append({'reference': match[0], 'reason': 'external_source_resolution_required'}); continue
                    target_source=targets[0]['source_id']
                    target_appendix=''
                visit=(target_source,target_appendix,*key)
                if visit in visited:continue
                visited.add(visit)
                if depth >= 3:
                    unresolved.append({'reference': match[0], 'reason': 'dependency_depth_limit'}); continue
                found = []
                for block in blocks_by_source.get(target_source,[]):
                    clause = block.get('clause', '')
                    if key[0] == 'table':
                        hit = bool(re.match(r'Таблица\s+'+re.escape(key[1])+r'(?![\w.])', block.get('locator', ''), re.I))
                    else:
                        local_clause=clause.rsplit(', ',1)[-1]
                        hit = local_clause == key[1] or local_clause.startswith(key[1]+'.')
                    hit = hit and block.get('appendix', '') == target_appendix
                    if hit: found.append(block)
                if not found:
                    unresolved.append({'reference': match[0], 'reason': 'target_not_resolved'}); continue
                for block in found:
                    quote = block.get('raw_text', block['text'])
                    ref = {'source_id': target_source, 'source_sha256': sources.get(target_source,{}).get('sha256',card['source_sha256']),
                           'locator': block['locator'], 'quote': quote, 'relation': key[0], 'reference': match[0]}
                    if not any(x['source_id']==target_source and x['locator'] == ref['locator'] and x['quote'] == quote for x in refs):
                        refs.append(ref); walk(quote, depth+1,target_source,block.get('appendix',''))
        walk(card['source_quote'])
        for parent in card.get('parent_context_refs', []):
            if CONDITION.search(parent['quote']):
                # Atomic-card parents can span several original paragraphs. Resolve
                # their real locators rather than inheriting the parent's first one.
                parent_blocks=[b for b in blocks if b.get('appendix','')==card.get('appendix','') and (parent['quote'] in b.get('raw_text',b['text']) or b.get('raw_text',b['text']) in parent['quote'])]
                for b in parent_blocks:
                    if b.get('raw_text',b['text']).strip():
                        refs.append({'source_id':card['source_id'], 'source_sha256':card['source_sha256'],
                                     'locator':b['locator'],'quote':b.get('raw_text',b['text']),'relation':'condition'})
        # Bound the dependency payload without ever declaring truncated evidence complete.
        kept = []; size = 0
        for ref in refs:
            size += len(ref['quote'])
            if size <= 18000: kept.append(ref)
            else: unresolved.append({'reference':ref['locator'], 'reason':'dependency_budget_exceeded'})
        card['normative_dependencies'] = kept
        card['unresolved_dependencies'] = unresolved
        card['document_reference_templates'] = document_refs
        text = card['source_quote']
        conditions = [s.strip() for s in re.split(r'(?<=[.;])\s+|\n', text) if CONDITION.search(s)]
        stages = [term for term in ('опытной эксплуатации','постоянной эксплуатации','разработки','сопровождения','испытаний') if term in text.lower()]
        card['applicability_contract'] = {
            'document_types':card['document_types'], 'scope':card.get('document_scope','general'),
            'lifecycle_mentions':stages, 'conditions':conditions,
            'requires_project_facts':bool(conditions or stages or card['document_types']==['general']),
            'document_set': 'required' if card.get('check_stage')=='inter' else 'not_established',
            'status':'source_annotated_requires_case_evidence',
            'exclusion_policy':'No negative applicability inferred from missing search hits.'}
        card['contract_version'] = CONTRACT_VERSION
    cat['normative_contract_version'] = CONTRACT_VERSION
    return cat


def scope_proof(finding, payload, documents, card):
    """Recompute exact coverage from source blocks; ignore model inventory assertions."""
    claim = finding.get('scope_claim', {})
    if not isinstance(claim,dict): return {'valid':False,'reason':'invalid_scope_claim'}
    scope = claim.get('kind', 'unknown'); did = claim.get('document')
    doc = next((d for d in documents if d['id']==did), None)
    if not doc or not finding.get('evidence') or any(e.get('document')!=did for e in finding['evidence']):
        return {'valid':False,'reason':'scope_document_mismatch'}
    if card.get('unresolved_dependencies'):
        return {'valid':False,'reason':'unresolved_normative_dependencies'}
    source = [b for b in doc['blocks'] if b.get('text','').strip() and not b.get('toc')]
    supplied = {(b.get('document'),b.get('locator'),b.get('offset',0),b.get('text')) for b in payload.get('blocks',[])}
    sections = claim.get('sections', [])
    if scope == 'section':
        if card.get('document_scope')!='template' or not sections:
            return {'valid':False,'reason':'rule_has_no_section_scope'}
        from .evidence_context import rule_evidence
        _, search = rule_evidence(doc,[card])
        targets = set(search['sections'])
        if not set(sections).issubset(targets):return {'valid':False,'reason':'wrong_target_section'}
        heads = [b for b in source if b['locator'] in sections]
        paths = [b.get('heading_path') for b in heads if b.get('heading_path')]
        source = [b for b in source if b['locator'] in sections or b.get('section') in sections or any(b.get('heading_path',[])[:len(p)]==p for p in paths)]
    elif scope != 'document':
        return {'valid':False,'reason':'unsupported_or_unknown_scope'}
    if scope=='document' and (doc.get('coverage',{}).get('images') or doc.get('coverage',{}).get('objects')):
        return {'valid':False,'reason':'unverified_nontext_content'}
    if not source or not all((did,b['locator'],b.get('offset',0),b['text']) in supplied for b in source):
        return {'valid':False,'reason':'incomplete_scope'}
    # Unresolved references in a section can legitimately delegate its content.
    if any(re.search(r'см\.|смотрите|привед[её]н[а-я]*\s+в|описан[а-я]*\s+в|в\s+документ[ае]|в\s+разделе|в\s+приложении',b['text'],re.I) for b in source):
        return {'valid':False,'reason':'referenced_content_requires_resolution'}
    if any(b.get('image') or b.get('images') for b in source) or any(x.get('locator') in {b['locator'] for b in source} for x in doc.get('coverage',{}).get('images',[])):
        return {'valid':False,'reason':'unverified_image_in_scope'}
    return {'valid':True,'kind':scope,'document':did,'sections':sections,'blocks':len(source),
            'fingerprint':digest([(b['locator'],b.get('offset',0),b['text']) for b in source])}


def validate_positive(row, rule, payload):
    """A positive/NA decision needs literal evidence for EVERY source obligation."""
    row = dict(row)
    if row.get('state') not in ('checked','not_applicable'):return row
    from .checks import validate_evidence
    # Validate against the ACTUAL payload, never a larger local document.
    grouped = defaultdict(list)
    for b in payload.get('blocks',[]):grouped[b['document']].append({**b,'address':b.get('address',b['locator'])})
    docs = [{'id':did,'blocks':bs} for did,bs in grouped.items()]
    checks = row.get('checks', [])
    expected = {x['id'] for x in rule.get('obligations',obligations(rule))}
    reason = None
    if rule.get('unresolved_dependencies'):reason='Не разрешены зависимости нормативного источника'
    if not isinstance(checks,list) or any(not isinstance(x,dict) for x in checks):checks=[]
    ids = [x.get('obligation_id') for x in checks]
    if set(ids)!=expected or len(ids)!=len(set(ids)):reason='Нет отдельного доказательства для каждой обязанности'
    try:
        for check in checks:
            validate_evidence(check.get('evidence',[]),docs)
            if not isinstance(check.get('reason'),str) or not check['reason'].strip():raise ValueError('Пустое обоснование')
            if check.get('state') not in ('checked','not_applicable'):raise ValueError('Не все обязанности проверены')
            if check.get('outcome') not in ('satisfied','violated','not_applicable'):raise ValueError('Не установлено выполнение или нарушение каждой обязанности')
            if (check['state']=='not_applicable')!=(check['outcome']=='not_applicable'):raise ValueError('Противоречивое решение о применимости')
            if row['state']=='not_applicable' and check['state']!='not_applicable':raise ValueError('Смешанные решения не доказывают неприменимость всей карточки')
            if check['state']=='not_applicable' and re.search(r'не найден|не обнаруж|в выборке отсутств|не представлен',check['reason'],re.I):raise ValueError('Отсутствие в выборке не доказывает неприменимость')
            if check['state']=='not_applicable':
                evidence_text=' '.join(e['quote'] for e in check['evidence'])
                if not re.search(r'не\s+(?:предусмотр|использ|примен|входит|выполн|требу)|отсутств|исключ|только|этап|стади|вид\s+работ',evidence_text,re.I):
                    raise ValueError('Неприменимость не подкреплена явным фактом об объекте, виде работ или стадии')
            if check.get('outcome')=='satisfied':
                quotes=[e['quote'] for e in check['evidence']]
                if quotes and all(re.search(r'типов[а-я]* механизм|средствами платформы|специфических требований.{0,30}не предъявляется|в документации на',q,re.I) for q in quotes):
                    raise ValueError('Общая отсылка к типовым средствам не раскрывает отдельную обязанность')
    except (ValueError,KeyError,TypeError) as e:reason=str(e)
    if reason:
        row.update(original_state=row['state'],state='insufficient',reason=reason+'; исходное объяснение: '+row.get('reason',''),positive_evidence_validated=False)
    else:row['positive_evidence_validated']=True
    return row


def applicability_facts(doc):
    """Literal facts only; neither filenames nor keyword absence decide applicability."""
    result=[]
    for b in doc.get('blocks',[]):
        if b.get('toc') or b.get('is_heading'):continue
        if re.search(r'вид\s+работ|стадия\s+(?:создания|разработки|проектирования)|этап\s+(?:работ|разработки|внедрения)|(?:разработка|модернизация|сопровождение)\s+(?:системы|программы)|не\s+предусмотрен[а-я]*',b['text'],re.I):
            result.append({'document':doc['id'],'locator':b['locator'],'quote':b['text']})
    return result[:12]


def coverage_summary(coverage, findings):
    grouped=defaultdict(list)
    for row in coverage:grouped[(row.get('document',''),row.get('requirement_id'))].append(row)
    states=Counter()
    for rows in grouped.values():
        values={r.get('state') for r in rows}
        if values & {'insufficient','unknown','unverified','pending'}:state='unverified'
        elif values <= {'checked','not_applicable'}:state='checked' if 'checked' in values else 'not_applicable'
        else:state='separate_or_skipped'
        states[state]+=1
    return {'unique_document_requirements':len(grouped),'states':dict(states),
            'confirmed_normative_findings':sum(f.get('status')=='confirmed' and bool(f.get('requirement_id')) for f in findings),
            'note':'Полнота проверки требований и число нарушений — независимые показатели.'}
