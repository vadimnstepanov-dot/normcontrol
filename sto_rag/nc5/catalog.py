"""Source-linked rule compiler. Automatic extraction never masquerades as expert validation."""
import re
import shutil
from collections import defaultdict
from pathlib import Path
from .common import ROOT, DATA, digest, read, write

OBLIGATION=re.compile(r'долж[еннаобыы]+|необходимо|следует|указыва[ею]т|привод[яи]т|содерж[аи]т|включа[ею]т|предусматрив|описыва[ею]т|запрещ|допуска|устанавлива|оформля|требован|использ|выполня|составля|излага|обознача|нумеру|размеща|печата|располага|набира|отступ|шрифт|интервал',re.I)
TEMPLATE_DIRECTION=re.compile(r'^(?:[^:\n]{1,180}:\s*)?(?:указыва[ею]т|привод[ия]т|описыва[ею]т|формулиру[ею]т|перечисл[яе]ют)(?:ся)?\b|^(?:В данном|В настоящем|В этом|В)\s+(?:разделе|пункте|подразделе)\s+(?:указыва|привод|описыва|формулиру)',re.I)
TYPES={'Техническое задание':'ТЗ','Частное техническое задание':'ЧТЗ','Описание информационной технологии':'ОИТ',
 'Проектное решение':'ПР','Проект технических решений подключения':'ПТРП','Описание комплекса программ':'ОКП',
 'Программа и методика испытаний':'ПМИ','Протокол испытаний':'ПРОТОКОЛ','Отчет об опытной эксплуатации':'ОТЧЁТ ОЭ'}
INCLUDED_KINDS=('обязательное требование','условное требование','рекомендация','смешанный нормативный фрагмент')

def kind(text, example=False):
    if example or re.match(r'^Пример\b',text,re.I):return 'пример'
    if re.search(r'рекоменду|целесообраз',text,re.I):
        mandatory=re.search(r'долж[а-яё]+|необходимо|не допускается|запрещ[а-яё]+|следует',text,re.I) or any(TEMPLATE_DIRECTION.search(line) for line in text.splitlines())
        return 'смешанный нормативный фрагмент' if mandatory else 'рекомендация'
    if OBLIGATION.search(text) or any(TEMPLATE_DIRECTION.search(line) for line in text.splitlines()):
        return 'условное требование' if re.search(r'\bесли\b|в случае|при наличии|при условии|по согласован',text,re.I) else 'обязательное требование'
    if re.match(r'^Примечани',text,re.I):return 'пояснение'
    return 'справочная информация'

