"""Conservative release screening. A machine screen is never expert approval.

Keep every card and every coverage gap; only the executable view is reduced.
No similarity merge and no arbitrary quota of requirements are permitted.
"""
import math,re
from collections import Counter
from .store import checksum

VERSION = 'screened-20260927.1'
MODE = 'screened_test'
LABELS = {
    'source_gap': 'Исходный пункт требует проверки полноты',
    'provenance': 'Не подтверждено происхождение',
    'incomplete': 'Не подтверждена полнота формулировки',
    'ambiguity': 'Есть смысловая неопределённость',
    'condition': 'Условия перенесены не полностью',
    'dependency': 'Не раскрыта нормативная ссылка',
    'applicability': 'Нужно уточнить применимость',
    'modality': 'Не установлена обязательность',
    'confidence': 'Недостаточная уверенность модели',
    'composition': 'Не раскрыт состав обязанности',
    'profile': 'Не определён профиль документа',
    'no_obligation': 'Не выделено проверяемое обязательство',
    'reference': 'Справочная запись, рекомендация или разрешение',
    'duplicate': 'Точный повтор в том же нормативном контексте',
    'context_audit': 'Контекстная перепроверка требует решения эксперта',
    'implicit_context': 'Нужна явная привязка к условию или родительскому пункту',
    'context_statement': 'Вводная или отсылочная фраза без самостоятельной проверки',
    'administrative_action': 'Организационное поручение: применимость к документу требует эксперта',
    'dependency_context': 'Нормативная зависимость отсутствует в закреплённых цитатах',
    'example_scope': 'Текст относится к примеру: обязательность требует решения эксперта',
}


def example_scopes(blocks):
    """A standalone example marker governs its section, not only two neighbours.

    Ambiguous boundaries stay conservative. An actual next structural heading
    or explicit end marker closes the scope; table captions do not.
    """
    scopes={};marker=None
    for b in sorted(blocks,key=lambda b:b.get('structure',{}).get('document_position',0)):
        text=b.get('exact_text','').strip();structure=b.get('structure',{})
        if re.fullmatch(r'(?i)примеры?(?:\s+\d+)?\s*[-–—:.]?',text):marker=b['locator']
        elif structure.get('is_heading') or re.fullmatch(r'(?i)конец\s+примера\s*[.]?',text):marker=None
        if marker:scopes[b['locator']]=marker
    return scopes


def source_example_scopes(store,analysis):
    import json
    with store.connection() as db:
        rows=[json.loads(r[0]) for r in db.execute("SELECT payload FROM records WHERE kind='structured_fragment' AND json_extract(payload,'$.source_revision[0]')=?",(analysis['source_id'],))]
    return example_scopes([b for b in rows if b.get('parse_ref')==analysis.get('parse_ref')])


def unknown(expression):
    if not isinstance(expression, dict) or not expression: return True
    if 'unknown' in expression: return True
    if 'fact' in expression:
        fact=expression['fact']
        return not isinstance(fact,dict) or not fact.get('name') or not fact.get('in')
    if 'not' in expression: return unknown(expression['not'])
    for op in ('all_of','any_of'):
        if op in expression:
            return not expression[op] or any(unknown(x) for x in expression[op])
    return True


