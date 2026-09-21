"""Independent checks on the *reason for a finding*, beyond quote existence."""
import difflib
import re
import sys
from pathlib import Path
from functools import lru_cache
from decimal import Decimal,InvalidOperation

@lru_cache(maxsize=1)
def morph():
    try:
        try:import pymorphy3
        except ImportError:
            sys.path.insert(0,str(Path(__file__).with_name('vendor')))
            import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except ImportError:return None

def upper_bound(e,documents):
    quote=e['quote'];marker=r'максимальн|не более|верхн\w*\s+(?:предел|границ)'
    units=r'секунд[а-я]*|сек\.?|час[а-я]*|минут[а-я]*|мин\.?'
    values=re.findall(r'(?<![\d.])(\d+(?:[,.]\d+)?)\s*('+units+r')',quote,re.I)
    if len(values)==1 and re.search(marker,quote,re.I):return 'duration'
    if not documents or not re.fullmatch(r'\s*\d+(?:[,.]\d+)?\s*',quote):return None
    doc=next((d for d in documents if d['id']==e.get('document')),None)
    block=next((b for b in doc['blocks'] if b['locator']==e.get('locator')),None) if doc else None
    table=(block or {}).get('table_context',{})
    if not table or not re.search(r'значени|предел|величин',table.get('column_name',''),re.I):return None
    row=[b for b in doc['blocks'] if b.get('table_context',{}).get('table')==table['table'] and b.get('table_context',{}).get('row')==table['row']]
    conditions=' '.join(b['text'] for b in row if re.search(r'услови|ограничени',b['table_context'].get('column_name',''),re.I))
    unit=' '.join(b['text'] for b in row if re.search(r'единиц',b['table_context'].get('column_name',''),re.I)).strip()
    if re.search(marker,conditions,re.I) and re.fullmatch(units,unit,re.I):return 'duration'
    return None


def assess_rejection(f,reason):
    source=f.get('source',{}).get('source_quote','')
    template=f.get('template_source',{})
    if template.get('title') and re.search(r'не допускается[^.]*переименов',source,re.I) and re.search(r'допустим[а-яё]* вариант|точное совпадение[^.]*не[^.]*обязательн|смысл[^.]*сохран|не противоречит смыслу',reason,re.I):
        return 'question','Модель отклонила переименование по смысловому сходству, хотя цитируемая норма прямо запрещает переименовывать пункты шаблона. Для отклонения нужно доказать допустимое исключение или неприменимость шаблона.'
    return None,None

