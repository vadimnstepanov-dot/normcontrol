"""Narrow source-proven grammar checks; ambiguity never becomes a finding."""
import re
from functools import lru_cache
from .quality_gate import morph

RULE = 'modal-requires-infinitive-v1'
MODAL = re.compile(r'\b(?P<modal>должен|должна|должно|должны)\s+(?P<verb>[а-яё]+)\b', re.I)
QUOTED = re.compile(r'«[^»]*»|"[^"\n]*"|`[^`]*`')

@lru_cache(maxsize=2048)
def finite_proof(word):
    analyzer = morph()
    if analyzer is None:
        return None
    parses = [p for p in analyzer.parse(word) if p.is_known]
    if not parses or any(p.tag.POS != 'VERB' for p in parses):
        return None
    lemmas = {p.normal_form for p in parses}
    if len(lemmas) != 1:
        return None
    lemma = next(iter(lemmas))
    suggestions = [lemma]
    if word.endswith('тся') and not word.endswith('ться'):
        spelling = word[:-2] + 'ься'
        if any(p.is_known and p.tag.POS == 'INFN' for p in analyzer.parse(spelling)):
            suggestions = list(dict.fromkeys([spelling, lemma]))
    return {'word': word, 'known_parses': [str(p.tag) for p in parses],
            'lemma': lemma, 'infinitives': suggestions}

def findings(document):
    for block in document['blocks']:
        if block.get('toc') or block.get('is_heading'):
            continue
        table = block.get('table_context') or block.get('table') or {}
        if re.search(r'идентификатор|имя поля|имя параметра|название атрибута|OLAP', table.get('column_name',''), re.I):
            continue
        text = block['text']
        quoted = [m.span() for m in QUOTED.finditer(text)]
        for match in MODAL.finditer(text):
            if any(start <= match.start() < end for start, end in quoted):
                continue
            # "Кто должен, платит" / "как должно" can contain a clause boundary
            # or an adverbial use rather than this modal construction. Leave
            # ambiguous syntax to the ordinary language pass.
            prefix=re.split(r'[.!?;:\n]',text[:match.start()])[-1]
            if re.search(r'\b(?:кто|что|как|так|когда|котор[а-яё]*|каков[а-яё]*)\b',prefix,re.I):
                continue
            proof = finite_proof(match['verb'].casefold())
            if proof is None:
                continue
            forms = proof['infinitives']
            suggestion = ('Использовать инфинитив «'+forms[0]+'».' if len(forms)==1 else
                'Использовать инфинитив; выбрать вид действия по смыслу: '+ ' / '.join('«'+s+'»' for s in forms)+'.')
            yield {'category':'грамотность','kind':'violation','severity':'minor',
                'issue':'Личная форма глагола вместо инфинитива после «'+match['modal']+'»',
                'explanation':'После «'+match['modal']+'» в этой конструкции требуется инфинитив; «'+match['verb']+'» является личной формой глагола.',
                'suggestion':suggestion,'requirement_id':'','search_query':'',
                'evidence':[{'document':document['id'],'locator':block['locator'],'quote':match[0]}],
                'cpu_rule':RULE,'cpu_proof':{**proof,'source_start':match.start(),'source_end':match.end()},
                'deterministic':True,'auto_edit_safe':False}

def duplicate_key(finding):
    """Merge only the same modal/verb defect, never all errors in a paragraph."""
    if finding.get('requirement_id') or finding.get('category') not in ('грамотность','grammar'):
        return None
    evidence = finding.get('evidence',[])
    if len(evidence) != 1:
        return None
    claim = finding.get('issue','')+' '+finding.get('explanation','')
    explicit = finding.get('cpu_rule') == RULE or re.search(r'инфинитив|личн[а-яё]*\s+форм|тся[^.]{0,40}ться',claim,re.I)
    # A wrong explanation of the same verb-form edit must not replace a proven
    # CPU reason. Do not fold compound issues or unrelated errors in this paragraph.
    verb_form = re.search(r'глагольн[а-яё]*\s+форм|форм[а-яё]*\s+(?:сказуемого|глагола)|(?:написан|форм)[а-яё]*\s+глагол',finding.get('issue',''),re.I)
    compound = re.search(r'\sи\s+(?:согласован|пунктуац|управлен|пропущ|лишн|ошибк)',finding.get('issue',''),re.I)
    if not explicit and (not verb_form or compound):
        return None
    matches = list(MODAL.finditer(evidence[0].get('quote','')))
    if len(matches) != 1:
        return None
    match = matches[0]
    proof = finite_proof(match['verb'].casefold())
    if proof is None:
        return None
    if not explicit and not any(re.search(r'\b'+re.escape(form)+r'\b',finding.get('suggestion',''),re.I) for form in proof['infinitives']):
        return None
    return [RULE,evidence[0].get('document'),evidence[0].get('locator'),
            match['modal'].casefold(),proof['word']]
