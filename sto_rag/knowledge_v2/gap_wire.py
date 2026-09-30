"""Lossless transport for repeated source-gap metadata; opt-in, never clearance."""
import copy
import re

VERSION='gap-groups-v1'
POLICY=('В completeness.gaps с encoding=gap-groups-v1 группы groups перечислены по порядку. '
    'Каждая группа задаёт общие поля common для всех её locators. locators — список строк '
    'или объект prefix/numbers/suffix: каждый locator равен prefix + десятичный номер + suffix. '
    'Разверни группы последовательно; state и reason_ref из common относятся к каждому locator. '
    'reason_ref раскрывается в completeness.gap_reasons. Все исходные ограничения чтения сохранены; '
    'компактная запись не доказывает прочтение области и не разрешает вывод о глобальном отсутствии.')

def pack(gaps):
    """Keep input order, duplicates, unknown fields and exact locator spelling."""
    if not isinstance(gaps,list) or not gaps:return copy.deepcopy(gaps)
    if any(not isinstance(g,dict) or not isinstance(g.get('locator'),str) for g in gaps):
        return copy.deepcopy(gaps)
    groups=[]
    for gap in gaps:
        common={k:copy.deepcopy(v) for k,v in gap.items() if k!='locator'}
        if not groups or groups[-1]['common']!=common:
            groups.append(dict(common=common,locators=[]))
        groups[-1]['locators'].append(gap['locator'])
    for group in groups:
        values=group['locators']
        parsed=[re.fullmatch(r'([^0-9]*)([0-9]+)([^0-9]*)',s) for s in values]
        if (len(values)>2 and all(parsed) and len({(m[1],m[3]) for m in parsed})==1
                and all(str(int(m[2]))==m[2] for m in parsed)):
            group['locators']=dict(prefix=parsed[0][1],numbers=[int(m[2]) for m in parsed],suffix=parsed[0][3])
    return dict(encoding=VERSION,groups=groups)

def unpack(value):
    """Validation helper; canonical evidence and the runner keep original gaps."""
    if not isinstance(value,dict) or value.get('encoding')!=VERSION:return copy.deepcopy(value)
    result=[]
    for group in value['groups']:
        locators=group['locators']
        if isinstance(locators,dict):
            locators=[locators['prefix']+str(n)+locators['suffix'] for n in locators['numbers']]
        result.extend(dict(copy.deepcopy(group['common']),locator=s) for s in locators)
    return result
