"""Opt-in analysis of source-grounded candidates, without expert promotion."""
VERSION = 'source-grounded-candidates-v1'
SOFT_ISSUES = frozenset({'semantic_ambiguity', 'critical_context_incomplete',
    'Normative extraction requires review', 'Normative semantic completeness unknown'})
GUIDANCE = ('Требование-кандидат нормативной базы не подтверждено экспертом. '
    'Проверь формулировку обязанности по точному первоисточнику context, включая '
    'условия и исключения. Если исходный текст не подтверждает обязанность или '
    'её смысл неоднозначен, верни unknown. Не заполняй пробелы нормами из памяти. '
    'Даже выявленное несоответствие является только предварительным замечанием.')


def eligible(card, context):
    quality = card.get('quality', {})
    if quality.get('status') != 'candidate': return False
    if card.get('state') in ('example', 'definition', 'rejected', 'superseded'): return False
    if card.get('validation', {}).get('provenance', {}).get('status') != 'verified': return False
    if not context or any(not f.get('exact_text', '').strip() for f in context): return False
    # A failed context audit, example, unresolved condition or missing dependency
    # is not equivalent to the pending expert review of imported source quotes.
    if set(quality.get('reasons', [])) - SOFT_ISSUES: return False
    return True


def enable(row, card, enabled):
    if not enabled or row['modality'] not in ('mandatory', 'prohibited') or not eligible(card, row['context']): return row
    row['candidate_analysis'] = dict(version=VERSION, guidance=GUIDANCE,
        limitations=list(row['issues']), ambiguities=card.get('ambiguities', []),
        semantic_completeness=card.get('validation', {}).get('completeness', {}))
    row['preliminary_only'] = True
    row['execution_issues'] = [x for x in row['execution_issues'] if x not in SOFT_ISSUES and x != 'quality_candidate']
    notice = 'Неподтверждённое требование-кандидат: результат требует экспертной проверки'
    if notice not in row['issues']: row['issues'].append(notice)
    return row
