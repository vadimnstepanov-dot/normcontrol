"""Measure inherited Word properties; page-dependent checks require a rendered artifact."""
import re
import subprocess
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as E
from .common import DATA

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}';N={'w':W[1:-1]}
R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

def props(el):
    if el is None:return {}
    return {x.tag[len(W):]:{k.split('}')[-1]:v for k,v in x.attrib.items()} or {'val':'1'} for x in el if x.tag.startswith(W)}

def merge(a,b):
    for k,v in b.items():a[k]={**a.get(k,{}),**v}
    return a

def measure(path):
    with ZipFile(path) as z:
        root=E.fromstring(z.read('word/document.xml'));styles=E.fromstring(z.read('word/styles.xml')) if 'word/styles.xml' in z.namelist() else E.Element('empty')
        theme=E.fromstring(z.read('word/theme/theme1.xml')) if 'word/theme/theme1.xml' in z.namelist() else None
    ans={'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
    theme_fonts={}
    if theme is not None:
        for kind in ('major','minor'):
            latin=theme.find('.//a:fontScheme/a:'+kind+'Font/a:latin',ans)
            if latin is not None:theme_fonts[kind]=latin.get('typeface')
    sm={s.get(W+'styleId'):s for s in styles.findall('w:style',N)};defaults={k:props(styles.find('w:docDefaults/w:'+k+'Default/w:'+k,N)) for k in ('pPr','rPr')}
    default_style=next((s.get(W+'styleId') for s in sm.values() if s.get(W+'type')=='paragraph' and s.get(W+'default')=='1'),None)
    def chain(sid):
        out=[];seen=set()
        while sid in sm and sid not in seen:
            seen.add(sid);s=sm[sid];out.insert(0,s);base=s.find('w:basedOn',N);sid=base.get(W+'val') if base is not None else None
        return out
    ptable={}
    for ti,t in enumerate(root.findall('.//w:tbl',N),1):
        ts=t.find('w:tblPr/w:tblStyle',N);sid=ts.get(W+'val') if ts is not None else None
        for p in t.findall('.//w:p',N):ptable[id(p)]=(ti,sid)
    result=[]
    for i,p in enumerate(root.findall('.//w:body//w:p',N),1):
        ps=p.find('w:pPr/w:pStyle',N);sid=ps.get(W+'val') if ps is not None else default_style
        layers=chain(sid);pp={};rp={};merge(pp,defaults['pPr']);merge(rp,defaults['rPr']);unresolved=[]
        if id(p) in ptable:
            ti,tsid=ptable[id(p)]
            for s in chain(tsid):
                merge(pp,props(s.find('w:pPr',N)));merge(rp,props(s.find('w:rPr',N)))
                if s.findall('w:tblStylePr',N):unresolved.append('Условные свойства стиля таблицы требуют отдельного разрешения')
        for s in layers:merge(pp,props(s.find('w:pPr',N)));merge(rp,props(s.find('w:rPr',N)))
        merge(pp,props(p.find('w:pPr',N)));runs=[]
        for r in p.findall('.//w:r',N):
            text=''.join(t.text or '' for t in r.findall('w:t',N))
            if not text.strip():continue
            f={k:dict(v) for k,v in rp.items()};rs=r.find('w:rPr/w:rStyle',N)
            for s in chain(rs.get(W+'val') if rs is not None else None):merge(f,props(s.find('w:rPr',N)))
            merge(f,props(r.find('w:rPr',N)))
            fonts=f.get('rFonts',{})
            for attribute in ('ascii','hAnsi'):
                t=fonts.get(attribute+'Theme')
                if t:
                    resolved=theme_fonts.get('major' if t.startswith('major') else 'minor')
                    if resolved:fonts[attribute]=resolved
                    else:unresolved.append('Не удалось разрешить шрифт темы')
            runs.append({'text':text,'properties':f})
        result.append({'locator':f'p{i}','paragraph':pp,'runs':runs,'unresolved':list(set(unresolved))})
    return result

def document_layout(path):
    """Read section/page properties that do not belong to a paragraph block."""
    with ZipFile(path) as z:
        root=E.fromstring(z.read('word/document.xml'))
        relationships={}
        if 'word/_rels/document.xml.rels' in z.namelist():
            rels=E.fromstring(z.read('word/_rels/document.xml.rels'))
            relationships={x.get('Id'):x.get('Target','') for x in rels}
        sections=[]
        for number,section in enumerate(root.findall('.//w:sectPr',N),1):
            size=section.find('w:pgSz',N);margins=section.find('w:pgMar',N)
            refs={x.get(W+'type'):relationships.get(x.get(R+'id'),'') for x in section.findall('w:footerReference',N)}
            footer={}
            for kind,target in refs.items():
                name='word/'+target.lstrip('/')
                if name not in z.namelist():continue
                node=E.fromstring(z.read(name));paragraphs=node.findall('.//w:p',N)
                page=any((x.get(W+'instr','') if x.tag==W+'fldSimple' else x.text or '').strip().upper()=='PAGE' for x in node.iter())
                centered=any((p.find('w:pPr/w:jc',N) is not None and p.find('w:pPr/w:jc',N).get(W+'val')=='center') for p in paragraphs)
                sizes={int(x.get(W+'val')) for x in node.findall('.//w:rPr/w:sz',N) if (x.get(W+'val') or '').isdigit()}
                footer[kind]={'page_field':page,'centered':centered,'sizes':sorted(sizes)}
            sections.append({'section':number,'width':int(size.get(W+'w')) if size is not None and size.get(W+'w','').isdigit() else None,
                'height':int(size.get(W+'h')) if size is not None and size.get(W+'h','').isdigit() else None,
                'orientation':size.get(W+'orient','portrait') if size is not None else None,
                'margins':{k:int(margins.get(W+k)) for k in ('left','right','top','bottom') if margins is not None and margins.get(W+k,'').isdigit()},
                'different_first_page':section.find('w:titlePg',N) is not None,'footer':footer})
    return sections

def compact_results(findings,coverage):
    """One style defect should not produce hundreds of indistinguishable rows."""
    groups={};ordered=[]
    for item in findings:
        key=(item.get('requirement_id',''),item.get('issue',''),item.get('suggestion',''),item.get('kind',''))
        if key not in groups:groups[key]=dict(item);groups[key]['evidence']=[];groups[key]['_reasons']=[];ordered.append(groups[key])
        target=groups[key];target['evidence'].extend(e for e in item.get('evidence',[]) if e not in target['evidence'])
        if item.get('explanation') not in target['_reasons']:target['_reasons'].append(item.get('explanation',''))
    result=[]
    for item in ordered:
        total=len(item['evidence']);reasons=item.pop('_reasons')
        if total>1:item['explanation']='Выявлено в '+str(total)+' местах. '+' Варианты измерений: '+'; '.join(reasons[:4]);item['occurrence_count']=total
        if total>24:item['additional_occurrences']=total-24;item['evidence']=item['evidence'][:24]
        result.append(item)
    compact=[]
    for item in coverage:
        if item.get('state')=='measured' and item.get('locator'):
            match=next((x for x in compact if x.get('state')=='measured' and x.get('check')==item.get('check') and x.get('requirement_ids')==item.get('requirement_ids')),None)
            if match:
                match['occurrence_count']=match.get('occurrence_count',1)+1
                if len(match.setdefault('sample_locators',[]))<20:match['sample_locators'].append(item['locator'])
            else:
                value=dict(item);value['sample_locators']=[value.pop('locator')];compact.append(value)
            continue
        if item.get('state')!='unverified':compact.append(item);continue
        match=next((x for x in compact if x.get('state')=='unverified' and x.get('check')==item.get('check') and x.get('reason')==item.get('reason') and x.get('requirement_ids')==item.get('requirement_ids')),None)
        if match:
            match['occurrence_count']=match.get('occurrence_count',1)+1
            if item.get('locator') and len(match.setdefault('sample_locators',[]))<20:match['sample_locators'].append(item['locator'])
            if item.get('section') and len(match.setdefault('sample_sections',[]))<20:match['sample_sections'].append(item['section'])
        else:
            value=dict(item)
            if value.get('locator'):value['sample_locators']=[value['locator']]
            if value.get('section'):value['sample_sections']=[value['section']]
            compact.append(value)
    return result,compact

def check(doc,cat,mode):
    if mode=='off':return [],[{'state':'skipped_setting','check':'formatting'}]
    measurements=measure(doc['path']);by={b['locator']:b for b in doc['blocks']};findings=[];coverage=[]
    instruction_rules={c['clause']:c for c in cat['cards'] if c['document_name']=='Инструкция по делопроизводству'}
    corporate=instruction_rules.get('7.6')
    size_rule=next((c for c in cat['cards'] if '04.001.1' in c['document_name'] and c['clause']=='7.1.2'),None)
    rules={c['clause']:c for c in cat['cards'] if '04.001.1' in c['document_name'] and not c['appendix']}
    def finding(b,rule,title,reason,suggestion,kind='violation'):
        if not rule:return
        findings.append({'category':'оформление','severity':'minor','kind':kind,'issue':title,'explanation':reason,'suggestion':suggestion,'evidence':[{'document':doc['id'],'locator':b['locator'],'quote':b['text']}],'requirement_id':rule['requirement_id'],'search_query':''})
    for m in measurements:
        b=by.get(m['locator'])
        if not b or b.get('toc') or not b.get('heading_path'):continue
        if m['unresolved']:
            coverage.append({'locator':m['locator'],'state':'unverified','reason':'; '.join(m['unresolved'])});continue
        heading=b.get('is_heading');outline=b.get('format',{}).get('outline');level=int(outline)+1 if outline is not None and str(outline).isdigit() else None
        ordinary=not heading and not b.get('table_context') and not re.match(r'\s*(?:Примечани|УТВЕРЖД|СОГЛАСОВАН|Приложени)',b['text'],re.I)
        rule=rules.get('7.1.3' if level==1 else '7.1.4' if level==2 else '7.1.5' if level in (3,4) else '') if heading else corporate if ordinary and corporate else size_rule
        allowed=({28,32} if level in (1,2) else {28}) if heading else {20,24,28} if b.get('table_context') else {24} if re.match(r'Примечани',b['text'],re.I) else {28}
        if rule:
            for r in m['runs']:
                size=r['properties'].get('sz',{}).get('val')
                if size and int(size) not in allowed and r['text'].strip() in b['text']:
                    finding(b,rule,'Размер шрифта отличается от допустимого для этого элемента',f'По OOXML размер {int(size)/2:g} пт; допустимо {", ".join(str(x//2) for x in sorted(allowed))} пт. Проверено наследование стилей и прямых свойств.','Привести размер шрифта к применимому пункту СТО.');break
        spacing=m['paragraph'].get('spacing',{});line=spacing.get('line');line_rule=spacing.get('lineRule','auto')
        if line and rule:
            actual=int(line);valid=(line_rule=='auto' and abs(actual-264)<=1) if heading else (line_rule=='auto' and actual==240) if (b.get('table_context') or re.match(r'Примечани',b['text'],re.I)) else ((line_rule=='exact' and actual==360) or (line_rule=='auto' and abs(actual-264)<=1))
            if not valid:finding(b,rule,'Междустрочный интервал не соответствует виду абзаца',f'Эффективное свойство Word: line={actual}, lineRule={line_rule}. '+('Множитель задаётся в долях 1/240 строки.' if line_rule=='auto' else 'Значение задаётся в двадцатых долях пункта.'),'Применить интервал, установленный указанным пунктом СТО.')
        elif rule:coverage.append({'locator':m['locator'],'state':'unverified','check':'line_spacing','reason':'Эффективный интервал не задан явно; требуется измерение в Word'})
        if heading and rule and level in (1,2,3,4):
            before={600,480} if level==1 else {480,360} if level==2 else {360};after={360} if level==1 else {360,240} if level==2 else {240}
            for attr,values in (('before',before),('after',after)):
                if spacing.get(attr+'Autospacing') in ('1','true'):continue
                if spacing.get(attr) is not None and int(spacing[attr]) not in values:finding(b,rule,'Интервал '+('перед' if attr=='before' else 'после')+' заголовка отличается от требования',f'Измерено {int(spacing[attr])/20:g} пт; допустимо {", ".join(str(v//20) for v in sorted(values))} пт.','Установить допустимый интервал заголовка.')
            if any(r['properties'].get('b',{}).get('val','0') not in ('1','true','on') for r in m['runs']):finding(b,rule,'Заголовок набран без полужирного начертания','Не все содержательные фрагменты заголовка имеют эффективное полужирное начертание.','Применить полужирное начертание к тексту заголовка.')
        # Similar typefaces are expressly permitted by 7.1.1, so unknown fonts require a question.
        unusual=sorted({r['properties'].get('rFonts',{}).get('hAnsi',r['properties'].get('rFonts',{}).get('ascii','')) for r in m['runs']}-{'','Times New Roman','Arial','Courier New'})
        if unusual:finding(b,rules.get('7.1.1'),'Требуется оценить допустимость использованного шрифта','Измерены шрифты: '+', '.join(unusual)+'. СТО разрешает аналоги по начертанию; список сам по себе не доказывает нарушение.','Установить, является ли шрифт допустимым аналогом; при необходимости заменить.','question')
        # Instruction 7.6 applies to ordinary body text. Headings, tables,
        # notes and multi-line requisites have their own values and are excluded.
        if ordinary and corporate:
            for r in m['runs']:
                if not r['text'].strip():continue
                rp=r['properties'];fonts=rp.get('rFonts',{});font=fonts.get('hAnsi') or fonts.get('ascii')
                if font and font.casefold()!='times new roman':finding(b,corporate,'Шрифт основного текста не соответствует корпоративному стилю',f'Измеренный эффективный шрифт: {font}; пункт 7.6 Инструкции устанавливает Times New Roman.','Применить Times New Roman к основному тексту.');break
                italic=rp.get('i',{}).get('val','0')
                if italic not in ('0','false','off'):finding(b,corporate,'Основной текст набран курсивом',f'Измерено наклонное начертание; пункт 7.6 Инструкции устанавливает прямое начертание.','Применить прямое начертание, если специальное выделение не предусмотрено отдельным правилом.');break
                underline=rp.get('u',{}).get('val','0')
                if underline not in ('0','false','off','none'):finding(b,instruction_rules.get('27.1'),'В основном тексте использовано подчёркивание',f'Измерено подчёркивание {underline}; пункт 27.1 Инструкции требует печатать документы без подчёркиваний.','Убрать подчёркивание, если оно не относится к заполняемой форме.');break
            jc=m['paragraph'].get('jc',{}).get('val')
            if jc and jc not in ('both','distribute'):finding(b,corporate,'Основной текст выровнен не по обеим границам',f'Эффективное выравнивание Word: {jc}; пункт 7.6 Инструкции требует выравнивание по обеим границам.','Установить выравнивание основного текста по ширине.')
            ind=m['paragraph'].get('ind',{});first=ind.get('firstLine');hanging=ind.get('hanging')
            if hanging and int(hanging)!=0:finding(b,corporate,'Вместо абзацного отступа установлен выступ',f'Измеренный выступ: {int(hanging)/567:g} см; пункт 7.6 Инструкции устанавливает абзацный отступ 1,25 см.','Убрать выступ и установить отступ первой строки 1,25 см.')
            elif first and abs(int(first)-709)>12:finding(b,corporate,'Абзацный отступ отличается от 1,25 см',f'Измеренный отступ первой строки: {int(first)/567:g} см; пункт 7.6 Инструкции устанавливает 1,25 см.','Установить отступ первой строки 1,25 см.')
            before=int(spacing.get('before','0'));after=int(spacing.get('after','0'))
            if before or after:finding(b,corporate,'Между абзацами установлен дополнительный интервал',f'Эффективные интервалы: перед абзацем {before/20:g} пт, после {after/20:g} пт; пункт 7.6 Инструкции требует отсутствие таких отступов.','Установить интервалы перед и после обычного абзаца 0 пт.')
        coverage.append({'locator':m['locator'],'state':'measured','check':'font size, theme, spacing, heading emphasis and paragraph properties'})
    if mode=='render':
        target=DATA/'renders'/(doc['sha256']+'.pdf')
        try:
            if not target.exists():subprocess.run(['powershell','-NoProfile','-File',str(Path(__file__).with_name('word_export.ps1')),'-Source',doc['path'],'-Destination',str(target),'-Format','pdf'],capture_output=True,check=True,timeout=180)
            coverage.append({'state':'rendered','artifact':str(target),'reason':'PDF сформирован; обнаружение наложений, обрезки и визуальная сверка требуют анализа отрисованных страниц'})
        except Exception as e:coverage.append({'state':'unverified','check':'render','reason':type(e).__name__,'remedy':'Проверить доступность Microsoft Word и повторить экспорт'})
    anchor=next((b for b in doc['blocks'] if not b.get('toc') and b['text'].strip()),None)
    def document_finding(rule,title,reason,suggestion,kind='violation'):
        if anchor:finding(anchor,rule,title,reason,suggestion,kind)
    for section in document_layout(doc['path']):
        page_rule=instruction_rules.get('27.4');margin_rule=instruction_rules.get('27.6')
        if page_rule and section['width'] and section['height'] and not (abs(section['width']-11906)<=60 and abs(section['height']-16838)<=60):
            document_finding(page_rule,'Формат страницы отличается от А4',f'Раздел {section["section"]}: размер страницы {section["width"]/567:.1f} × {section["height"]/567:.1f} см; пункт 27.4 Инструкции устанавливает А4.','Установить формат страницы А4 (210 × 297 мм).')
        margins=section['margins']
        limits={'left':(1417,None,'левое не менее 25 мм'),'right':(850,1134,'правое 15–20 мм'),'top':(1134,None,'верхнее не менее 20 мм'),'bottom':(1417,None,'нижнее не менее 25 мм')}
        bad=[]
        for key,(minimum,maximum,label) in limits.items():
            value=margins.get(key)
            if value is not None and (value<minimum or maximum is not None and value>maximum):bad.append(f'{label}, измерено {value/56.7:.1f} мм')
        if margin_rule and bad:document_finding(margin_rule,'Поля страницы не соответствуют инструкции',f'Раздел {section["section"]}: '+'; '.join(bad)+'.','Установить поля согласно пункту 27.6 Инструкции.')
        page_number_rule=instruction_rules.get('27.8');default_footer=section['footer'].get('default',{})
        if page_number_rule and default_footer.get('page_field') and (not default_footer.get('centered') or default_footer.get('sizes') and 28 not in default_footer['sizes']):
            document_finding(page_number_rule,'Оформление номера страницы требует исправления',f'Раздел {section["section"]}: поле PAGE найдено, но не подтверждены одновременно центральное выравнивание и размер 14 пт.','Разместить номер страницы по центру и установить размер 14 пт.')
        elif page_number_rule and not default_footer.get('page_field'):
            coverage.append({'state':'unverified','check':'page_numbering','section':section['section'],'requirement_ids':[page_number_rule['requirement_id']],'reason':'Поле автоматического номера PAGE в нижнем колонтитуле не найдено. Номер может быть задан иным способом; требуется проверка отрисованных страниц.'})
        coverage.append({'state':'measured','check':'instruction_page_layout','section':section['section'],'requirement_ids':[x['requirement_id'] for x in (page_rule,margin_rule,page_number_rule) if x]})
    indent=rules.get('7.1.7');instruction=corporate
    if indent and instruction and '1,25 мм' in indent['source_quote'] and '1,25 см' in instruction['source_quote']:
        coverage.append({'state':'source_conflict','check':'first_line_indent','requirement_ids':[indent['requirement_id'],instruction['requirement_id']],'reason':'В СТО указано 1,25 мм, в инструкции — 1,25 см. СТО ссылается на корпоративные параметры. Автоматическое исправление при расхождении единиц запрещено; требуется разрешение конфликта источников.'})
    measured_clauses={'7.6','7.21','27.1','27.4','27.6','27.8','27.11'}
    coverage.append({'state':'unverified','check':'instruction_requirements','measured_clauses':sorted(measured_clauses),'unverified_clauses':sorted(set(instruction_rules)-measured_clauses),
        'reason':'Автоматически измерены параметры Word и страницы. Требования к полномочиям, регистрации, бланкам, подписям, печатям и фактическому расположению реквизитов зависят от вида управленческого документа или отрисованной страницы и требуют отдельного подтверждения.'})
    coverage.append({'state':'unverified','check':'visual_layout','reason':'Наложения, обрезка, читаемость рисунков и фактические страницы требуют анализа рендера; измерение OOXML не подтверждает эти свойства.'})
    return compact_results(findings,coverage)
