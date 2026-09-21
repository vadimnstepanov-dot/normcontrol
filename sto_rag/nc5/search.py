import math
import re
from collections import Counter,defaultdict
from .documents import compact_block

def terms(text):
    return [s[:7] if len(s)>7 else s for s in re.findall(r'[a-zа-яё0-9_]{3,}',text.lower()) if s not in ('который','которые','должен','должна','должны','системы','документа','требования')]

class Index:
    def __init__(self,docs):
        self.blocks=[b for d in docs for b in d['blocks'] if b['text'].strip() and not b.get('toc')];self.post=defaultdict(list);self.tfs=[];self.df=Counter();self.positions={}
        for i,b in enumerate(self.blocks):
            tf=Counter(terms(b['text']+' '+' / '.join(b.get('heading_path',[]))))
            self.tfs.append(tf);self.df.update(tf.keys());self.positions[(b['document'],b['locator'])]=i
            for term in tf:self.post[term].append(i)
    def retrieve(self,query,anchors=(),limit=12,neighbors=1):
        scores=Counter();n=len(self.blocks)
        for term in set(terms(query)):
            for i in self.post.get(term,[]):scores[i]+=math.log(1+n/(1+self.df[term]))*self.tfs[i][term]/(1+sum(self.tfs[i].values())/60)
        chosen=set()
        for e in anchors:
            i=self.positions.get((e['document'],e['locator']))
            if i is not None:chosen.add(i)
        for i,_ in scores.most_common(limit):chosen.add(i)
        for i in list(chosen):
            for j in range(max(0,i-neighbors),min(n,i+neighbors+1)):
                if self.blocks[j]['document']==self.blocks[i]['document'] and self.blocks[j].get('section')==self.blocks[i].get('section'):chosen.add(j)
        # Pull whole cells/row for table evidence rather than guessing column meanings.
        rows={(self.blocks[i]['document'],self.blocks[i].get('table_context',{}).get('table'),self.blocks[i].get('table_context',{}).get('row')) for i in chosen if self.blocks[i].get('table_context')}
        if rows:
            for i,b in enumerate(self.blocks):
                t=b.get('table_context',{})
                if (b['document'],t.get('table'),t.get('row')) in rows:chosen.add(i)
        return [compact_block(self.blocks[i]) for i in sorted(chosen)]

    def search_record(self,query):
        return {'query':query,'method':'lexical_full_index','indexed_blocks':len(self.blocks),'complete_text_index':True,'does_not_prove_semantic_absence':True}
