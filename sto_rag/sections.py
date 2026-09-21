"""Bounded DOCX section extraction. Original files are never modified."""
import json,re,xml.etree.ElementTree as E
from word_compact import package,plain,N,Q

def value(parent,path):
    e=parent.find(path,N) if parent is not None else None
    return e.get(Q+'val') if e is not None else None

def plan(path,max_chars=6000):
    d=package(path);styles={}
    if 'word/styles.xml' in d['files']:
        sr=E.fromstring(d['files']['word/styles.xml'])
        styles={s.get(Q+'styleId'):s for s in sr.findall('w:style',N)}
    else:sr=E.Element('empty')
    def inherited(p):
        sid=value(p,'w:pPr/w:pStyle');chain=[];seen=set();cur=sid
        while cur and cur in styles and cur not in seen:
            seen.add(cur);s=styles[cur];chain.insert(0,s);cur=value(s,'w:basedOn')
        return sid,[sr.find('w:docDefaults',N)]+chain+[p]
    def properties(p):
        sid,chain=inherited(p);out={'style':sid or 'default'}
        paths={'outline':'w:pPr/w:outlineLvl','align':'w:pPr/w:jc','numbering_id':'w:pPr/w:numPr/w:numId','numbering_level':'w:pPr/w:numPr/w:ilvl'}
        for s in chain:
            if s is None:continue
            for k,v in paths.items():
                x=value(s,v)
                if x is not None:out[k]=x
            for k,v in [('spacing','w:pPr/w:spacing'),('indent','w:pPr/w:ind')]:
                e=s.find(v,N)
                if e is not None:out.setdefault(k,{}).update({a.split('}')[-1]:b for a,b in e.attrib.items()})
        # Font declarations are reported as OOXML metadata, not a rendered-page measurement.
        defaults={}
        for s in chain:
            if s is None:continue
            rp=s.find('w:rPr',N)
            if s.tag.endswith('docDefaults'):rp=s.find('w:rPrDefault/w:rPr',N)
            if rp is not None:
                for e in rp:
                    if e.tag in (Q+'rFonts',Q+'sz',Q+'b',Q+'i'):
                        defaults[e.tag.split('}')[-1]]={a.split('}')[-1]:b for a,b in e.attrib.items()} or {'val':'1'}
        fonts=[]
        for r in p.findall('w:r',N):
            if not plain(r):continue
            f=dict(defaults);rp=r.find('w:rPr',N)
            if rp is not None:
                for e in rp:
                    if e.tag in (Q+'rFonts',Q+'sz',Q+'b',Q+'i'):
                        k=e.tag.split('}')[-1];f[k]={**f.get(k,{}),**({a.split('}')[-1]:b for a,b in e.attrib.items()} or {'val':'1'})}
            if f and f not in fonts:fonts.append(f)
        out['run_formats']=fonts[:4]
        if len(fonts)>4:out['additional_run_formats']=len(fonts)-4
        if p.find('.//w:drawing',N) is not None:out['has_image']=True
        if p.find('.//w:fldChar',N) is not None or p.find('.//w:fldSimple',N) is not None:out['has_field']=True
        if 'numbering_id' in out:out['numbering_note']='Номер списка не вычислен; не угадывать отображаемую метку.'
        return out
    table_map={}
    for ti,t in enumerate(d['root'].findall('.//w:tbl',N),1):
        for ri,row in enumerate(t.findall('w:tr',N),1):
            for ci,cell in enumerate(row.findall('w:tc',N),1):
                for p in cell.findall('.//w:p',N):table_map[id(p)]={'table':ti,'row':ri,'cell':ci}
    blocks=[];section=0;title='Начало документа';headings=[]
    for p,r in zip(d['paragraphs'],d['records']):
        fmt=properties(p);txt=r['text'];sid=fmt['style'];s=styles.get(sid)
        name=value(s,'w:name') or sid
        heading=not r['table'] and bool(txt.strip()) and (fmt.get('outline') in tuple(map(str,range(9))) or re.search(r'heading|заголовок',name,re.I) or (len(txt)<180 and re.match(r'^\d+(?:\.\d+){0,4}[.\s]+\S',txt)))
        if heading:
            section+=1;title=txt[:180];headings.append({'section':section,'title':title,'paragraph':r['id']})
        if id(p) in table_map:fmt.update(table_map[id(p)])
        blocks.append({'text':txt,'locator':r['id'],'section':section,'section_title':title,'format':fmt})
    # Include text of headers/footers/notes as separate sections; no page-position claims.
    for name,raw in d['files'].items():
        if not re.match(r'word/(header\d+|footer\d+|footnotes|endnotes)\.xml$',name):continue
        section+=1;title=name;headings.append({'section':section,'title':name})
        for i,p in enumerate(E.fromstring(raw).findall('.//w:p',N),1):
            blocks.append({'text':plain(p),'locator':f'{name}:p{i}','section':section,'section_title':title,'format':properties(p)})
    groups=[];group=[];size=0;formats=set()
    for b in blocks:
        # Oversized paragraphs are split with exact character offsets, never dropped.
        for offset in range(0,max(1,len(b['text'])),max_chars):
            part={**b,'text':b['text'][offset:offset+max_chars],'offset':offset}
            fmt_key=json.dumps({k:v for k,v in b['format'].items() if k not in ('table','row','cell')},ensure_ascii=False,sort_keys=True)
            weight=len(part['text'])+100+(len(fmt_key) if fmt_key not in formats else 0)
            if group and (group[0]['section']!=b['section'] or size+weight>max_chars+2000):
                groups.append(group);group=[];size=0;formats=set()
                weight=len(part['text'])+100+len(fmt_key)
            group.append(part);size+=weight;formats.add(fmt_key)
    if group:groups.append(group)
    layout=[]
    for sp in d['root'].findall('.//w:sectPr',N):
        row={}
        for name in ('pgSz','pgMar','cols'):
            e=sp.find('w:'+name,N)
            if e is not None:row[name]={k.split('}')[-1]:v for k,v in e.attrib.items()}
        if row not in layout:layout.append(row)
    return {'path':d['path'],'sha256':d['sha256'],'groups':groups,'headings':headings,'layout':layout,
            'paragraphs':len(blocks),'warnings':['Проверяется извлечённый текст тела, колонтитулов и сносок; изображения и отрисовка страниц не проверены.',
            'Свойства OOXML переданы частично; условные стили таблиц, символьные стили и тема шрифтов не разрешены полностью. Размер sz — полупункты, поля и отступы — единицы OOXML, не пиксели.',
            'Разделы определены по стилям, уровням и явным заголовкам; распознавание заголовков эвристическое. Отображаемая автоматическая нумерация не восстановлена.']}

def model_metadata(group):
    definitions=[];paragraphs=[]
    for b in group:
        fmt={k:v for k,v in b.get('format',{}).items() if k not in ('table','row','cell')}
        if fmt not in definitions:definitions.append(fmt)
        paragraphs.append({'id':b['locator'],'offset':b.get('offset',0),'format':definitions.index(fmt),
                           **{k:v for k,v in b.get('format',{}).items() if k in ('table','row','cell')}})
    return {'paragraphs':paragraphs,'formats':definitions}
