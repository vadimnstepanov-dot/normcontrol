"""Cheap recall pass: hypotheses for verification, never automatic verdicts."""
import difflib,re
from collections import Counter
from .quality_gate import morph

WORDS=re.compile(r'\b[А-Яа-яЁё]{5,}\b')
def candidates(doc):
    m=morph()
    if not m:return []
    blocks=[b for b in doc['blocks'] if not b.get('toc')]
    words=Counter(w.lower() for b in blocks for w in WORDS.findall(b['text']))
    known={w for w in words if m.word_is_known(w)}
    heading_terms={m.parse(w)[0].normal_form for b in blocks if b.get('is_heading') for w in WORDS.findall(b['text'])}
    out=[]
    def add(b,issue,explanation,suggestion):
        out.append({'category':'грамотность','kind':'violation','severity':'minor','issue':issue,'explanation':explanation,'suggestion':suggestion,'requirement_id':'','search_query':'',
                    'candidate_method':'local_language_screen','evidence':[{'document':doc['id'],'locator':b['locator'],'quote':b['text']}]})
    for b in blocks:
        text=b['text'];column=b.get('table_context',{}).get('column_name','')
        if re.search(r'реквизит|название атрибута|имя поля|идентификатор',column,re.I):continue
        bracket_text=re.sub(r'^\s*(?:\d+|[а-яА-Яa-zA-Z])\)\s*','',text)
        if '(' in bracket_text and bracket_text.count('(')!=bracket_text.count(')'):
            add(b,'Непарные скобки в тексте','Количество открывающих и закрывающих скобок в абзаце различается. Нужно проверить продолжение фразы и границы ячейки.','Проверить полную фразу; восстановить пару скобок, если она не продолжается в соседнем абзаце.')
        for word in dict.fromkeys(WORDS.findall(text)):
            w=word.lower()
            if word!=w or m.word_is_known(w) or m.parse(w)[0].normal_form in heading_terms:continue
            pool=[v for v in known if abs(len(v)-len(w))<=1 and v[:3]==w[:3]]
            near=difflib.get_close_matches(w,pool,n=2,cutoff=.9)
            if len(near)!=1:continue
            target=near[0];ops=[o for o in difflib.SequenceMatcher(a=w,b=target).get_opcodes() if o[0]!='equal']
            if len(ops)!=1 or max(ops[0][2]-ops[0][1],ops[0][4]-ops[0][3])!=1:continue
            add(b,'Возможная опечатка «'+word+'»','Словоформа не найдена общим словарём; в этом же документе встречается близкая словарная форма «'+target+'». Это гипотеза, а не доказательство.','Проверить по смыслу замену «'+word+'» на «'+target+'»; названия и термины не менять без основания.')
    return out