def assessment(card, gaps=(), *, provenance=True, profile=True, approved=False):
    reasons=[];validation=card.get('validation',{})
    if not provenance or validation.get('provenance',{}).get('status')!='verified':reasons.append('provenance')
    if validation.get('completeness',{}).get('semantic') not in ('verified','verified_simple','model_reviewed') or validation.get('completeness',{}).get('structural')=='incomplete':reasons.append('incomplete')
    resolution=card.get('expert_resolution',{})
    resolved=bool(resolution.get('reason') and resolution.get('questions_digest')==checksum([card.get('ambiguities',[]),card.get('condition_review',{})]))
    if card.get('ambiguities') and not resolved:reasons.append('ambiguity')
    if card.get('condition_review',{}).get('status')=='needs_review' and not resolved:reasons.append('condition')
    if any(d.get('unresolved') for d in card.get('dependencies',[])):reasons.append('dependency')
    cited={c.get('locator') for c in card.get('citations',[])}|{card.get('locator')}
    if any(d.get('required') and not d.get('unresolved') and (d.get('target') or d.get('target_locator')) not in cited for d in card.get('dependencies',[])):reasons.append('dependency_context')
    if cited & set(gaps) and not approved:reasons.append('source_gap')
    if not profile:reasons.append('profile')
    # Definitions/permissions remain reference material, never mandatory checks.
    reference=card.get('entity_type') in ('definition','permission','recommendation','assumption')
    if not reference:
        quotes=[c.get('quote','').strip() for c in card.get('citations',[]) if c.get('locator')==card.get('locator')]
        primary=' '.join(quotes)
        if re.match(r'(?i)^\s*(?:\d+[.)]\s*)?(утвердить\b|ввести в действие\b|признать утратившим\b|возложить контроль\b|контроль за исполнением\b|начальникам\b|руководителям\b|обеспечить доведение\b)',primary):reasons.append('administrative_action')
        if (re.match(r'(?i)^(при этом|там же|в этом случае|при наличии|при необходимости|если\b|[-–—]\s)',primary)
            and not card.get('conditions') and not any(d.get('required') and not d.get('unresolved') for d in card.get('dependencies',[]))):reasons.append('implicit_context')
        if re.search(r'(?i)(требования.{0,100}(содержатся|приведены|изложены).{0,50}(шаблон|приложени)|(?:настоящий|данный) шаблон определяет требования|в соответствии с настоящим стандартом\s*[.]?$)',primary):reasons.append('context_statement')
        if unknown(card.get('applicability')):reasons.append('applicability')
        if card.get('modality') not in ('mandatory','prohibited'):reasons.append('modality')
        if not card.get('obligations'):reasons.append('no_obligation')
        logic=card.get('composition',{})
        if len(logic)!=1 or next(iter(logic),'unknown') not in ('atom','all_of','any_of') or (next(iter(logic),'atom')!='atom' and len(next(iter(logic.values()),[]))<2):reasons.append('composition')
    confidence=card.get('model_confidence',0)
    if not approved and (not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or confidence<0.9):reasons.append('confidence')
    return dict(version=VERSION,status='candidate' if reasons else 'reference' if reference else 'ready',
                reasons=list(dict.fromkeys(reasons or (['reference'] if reference else []))),expert_approval=False)


def duplicate_key(card, source):
    # Exact meaning AND evidence in the same source/profile. Numbers, conditions,
    # exceptions, modality and dependencies cannot disappear in a merge.
    keys=('description','entity_type','modality','obligations','conditions','exceptions','parameters',
          'applicability','dependencies','composition','composition_group','profile_id','local_profile','citations')
    return checksum([source,{k:card.get(k) for k in keys}])


def screen(rows, analysis_gaps):
    seen={};out={}
    for row in sorted(rows,key=lambda r:r['item']['id']):
        item,card=row['item'],row['card']
        q=assessment(card,analysis_gaps.get(item['source_id'],[]),provenance=row['provenance'],profile=bool(card.get('profile_ids')))
        if item['status'] in ('rejected','superseded'):q.update(status='excluded',reasons=[item['status']])
        if q['status'] in ('ready','reference'):
            key=duplicate_key(card,item['source_id'])
            if key in seen:q.update(status='duplicate',reasons=['duplicate'],duplicate_of=seen[key])
            else:seen[key]=item['id']
        out[item['id']]=q
    return out


def counts(catalog):
    return dict(Counter((c.get('quality') or {}).get('status','unscreened') for c in catalog))


def limitation(policy):
    q=policy.get('quality',{})
    if not q:return []
    if q.get('mode')=='imported_reference':
        return [q['limitation']+' Неподтверждённые требования не дают окончательных нарушений; карточки с блокирующими вопросами не исполняются моделью.']
    return [f"Тестовый нормативный выпуск: рабочих требований {q['counts'].get('ready',0)}, "
            f"кандидатов вне автоматической проверки {q['counts'].get('candidate',0)}. "
            f"Вопросов полноты источников {q.get('coverage_gap_count',0)}, "
            f"вопросов распознавания {q.get('source_gap_count',0)}. "
            "Процент проверки относится к отобранным требованиям; полнота всего СТО не подтверждена. "
            "Неподтверждённые экспертом требования дают только предварительные замечания."]
