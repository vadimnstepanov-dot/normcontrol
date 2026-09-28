"""Bounded evidence packets for subsequent semantic interpretation, without truncation."""
import json
from .store import checksum


class ContextTooLarge(ValueError):pass


def build_packet(cards,fragments,tokenize,context_limit,output_reserve=4096,system_reserve=2048):
    """Count with the connected model tokenizer, not characters-per-token guesses.

    A card plus all dependency evidence is indivisible. A caller can regroup cards,
    but cannot silently discard headings, exceptions or linked rows to fit a window.
    Missing dependency targets stay explicit unknowns.
    """
    if not callable(tokenize):raise ValueError('Actual tokenizer required')
    if min(context_limit,output_reserve,system_reserve)<=0:raise ValueError('Token budgets')
    selected={};requirements=[];unresolved=[]
    for card in cards:
        own=fragments.get(card['locator'])
        if not own:raise ValueError('Missing primary fragment')
        locators={card['locator']}|{c['locator'] for c in card['citations']}
        for dep in card['dependencies']:
            if dep.get('unresolved'):unresolved.append({'requirement':card['id'],**dep})
            elif dep.get('required'):locators.add(dep['target'])
        for loc in sorted(locators):
            if loc not in fragments:raise ValueError('Missing dependency evidence')
            b=fragments[loc]
            selected[loc]={k:b[k] for k in ('locator','exact_text','context_hash','source_sha256')}
        requirements.append({k:card[k] for k in ('id','locator','modality','composition','conditions','exceptions','scope','category')})
    packet={'schema':'normative-context-v1','requirements':requirements,'evidence':list(selected.values()),'unresolved':unresolved}
    raw=json.dumps(packet,ensure_ascii=False,sort_keys=True)
    tokens=tokenize(raw)
    if type(tokens) is not int or tokens<0:raise ValueError('Tokenizer must return a nonnegative count')
    if tokens+output_reserve+system_reserve>context_limit:raise ContextTooLarge('Complete evidence packet exceeds context budget')
    return dict(packet=packet,input_tokens=tokens,output_reserve=output_reserve,system_reserve=system_reserve,digest=checksum(packet))
