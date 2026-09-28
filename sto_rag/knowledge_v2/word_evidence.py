"""Match immutable OOXML evidence to actual LibreOffice labels, never guessed clause numbers."""
from collections import Counter
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import subprocess


def normalized(text):
    return re.sub(r'\s+','',text).replace('\u00ad','')


def apply_probe(result,probe,pdf):
    original=[b for b in result['blocks'] if re.fullmatch(r'p\d+',b['locator'])]
    rendered=[p for p in probe['paragraphs'] if p['text'].strip()]
    a=[normalized(b['exact_text']) for b in original];b=[normalized(p['text']) for p in rendered]
    ac,bc=Counter(a),Counter(b);matched=set()
    for start,end,size in SequenceMatcher(None,a,b,autojunk=False).get_matching_blocks():
        anchored=any(ac[a[i]]==bc[a[i]]==1 for i in range(start,start+size))
        if not anchored:continue
        for off in range(size):
            source=original[start+off];observation=rendered[end+off]
            source['computed_number_label']=source.get('number_label','')
            source.update(number_label=observation['label'],numbering_verified=True,numbering_method='libreoffice_render',
                word_page=observation.get('page'),word_render_pdf=pdf,
                numbering_evidence=dict(method=probe['method'],exact_text_match=True,ordered_context_match=True,
                                        renderer_paragraph=end+off+1))
            matched.add(source['locator'])
    # Only remove issues proven resolved for this exact source occurrence.
    result['coverage']=[x for x in result['coverage'] if not
        (x['locator'].endswith('/numbering') and x['locator'].split('/')[0] in matched)]
    outstanding=[x for x in original if x.get('numbering',{}).get('num') not in (None,'0') and x['locator'] not in matched]
    if not outstanding:
        result['coverage']=[x for x in result['coverage'] if x['locator']!='document/numbering']
    by={x['locator']:x for x in original}
    for item in result['blocks']+result['tables']:
        item['heading_addresses']=[(by[r].get('number_label','')+' '+by[r]['exact_text']).strip()
            if by[r].get('numbering_verified') else by[r]['exact_text'] for r in item.get('heading_refs',[]) if r in by]
    tables=[t for t in result['tables'] if re.fullmatch(r't\d+',t['id'])]
    tables.sort(key=lambda t:int(t['id'][1:]))
    if len(tables)==len(probe['tables']):
        for t,p in zip(tables,probe['tables']):
            a=Counter(normalized(c['exact_text']) for c in t['cells'] if c['exact_text'].strip())
            b=Counter(normalized(c['text']) for c in p['cells'] if c['text'].strip())
            if a==b:t.update(word_page=p.get('page'),word_render_pdf=pdf,word_table_match='ordered_exact_cell_multiset')
    result['word_render']=dict(pdf=pdf,method=probe['method'],page_basis=probe['page_basis'],matched_paragraphs=len(matched),
        total_paragraphs=len(original),unmatched_numbered=[x['locator'] for x in outstanding])


def enrich_word(result,source,run,cache,cancel=lambda:False):
    python=os.environ.get('KNOWLEDGE_UNO_PYTHON','/usr/bin/python3')
    if not Path(python).exists() or (python=='/usr/bin/python3' and not Path('/usr/lib/python3/dist-packages/uno.py').exists()):
        result['coverage'].append(dict(locator='document/render',state='needs_review',reason='LibreOffice UNO runtime unavailable'))
        return
    key=cache.key('office-labels',{'source':str(Path(source).name)})
    probe=cache.get(key);out=run/'conversions'/'office'
    if not probe or not (out/'source-render.pdf').is_file():
        if cancel():raise InterruptedError('Paused before Word render')
        try:
            subprocess.run([python,str(Path(__file__).with_name('office_probe.py')),str(source),str(out)],
                           check=True,capture_output=True,timeout=240)
        except (OSError,subprocess.SubprocessError) as exc:
            result['coverage'].append(dict(locator='document/render',state='needs_review',reason='Word render failed: '+type(exc).__name__))
            return
        probe=json.loads((out/'office-probe.json').read_text(encoding='utf8'));cache.put(key,probe)
    apply_probe(result,probe,(out/'source-render.pdf').relative_to(run).as_posix())
