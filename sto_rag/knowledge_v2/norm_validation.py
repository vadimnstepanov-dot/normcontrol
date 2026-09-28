"""Independent evidence, coverage and applicability checks for extracted candidates."""
import re
from .applicability import OPERATIONS, evaluate, validate_expression

# Kept separate from extraction patterns: a broad audit catches unhandled wording.
AUDIT_CUES=re.compile(r'долж(?:ен|на|но|ны)|необходим|следует|запрещ|не\s+допуска|рекоменду|допускается|'
                      r'привод(?:ится|ятся)|описыва(?:ется|ются)|указыва(?:ется|ются)|формулируется|требуется',re.I)


def provenance(card,fragments):
    errors=[]
    for cite in card.get('citations',[]):
        fragment=fragments.get(cite.get('locator'))
        if not fragment:errors.append('unknown_locator');continue
        text=fragment['exact_text'];start=cite.get('start');end=cite.get('end')
        if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):errors.append('invalid_span');continue
        if text[start:end]!=cite.get('quote'):errors.append('quote_mismatch')
        if cite.get('context_hash')!=fragment['context_hash']:errors.append('context_changed')
        if cite.get('source_sha256')!=fragment.get('source_sha256'):errors.append('source_changed')
    if not card.get('citations'):errors.append('missing_citations')
    if card.get('operation') not in OPERATIONS:errors.append('unsafe_operation')
    for atom in card.get('obligations',[]):
        origin=atom.get('citation')
        if origin not in card.get('citations',[]):errors.append('ungrounded_obligation')
        if not all(isinstance(atom.get(k),str) and atom[k] for k in ('subject','action','object')):errors.append('unresolved_roles')
        if isinstance(origin,dict) and atom.get('object') and atom['object'] not in origin.get('quote',''):
            errors.append('object_not_in_quote')
    return {'status':'verified' if not errors else 'failed','errors':sorted(set(errors))}


def completeness(card,fragments,dependencies):
    """Lexical/structural completeness is not semantic completeness."""
    loc=card['locator'];text=fragments[loc]['exact_text'];covered=[];reasons=[]
    for atom in card.get('obligations',[]):
        c=atom['citation']
        if c['locator']==loc:covered.append((c['start'],c['end']))
    missing=[m.start() for m in AUDIT_CUES.finditer(text) if not any(a<=m.start()<b for a,b in covered)]
    if missing:reasons.append('uncovered_normative_cues')
    if not card.get('obligations'):reasons.append('no_obligations')
    if any(d.get('unresolved') and d.get('required') for d in dependencies):reasons.append('unresolved_dependency')
    if card.get('ambiguities'):reasons.extend(card['ambiguities'])
    if re.search(r',|;|\bа также\b',text,re.I):reasons.append('complex_syntax_needs_semantic_validation')
    if card.get('example'):reasons.append('example_not_a_general_rule')
    if card.get('definition'):reasons.append('definition_not_an_obligation')
    if any(x.get('kind')=='unknown' for x in card.get('conditions',[])+card.get('exceptions',[])):
        reasons.append('condition_needs_interpretation')
    # Auto acceptance is deliberately limited to single explicit, unconditional modal statements.
    semantic='verified_simple' if (not reasons and len(card['obligations'])==1 and card.get('explicit')
                                 and card.get('modality') in ('mandatory','prohibited','recommended','permitted')) else 'unknown'
    return {'structural':'complete' if not missing and not any(d.get('unresolved') and d.get('required') for d in dependencies) else 'incomplete',
            'semantic':semantic,'uncovered_cue_offsets':missing,'reasons':sorted(set(reasons))}


def applicability(card,facts,verify_evidence=None):
    try:result=evaluate(card['applicability'],facts,verify_evidence)
    except (ValueError,KeyError,TypeError):return {'result':'unknown','evidence':[],'missing':['invalid_profile']}
    if result['result']=='not_applicable' and not result['evidence']:
        return {'result':'unknown','evidence':[],'missing':['non_applicability_without_basis']}
    return result


def audit_coverage(fragments,cards,ledger):
    by_loc={c['locator']:c for c in cards};by_ledger={r['locator']:r for r in ledger};gaps=[]
    for loc,fragment in fragments.items():
        row=by_ledger.get(loc)
        if not row:gaps.append({'locator':loc,'reason':'missing_ledger'});continue
        if AUDIT_CUES.search(fragment['exact_text']) and loc not in by_loc and row['state'] not in ('example','definition','needs_review') and not fragment.get('is_heading'):
            gaps.append({'locator':loc,'reason':'normative_cue_silently_dropped'})
    return {'fragments':len(fragments),'accounted':len(set(fragments)&set(by_ledger)),'gaps':gaps}
