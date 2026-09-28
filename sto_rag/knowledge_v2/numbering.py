"""Word list labels from explicit numbering definitions; unsupported formats stay unknown."""
import re
from .ingest import W


class Numbering:
    def __init__(self, root):
        self.abstracts={x.get(W+'abstractNumId'):x for x in root.findall(W+'abstractNum')} if root is not None else {}
        self.nums={x.get(W+'numId'):x for x in root.findall(W+'num')} if root is not None else {}
        self.counters={};self.seen=set()

    def definition(self, num, level):
        n=self.nums.get(num)
        if n is None:return None
        aid=n.find(W+'abstractNumId');a=self.abstracts.get(aid.get(W+'val')) if aid is not None else None
        if a is None:return None
        result=a.find(f'{W}lvl[@{W}ilvl="{level}"]')
        override=n.find(f'{W}lvlOverride[@{W}ilvl="{level}"]')
        if override is not None and override.find(W+'lvl') is not None:result=override.find(W+'lvl')
        if result is None:return None
        def val(key,default):
            x=result.find(W+key);return x.get(W+'val',default) if x is not None else default
        start=val('start','1')
        if override is not None and override.find(W+'startOverride') is not None:start=override.find(W+'startOverride').get(W+'val','1')
        return dict(start=int(start),fmt=val('numFmt','decimal'),text=val('lvlText','%1'),restart=val('lvlRestart',str(level)))

    def label(self, props):
        num=props.get('num');level=int(props.get('ilvl','0'))
        if num in (None,'0'):return '',None
        d=self.definition(num,level)
        if not d:return '', 'numbering_definition_missing'
        key=(num,level)
        self.counters[key]=self.counters.get(key,d['start']-1)+1
        for child in range(level+1,9):
            cd=self.definition(num,child)
            if cd and int(cd['restart'])!=0 and level < int(cd['restart']):self.counters.pop((num,child),None)
        def fmt(value,form):
            if form=='decimal':return str(value)
            if form in ('lowerRoman','upperRoman') and 0<value<4000:
                roman=''
                for number,letter in ((1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),(50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')):
                    while value>=number:roman+=letter;value-=number
                return roman.lower() if form=='lowerRoman' else roman
            if form in ('lowerLetter','upperLetter') and 1<=value<=26:
                return chr((97 if form=='lowerLetter' else 65)+value-1)
            if form=='bullet':return d['text']
            raise ValueError('unsupported_number_format')
        try:
            def substitute(m):
                l=int(m.group(1))-1;ld=self.definition(num,l)
                if not ld:raise ValueError('numbering_parent_missing')
                return fmt(self.counters.get((num,l),ld['start']),ld['fmt'])
            return re.sub(r'%([1-9])',substitute,d['text']),None
        except ValueError as e:return '',str(e)