def compile_catalog():
    manifest=read(ROOT/'sto_rag/data/manifest.json'); sources=[];cards=[];ledger=[];profiles={};blocks_by_source={}
    for source in manifest['sources']:
        path=ROOT/source['file'];sha=digest(path.read_bytes())
        if sha!=source['sha256']:raise ValueError('Изменился источник: '+source['file']+'; сначала обновите извлечение')
        extracted=read(ROOT/'sto_rag/data/extracted'/Path(source['file']).with_suffix('.json'))
        sid=sha[:16];source={**source,'source_id':sid};sources.append(source);bs=extracted['blocks'];blocks_by_source[sid]=bs
        snapshot=DATA/'sources'/sha/path.name;snapshot.parent.mkdir(parents=True,exist_ok=True)
        if not snapshot.exists():shutil.copyfile(path,snapshot)
        source['snapshot']=str(snapshot.relative_to(DATA));write(snapshot.parent/'extracted.json',extracted)
        context_clauses={x.get('clause','') for x in bs if not x.get('appendix') and re.fullmatch(r'(?:\d+\s+)?(?:Область применения|Нормативные ссылки|Термины и определения|Обозначения и сокращения|Термины, определения и сокращения)',x.get('raw_text',x['text']).strip(),re.I)}
        # Discover every declared template, not only a hard-coded list of document types.
        templates={b.get('appendix'):b.get('appendix_title','') for b in bs if 'Шаблон' in b.get('appendix_title','')}
        template_profiles={}
        for appendix,title in templates.items():
            m=re.search('«([^»]+)»',title);name=m[1] if m else title
            name=name[:1].upper()+name[1:];code=TYPES.get(name,name)
            pid=digest([source['standard'],appendix,name])[:12];template_profiles[appendix]=pid
            profiles[pid]={'id':pid,'name':name,'code':code,'template':title,'source_id':sid,'appendix':appendix,'requirements':[], 'general':[], 'validation_status':'automatically_extracted'}
        groups=[];current=[];prev=None
        for i,b in enumerate(bs):
            key=(b.get('appendix',''),b.get('clause',''),b.get('heading',''))
            if current and (key!=prev or sum(len(x[1]['raw_text']) for x in current)>5000):groups.append(current);current=[]
            current.append((i,b));prev=key
        if current:groups.append(current)
        example=False;lastkey=None
        for group in groups:
            i,b=group[0];key=(b.get('appendix',''),b.get('clause',''),b.get('heading',''))
            if key!=lastkey:example=False
            lastkey=key
            segments=[];segment=[]
            for idx,block in group:
                text=block.get('raw_text',block['text'])
                starts_example=bool(re.match(r'^Пример\b',text,re.I))
                # Explicit instructions to the template author close an illustrative example.
                # A technical 'must' inside an example does not close it.
                if example and not starts_example and TEMPLATE_DIRECTION.search(text):
                    if segment:segments.append((True,segment));segment=[]
                    example=False
                if starts_example and segment:segments.append((example,segment));segment=[]
                if starts_example:example=True
                # Never infer that an example has ended without a new structural boundary.
                segment.append((idx,block))
                if starts_example and re.fullmatch(r'Пример\s*[-–—]\s*[А-ЯA-Z0-9.\-]+[.;]?',text):
                    segments.append((True,segment));segment=[];example=False
            if segment:segments.append((example,segment))
            for is_example,segment in segments:
                text='\n'.join(x.get('raw_text',x['text']) for _,x in segment);first=segment[0][1]
                k=kind(text,is_example);meta=not first.get('clause') and not first.get('appendix')
                if source.get('standard','').startswith('СТО') is False:meta=True
                if re.search(r'Библиография|Ключевые слова|ОКС\s*\d',first.get('heading','')+' '+text[:80],re.I):meta=True
                if not first.get('appendix') and any(first.get('clause','')==cl or first.get('clause','').startswith(cl+'.') for cl in context_clauses if cl):meta=True
                if meta:k='метаданные'
                rid=digest([sid,[n for n,_ in segment],text])[:20]
                applicable=template_profiles.get(first.get('appendix'))
                # Resolve the actual clause ancestry, then add adjacent introductory conditions.
                ancestors=[];clause=first.get('clause','')
                while '.' in clause:
                    clause=clause.rsplit('.',1)[0];ancestors.append(clause)
                context=[x for x in bs[:segment[0][0]] if x.get('appendix','')==first.get('appendix','') and x.get('clause','') in ancestors and (x.get('kind')!='table_row')]
                context+=bs[max(0,segment[0][0]-3):segment[0][0]]
                parents=list({x['locator']:{'locator':x['locator'],'quote':x.get('raw_text',x['text'])} for x in context}.values())
                card=dict(requirement_id=rid,version=1,source_id=sid,source_sha256=sha,document_name=source.get('standard') or source['file'],edition='2021' if source.get('standard') else '',
                    clause=first.get('clause',''),appendix=first.get('appendix',''),source_locator=first['locator'],source_quote=text,parent_context_refs=parents,
                    normative_kind=k,obligation=text,applicability={'profile':applicable or 'general','conditions':'Определить по исходной цитате и родительскому контексту'},exceptions='Не исключать условия и примеры из родительского контекста',
                    document_types=[applicable] if applicable else ['general'],lifecycle_stage='из источника',document_scope='template' if applicable else 'general',
                    check_stage='sto',check_method='semantic',expected_evidence=first.get('heading',''),related_requirements=[],conflict_group='',compact_text=text,
                    extraction_confidence='automatic',validation_status='source_exact_semantics_pending')
                if applicable and len(segment)==1 and re.fullmatch(r'\d+(?:\.\d+)*\s+.+',text) and text==first.get('heading'):
                    card['check_stage']='structure';card['check_method']='complete_local_outline';card['normative_kind']='структурное требование'
                if source.get('standard')=='СТО РЖД 04.001.1–2021' and not first.get('appendix'):
                    if first.get('clause','').startswith('7.') or first.get('clause')=='7':card.update(check_stage='formatting',check_method='properties_or_render',document_scope='formatting')
                    if first.get('clause') in ('7.1.9','7.1.10','7.1.11'):card.update(check_stage='sto',check_method='structure_and_semantics',document_scope='general')
                    if first.get('clause')=='6':card.update(check_stage='inter',document_scope='delivery_set',check_method='document_registry')
                include=k in INCLUDED_KINDS
                if include:
                    cards.append(card)
                    if applicable:profiles[applicable]['requirements'].append(rid)
                for idx,fragment in segment:
                    ledger.append({'source_id':sid,'index':idx,'locator':fragment['locator'],'requirement_ids':[rid] if include else [],'kind':k,'reason':'Карточка с исходным контекстом' if include else 'Контекст; не самостоятельное предписание'})
    # Legacy working copy: provenance is explicit, order confirmation is separate from revision identity.
    instruction=ROOT/'ИНСТРУКЦИЯ (для работы).doc';txt=ROOT/'deliverables/redesign-prompt-20260917/instruction-extracted.txt'
    if instruction.exists() and txt.exists():
        sha=digest(instruction.read_bytes());sid=sha[:16]
        snap=DATA/'sources'/sha/instruction.name;snap.parent.mkdir(parents=True,exist_ok=True)
        if not snap.exists():shutil.copyfile(instruction,snap)
        shutil.copyfile(txt,snap.parent/'extracted.txt')
        order=ROOT/'deliverables/redesign-prompt-20260917/sources/Приказ-РЖД-99-2024-12-10-фрагмент.png'
        order_meta={}
        if order.exists():
            shutil.copyfile(order,snap.parent/'approval.png');order_meta={'sha256':digest(order.read_bytes()),'snapshot':str((snap.parent/'approval.png').relative_to(DATA)),'confirmed':'Утверждение приказом №99 и введение с 01.01.2025; фрагмент пункта 1'}
        sources.append({'source_id':sid,'file':instruction.name,'sha256':sha,'snapshot':str(snap.relative_to(DATA)),'document':'Инструкция по делопроизводству','approval':'Приказ от 10.12.2024 №99; действует с 01.01.2025 по предоставленному фрагменту','approval_evidence':order_meta,'revision_identity':'Рабочий DOC: тождество полному утверждённому тексту не установлено'})
        paragraphs=[s.strip() for s in txt.read_text(encoding='utf-8-sig').splitlines() if s.strip()];clause='';groups=[];group=[]
        for i,text in enumerate(paragraphs):
            m=re.match(r'^(\d+\.\d+(?:\.\d+)*)(?:\.)?\s',text)
            if m:
                if group:groups.append((clause,group))
                clause=m[1];group=[]
            group.append((i,text))
        if group:groups.append((clause,group))
        for clause,group in groups:
            i=group[0][0];text='\n'.join(t for _,t in group);rid=digest([sid,i,text])[:20];k=kind(text);include=clause.startswith(('7.','27.')) and k in INCLUDED_KINDS
            for j,t in group:ledger.append({'source_id':sid,'index':j,'locator':f'текстовый абзац {j+1}','requirement_ids':[rid] if include else [],'kind':k,'reason':'Общие правила оформления, применимость условная' if include else 'Делопроизводство/контекст, не универсальное требование технического документа'})
            if include:cards.append(dict(requirement_id=rid,version=1,source_id=sid,source_sha256=sha,document_name='Инструкция по делопроизводству',edition='рабочая копия',clause=clause,appendix='',source_locator=f'текстовый абзац {i+1}',source_quote=text,parent_context_refs=[{'locator':f'текстовый абзац {j+1}','quote':paragraphs[j]} for j in range(max(0,i-2),i)],normative_kind=k,obligation=text,applicability={'profile':'instruction','conditions':'СТО 04.001.1–2021 п.7.1.9: только вопросы, не урегулированные специальными СТО'},exceptions='Конфликт с СТО требует разрешения',document_types=['instruction'],lifecycle_stage='любой',document_scope='formatting',check_stage='formatting',check_method='properties_or_render',expected_evidence='измеренные свойства Word',related_requirements=[],conflict_group='sto_instruction',compact_text=text,extraction_confidence='automatic',validation_status='working_copy_applicability_pending'))
    # The document registry includes types without a standalone appendix template.
    registry=[]
    for source in sources:
        sid=source['source_id']
        for b in blocks_by_source.get(sid,[]):
            if not source.get('standard','').startswith('СТО') or b.get('kind')!='table_row' or b.get('appendix'):continue
            parts=[x.strip() for x in b['raw_text'].split('|')]
            if b['locator'].startswith('Таблица 9.1') and len(parts)==2 and re.fullmatch(r'[А-ЯA-Z0-9]{2,3}',parts[1]):
                for name in parts[0].split(', '):registry.append({'name':name,'code':parts[1],'source_id':sid,'locator':b['locator'],'quote':b['raw_text']})
            if b['locator'].startswith('Таблица 6.1') and len(parts)>=3 and parts[2] and not parts[2].isdigit():
                name=re.sub(r'\d+\)$','',parts[2]).strip()
                names=['Техническое задание','Частное техническое задание'] if name=='Техническое задание (Частное техническое задание)' else [name]
                for name in names:
                    if re.match(r'^(Техническое задание|Частное техническое задание|Проектное решение|Проект технических|Описание |Руководство |Ведомость |Программа и методика|Акт |Протокол |Отч[её]т )',name):registry.append({'name':name,'code':'','source_id':sid,'locator':b['locator'],'quote':b['raw_text'],'lifecycle_stage':parts[0]+' / '+parts[1]})
    def canonical(s):return re.sub(r'\s+',' ',s.casefold().replace('ё','е').replace('базы данных','баз данных').replace('ведомость рд','ведомость рабочей документации')).strip()
    by_name={canonical(p['name']):p for p in profiles.values()}
    for r in registry:
        key=canonical(r['name'])
        if key not in by_name:
            pid=digest(['registry',key])[:12];p={'id':pid,'name':r['name'],'code':r['code'] or r['name'],'template':'Отдельный шаблон не обнаружен','source_id':r['source_id'],'appendix':'','requirements':[],'general':[],'validation_status':'registry_only','limitations':['Доступны общие положения и упоминания состава; отдельный шаблон отсутствует в обнаруженных источниках']}
            profiles[pid]=p;by_name[key]=p
        p=by_name[key];p.setdefault('registry_refs',[]).append(r)
        if r['code']:p['document_code']=r['code']
    general=[c['requirement_id'] for c in cards if c['document_types']==['general']]
    for p in profiles.values():
        p['general']=general;p['expected_sections']=[]
        if not p['appendix']:continue
        for b in blocks_by_source[p['source_id']]:
            if b.get('appendix')!=p['appendix']:continue
            raw=b.get('raw_text',b['text']);match=re.fullmatch(r'(\d+(?:\.\d+)*)\s+(.+)',raw)
            if match and raw==b.get('heading'):
                p['expected_sections'].append({'number':match[1],'title':match[2],'source_locator':b['locator'],'source_quote':raw,'depth':match[1].count('.')+1})
    inter=[c['requirement_id'] for c in cards if c['check_stage']=='inter' or re.search(r'согласован|соответств.*(?:техническ|частн).*задан|на основании|прослежив|преемствен',c['source_quote'],re.I)]
    cat={'schema':1,'sources':sources,'profiles':list(profiles.values()),'document_registry':registry,'cards':cards,'ledger':ledger,'interdocument':inter,'coverage':{'fragments':len(ledger),'mapped':len(ledger),'lost':0,'semantic_review_pending':len(cards)},'limitations':['Карточки извлечены автоматически и точно привязаны к источнику. Семантическое выделение обязанностей, границы примеров и применимость требуют валидации перед объявлением полной нормативной полноты.']}
    from .planning import atomize_catalog
    cat=atomize_catalog(cat)
    cat['version']=digest(cat)[:20];target=DATA/'catalogs'/cat['version']/'catalog.json';write(target,cat)
    old=read(DATA/'catalog-current.json') if (DATA/'catalog-current.json').exists() else None
    write(target.parent/'diff.json',{'previous':old,'current':cat['version'],'source_hashes':{s['source_id']:s['sha256'] for s in sources}})
    write(DATA/'catalog-current.json',{'version':cat['version']});return cat

def load_catalog(version=None):
    version=version or read(DATA/'catalog-current.json')['version'];return read(DATA/'catalogs'/version/'catalog.json')