def assess(f,documents=None):
    text=f.get('issue','')+' '+f.get('explanation','');quotes=' '.join(e['quote'] for e in f['evidence']);suggestion=f.get('suggestion','')
    reason=f.get('explanation','')
    if not f.get('requirement_id') and re.search(r'управлен|падеж|предлог',text,re.I) and re.search(r'\b(?:вход[а-яё]*|включ[а-яё]*|попад[а-яё]*|впис[а-яё]*)\b',quotes,re.I):
        analyzer=morph()
        if analyzer:
            for match in re.finditer(r'\bв\s+([а-яё]+)\b',quotes,re.I):
                forms=analyzer.parse(match[1])
                if any(p.is_known and p.tag.case=='accs' for p in forms):
                    return 'question','При глаголах вхождения или включения предлог «в» допускает винительный падеж. Замена на предложный падеж не является автоматическим исправлением; нужен разбор всей конструкции и значения глагола.'
    if re.search(r'статус\s+[«"\']?question|(?:вероятн[а-яё]*|возможн[а-яё]*)\s+опечатк|требование уточнения правомерно|(?:вероятн[а-яё]*)\s+ошибк',reason,re.I):
        return 'question','Объяснение устанавливает гипотезу или необходимость уточнения, а не доказанное нарушение. Такой вывод нельзя публиковать как подтверждённый.'
    if re.search(r'^(?:отсутствие нарушени|нарушени[ея] не выявлен|ссылка .{0,100}корректна|соответствие подтверждено)',f.get('issue',''),re.I):
        return 'rejected','Сообщение о корректности не является замечанием к документу.'
    by={(d['id'],b['locator']):b for d in (documents or []) for b in d['blocks']}
    evidence_blocks=[by.get((e.get('document'),e.get('locator')),{}) for e in f['evidence']]
    if not f.get('requirement_id') and re.search(r'инициатор',text,re.I) and re.search(r'источник|получател',text,re.I) and re.search(r'несовместим|несогласован|противореч|некоррект|не может',text,re.I):
        labels={b.get('table_context',{}).get('column_name','').casefold() for b in evidence_blocks}
        if labels and any('инициатор' in label for label in labels):
            return 'question','Источник данных, получатель и инициатор обмена являются независимыми ролями. Совпадение или различие участников в этих колонках не доказывает противоречия; необходим контракт или описание направления и механизма обмена.'
    if not f.get('requirement_id') and re.search(r'пересечени|наложени|совпадени',text,re.I) and re.search(r'срок|этап|период',text,re.I):
        from datetime import datetime
        periods=[]
        for e in f['evidence']:
            dates=re.findall(r'\b\d{2}\.\d{2}\.\d{4}\b',e['quote'])
            if len(dates)==2:
                try:periods.append(tuple(datetime.strptime(x,'%d.%m.%Y') for x in dates))
                except ValueError:pass
        if len(periods)==2 and max(p[0] for p in periods)==min(p[1] for p in periods):
            return 'question','У этапов совпадает только граничная дата. Без времени суток или требования непересечения это не доказывает конфликт графика; нужно уточнение порядка перехода между этапами.'
    if not f.get('requirement_id') and re.search(r'пробел|слитн|раздельн',text,re.I):
        for e,b in zip(f['evidence'],evidence_blocks):
            label=b.get('table_context',{}).get('column_name','')
            token=e['quote'].strip()
            if re.search(r'реквизит|атрибут|идентификатор|имя\s+поля',label,re.I) and re.fullmatch(r'[\w.]+',token) and re.search(r'[а-яёa-z][А-ЯЁA-Z]',token):
                return 'question','В колонке задано техническое имя реквизита или атрибута. Слитная запись и регистр могут быть частью идентификатора; добавление пробелов требует определения из схемы или контракта.'
    # Check explicit grammatical claims against morphology, not the model's confidence.
    if re.search(r'падеж|подлежащ|существительн|глагол',reason,re.I):
        cases={'именительн':'nomn','родительн':'gent','дательн':'datv','винительн':'accs','творительн':'ablt','предложн':'loct'}
        for match in re.finditer(r'предлог\s+[«"]([^»"]+)[»"]\s+требует\s+([а-яё]+)\s+падеж',reason,re.I):
            expected=next((v for k,v in cases.items() if match[2].lower().startswith(k)),None)
            allowed={'в':{'accs','loct'},'к':{'datv'},'без':{'gent'},'для':{'gent'}}.get(match[1].lower())
            if expected and allowed and expected not in allowed:return 'question','Указанное в объяснении управление предлога неверно. Наличие дефекта следует перепроверить отдельно от ошибочно названного падежа.'
        m=morph()
        if m:
            for match in re.finditer(r'(подлежащ(?:ее|им|его)|существительн(?:ое|ым)|глагол(?:ом)?)\s+[^«".\n]{0,65}[«"]([а-яё]+)[»"]',reason,re.I):
                parses=[p for p in m.parse(match[2]) if p.is_known]
                if not parses:continue
                tag=match[1].lower()
                possible=any(p.tag.case=='nomn' for p in parses) if tag.startswith('подлежащ') else any(p.tag.POS in ('VERB','INFN') for p in parses) if tag.startswith('глагол') else any(p.tag.POS in ('NOUN','NPRO') for p in parses)
                if not possible:return 'question','Морфологический разбор не подтверждает названную в объяснении часть речи или форму подлежащего. Нужен корректный синтаксический разбор перед подтверждением исправления.'
    if re.search(r'арифмет|сумм|цен[ауы]',text,re.I) and re.search(r'превышает цену|между ценой.*суммой|сумма без НДС',text,re.I):
        rows={(e.get('document'),b.get('table_context',{}).get('table'),b.get('table_context',{}).get('row')) for e,b in zip(f['evidence'],evidence_blocks)}
        if len(rows)==1:
            did,table,row=next(iter(rows));values={};groups=set();money_units=set()
            for (docid,_),b in by.items():
                tc=b.get('table_context',{})
                if docid!=did or table is None or (tc.get('table'),tc.get('row'))!=(table,row):continue
                labels=tc.get('column_name','').split(' / ');label=labels[-1].lower()
                role='quantity' if re.search(r'количеств',label) else 'price' if re.search(r'цена с НДС',label,re.I) else 'net' if re.search(r'сумма без НДС',label,re.I) else 'tax' if re.search(r'^сумма НДС',label,re.I) else None
                if role:
                    groups.add(' / '.join(labels[:-1]))
                    if role!='quantity':
                        currency='rub' if re.search(r'руб|₽',label) else 'eur' if re.search(r'евро|eur|€',label) else 'usd' if re.search(r'долл|usd|\$',label) else 'unspecified'
                        scale='million' if re.search(r'\bмлн',label) else 'thousand' if re.search(r'\bтыс',label) else 'one'
                        money_units.add((currency,scale))
                    try:values.setdefault(role,[]).append(Decimal(b['text'].replace(' ','').replace('\xa0','').replace(',','.')))
                    except InvalidOperation:pass
            if len(groups)==1 and len(money_units)==1 and all(len(values.get(k,[]))==1 for k in ('quantity','price','net','tax')):
                q,p,n,t=(values[k][0] for k in ('quantity','price','net','tax'))
                if q>1 and abs(q*p-n-t)<=Decimal('.01'):
                    return 'rejected',f'Сравнены цена единицы и сумма строки. Исходная строка согласована: {q} × {p} = {n} + {t}. Превышение суммы над ценой единицы при количестве больше одного не является ошибкой.'
    source=f.get('source',{}).get('source_quote','') if f.get('source') else ''
    if not source and re.search(r'СТО\s+РЖД|ГОСТ\s*\d',text,re.I):
        return 'question','Обоснование ссылается на стандарт, которого нет среди проверенных источников замечания. Номер нормы и её применимость нельзя подтверждать по памяти модели; локальный дефект нужно обосновать отдельно.'
    if source and re.search(r'место|сроки|перечень организаций',text,re.I) and re.search(r'(?:место|сроки)[^.]{0,160}должны быть отражены в Программе и методике испытаний',source,re.I):
        profiles={d.get('profile',{}).get('type') for d in (documents or []) if any(e.get('document')==d['id'] for e in f['evidence'])}
        if profiles and None not in profiles and 'ПМИ' not in profiles:
            return 'question','Цитируемая норма относит место, сроки и состав участников испытаний к ПМИ. Их обязательное включение в проверяемый документ этой цитатой не доказано; нужна проверка адресата требования.'
    if source and re.search(r'Могут быть указаны[^.]*показател',source,re.I) and re.search(r'показател|критери',text,re.I) and re.search(r'долж|обязательн|противореч',text,re.I) and not re.search(r'оптимизац',quotes,re.I):
        return 'question','Источник допускает указание показателей, а критерий оптимальности обязателен при постановке цели оптимизации. Универсальная обязанность приводить показатели из этой нормы не следует; требуется уточнить основание замечания.'
    code_context=any(re.search(r'пол[ея]|атрибут|параметр|ключ',b.get('table_context',{}).get('column_name',''),re.I) for b in evidence_blocks)
    if not f.get('requirement_id') and re.search(r'опечатк|орфограф|написани|английск.*слов',text,re.I) and (code_context or re.search(r'API|идентификатор|им[яени]+\s+(?:пол[ея]|атрибут)',text,re.I)):
        fields=[e['quote'].strip() for e in f['evidence']]
        if fields and all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',q) for q in fields):
            return 'question','Имена программных полей могут отличаться от словарного английского написания. В доказательствах нет контракта API или определения правильного идентификатора; языковое переименование поля не подтверждено.'
    # Different operational scopes are not evidence of a numerical contradiction.
    if not f.get('requirement_id') and len(evidence_blocks)>=2 and re.search(r'несовпад|противореч|несогласован|несоответств',text,re.I) and re.search(r'периодичност|частот|срок|времен|количеств|объем|объём',text,re.I):
        contours=[]
        for b in evidence_blocks:
            context=' '.join(b.get('heading_path',[]))+' '+b.get('table_context',{}).get('title','')
            labels=set(re.findall(r'\b(тестов|продуктивн|промышленн)[а-я]*',context,re.I))
            contours.append(labels)
        if all(len(c)==1 for c in contours) and len(set(next(iter(c)).lower() for c in contours))>1:
            return 'question','Цитаты относятся к разным контурам. Разница параметров сама по себе допустима; подтверждение требует отдельного требования об их равенстве.'
    lexical_claim=re.search(r'неологизм|слово[^.]{0,60}не существует|устаревш[а-я]*\s+(?:термин|сокращени)|не имеет\s+(?:морфологического|лексического)\s+обоснования|не является\s+(?:нормативн|допустим)[а-яё]*\s+(?:слов|форм)',text,re.I)
    lexical_claim=lexical_claim or re.search(r'не является\s+(?:нормативн|допустим)[а-яё]*\s+термин|несуществующ[а-яё]*\s+(?:слов|термин)',text,re.I)
    if lexical_claim and not f.get('requirement_id'):
        return 'question','Вывод основан на недопустимости слова или термина, но проверенный словарный или отраслевой источник не приведён. Незнакомое модели слово само по себе не является ошибкой; нужна проверка термина в его предметной области.'
    if not f.get('requirement_id') and re.search(r'(?:неверн|ошибочн|некорректн)[а-я]*\s+(?:аббревиатур|сокращени)|опечатк[а-я]*\s+в\s+(?:аббревиатур|сокращени)|(?:аббревиатур|сокращени).{0,45}(?:ошиб|расшифров)',text,re.I) and len(f['evidence'])==1:
        return 'question','Ошибка в сокращении требует определения термина или сопоставления с правильным обозначением в документе. Расшифровку нельзя придумывать по памяти модели.'
    if re.search(r'(?:неправильн|неверн|ошибочн)[а-яё]*\s+род\s+существительного',text,re.I):
        m=morph()
        for word in re.findall(r'[«"„“]([а-яё]+)[»"“]',text,re.I):
            if m and m.word_is_known(word):
                genders={p.tag.gender for p in m.parse(word) if p.is_known and p.tag.POS=='NOUN'}
                if len(genders)>1:
                    return 'question','Словарь допускает несколько разборов исходного существительного с разным родом. Без разбора синтаксиса и предметного значения нельзя объявлять выбранную форму ошибкой.'
    if re.search(r'противореч|несогласован|несоответств',text,re.I) and re.search(r'времен|секунд|час|предел|производительност',text,re.I):
        bounds=[upper_bound(e,documents) for e in f['evidence']]
        if len(bounds)>=2 and all(bounds) and len(set(bounds))==1:
            return 'question','Все приведённые значения — верхние ограничения одной размерности, с учётом колонок условия и единицы измерения. Они математически совместимы; разница чисел не доказывает противоречия. Для обязательного равенства или фактического превышения нужны дополнительные доказательства.'
    if not f.get('requirement_id') and (f.get('category','').casefold() in ('style','стилистика') or re.search(r'^стилистическ|стилистическая неточность|единообрази[ея]\s+(?:оформлен|формат)|несогласованн[а-яё]*\s+формат\s+записи',f.get('issue',''),re.I)):
        return 'style','Редакторское предложение без доказанного обязательного требования.'
    # A claim about a missing letter must agree with the actual spelling change.
    missing=re.search(r'пропущен[аы]?\s+букв[ауы]\s+[«"„“]?([а-яё])[»"“]?\s+в\s+слове\s+[«"]([^»"]+)',text,re.I)
    if missing:
        char,target=missing[1].casefold(),missing[2].casefold();words=re.findall(r'[а-яё]+',quotes.casefold());near=difflib.get_close_matches(target,words,n=1,cutoff=.65)
        if near and target.count(char)<=near[0].count(char):return 'question','Объяснение о пропущенной букве не соответствует исходному и предлагаемому написанию; требуется корректное доказательство.'
    if re.search(r'орфограф|опечатк|spelling|typo',text,re.I):
        m=morph()
        if m:
            originals=re.findall(r'[А-Яа-яЁё]{4,}',quotes);targets=re.findall(r'[А-Яа-яЁё]{4,}',suggestion)
            for old in originals:
                # Protect consistently used project terms before fuzzy replacement matching.
                # A model can propose a completely different word, so edit distance is not a gate.
                targeted=bool(re.search(r'\b'+re.escape(old)+r'\b',f.get('issue',''),re.I) or re.search(r'(?:слов[а-я]*|написани[а-я]*|термин[а-я]*)\s+[«"]'+re.escape(old)+r'[»"]',reason,re.I))
                if not targeted and not m.word_is_known(old):
                    targeted=any(m.parse(w)[0].normal_form==m.parse(old)[0].normal_form for w in re.findall(r'[А-Яа-яЁё]{4,}',f.get('issue','')))
                if targeted and documents and not f.get('requirement_id') and not m.word_is_known(old):
                    lemma=m.parse(old)[0].normal_form;prefix=re.escape(lemma[:max(5,len(lemma)-3)])
                    uses=0;in_heading=False
                    for doc in documents:
                        if doc['id'] not in {e.get('document') for e in f['evidence']}:continue
                        for b in doc['blocks']:
                            if b.get('toc'):continue
                            matching=[w for w in re.findall(r'\b'+prefix+r'[а-яё]*\b',b['text'],re.I) if m.parse(w)[0].normal_form==lemma]
                            uses+=len(matching)
                            if matching and b.get('is_heading'):in_heading=True
                    if uses>=3 and in_heading:return 'question','Слово устойчиво используется в тексте и заголовках как термин проекта. Общий словарь и предположение о похожем слове недостаточны для его переименования; требуется терминологическое основание.'
                if old in targets:continue
                # Quoted replacement wording varies. Protect an unknown repeated
                # term even when the issue title does not explicitly name it.
                if documents and not f.get('requirement_id') and not m.word_is_known(old):
                    near=difflib.get_close_matches(old.casefold(),[w.casefold() for w in targets],n=1,cutoff=.72)
                    if near:
                        stem=old.casefold()[:max(5,len(old)-3)]
                        relevant=[b for d in documents if d['id'] in {e.get('document') for e in f['evidence']} for b in d['blocks'] if not b.get('toc')]
                        matches=[b for b in relevant if re.search(r'\b'+re.escape(stem)+r'[а-яё]*\b',b['text'],re.I)]
                        if len(matches)>=3 and any(b.get('is_heading') for b in matches):
                            return 'question','Предлагается переименовать устойчивый термин из заголовков и текста. Для замены нужен проверенный терминологический источник; сходство с общеупотребительным словом недостаточно.'
                similar=difflib.get_close_matches(old.casefold(),[w.casefold() for w in targets],n=1,cutoff=.84)
                if not similar:continue
                new=similar[0]
                if not m.word_is_known(old) and documents:
                    lemma=m.parse(old)[0].normal_form;prefix=re.escape(lemma[:max(5,len(lemma)-3)])
                    uses=0;in_heading=False
                    for doc in documents:
                        for b in doc['blocks']:
                            if b.get('toc'):continue
                            matching=[w for w in re.findall(r'\b'+prefix+r'[а-яё]*\b',b['text'],re.I) if m.parse(w)[0].normal_form==lemma]
                            uses+=len(matching)
                            if matching and b.get('is_heading'):in_heading=True
                    if uses>=3 and in_heading:
                        return 'question','Слово последовательно используется в нескольких местах и в заголовке как предметный термин. Отсутствие его в общем словаре не доказывает опечатку; замена требует подтверждения терминологией проекта.'
                if old.casefold().replace('ё','е')==new.replace('ё','е'):
                    return 'style','Изменение е/ё само по себе не доказывает орфографического нарушения.'
                if m.word_is_known(old) and not m.word_is_known(new):
                    return 'question','Исходная словоформа есть в словаре, предлагаемая не найдена. Это не окончательный вердикт, но автоматическое подтверждение такой замены запрещено.'
                if m.word_is_known(old) and m.word_is_known(new):
                    left={p.normal_form for p in m.parse(old) if p.is_known};right={p.normal_form for p in m.parse(new) if p.is_known}
                    if not left.intersection(right):
                        return 'question','Обе словоформы известны словарю и относятся к разным лексемам. Орфографическое исправление без доказательства смысловой ошибки не подтверждено.'
    if re.search(r'«([^»]+)»\s*\([^)]*\).*?написано\s*«\1»',text,re.I):
        return 'question','Объяснение сравнивает одинаковое написание; отличие не доказано.'
    return None,None
