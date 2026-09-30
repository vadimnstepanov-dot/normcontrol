"""Recover exact source spans when a model only changes whitespace."""
import re

BLOCK_EVIDENCE_STAGES=frozenset(('check','verify','visual_check','visual_verify'))


def attach_source_quotes(decisions, documents):
    """Hydrate block-only model references from the exact submitted source.

    Existing quoted responses still go through normal quote validation. No
    source lookup outside this request or repair of a foreign ID is allowed.
    """
    blocks={b['id']:b['text'] for b in documents}
    for decision in decisions:
        for evidence in decision.get('evidence',[]):
            if 'quote' in evidence:continue
            text=blocks.get(evidence.get('block_id'))
            if not isinstance(text,str) or not text.strip():
                raise ValueError('Evidence block is not in the submitted corpus')
            evidence['quote']=text
    return decisions


def source_quote(text, quote):
    if not isinstance(text, str) or not isinstance(quote, str) or not quote.strip():
        raise ValueError('Evidence quote is not in the submitted corpus')
    if quote in text:
        return quote
    # No case folding, punctuation repair, numeric changes, fuzzy matching, or
    # lookup in another block. The stored quote is always a verbatim source span.
    pattern = r'\s+'.join(re.escape(part) for part in re.split(r'\s+', quote.strip()))
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError('Evidence quote is not in the submitted corpus')
    return matches[0].group(0)
