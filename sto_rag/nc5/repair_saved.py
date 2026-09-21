"""Replay validation of saved responses after a schema fix; never re-read through the LLM."""
from .store import Store
from .common import DATA,dumps

def replay(jid):
    s=Store()
    if any(t['state']=='running' for t in s.tasks(jid)):raise ValueError('Дождитесь окончания текущей ограниченной задачи')
    count=0
    for t in s.tasks(jid):
        if t['stage']!='language' or t['state']!='done' or not t['result']:continue
        r=t['result']
        if not r.get('invalid'):continue
        if not any(x.get('error')=='Неизвестная нормативная ссылка' for x in r['invalid']):continue
        s.cache(t['cache_key'],{'raw':r['raw'],'metrics':r['metrics']})
        with s.connect() as c:c.execute("UPDATE tasks SET state='pending' WHERE id=?",(t['id'],))
        s.event(jid,'validation_replay',{'task':t['id'],'reason':'language stage label was incorrectly supplied as a normative ID; original model response reused'})
        count+=1
    return count

if __name__=='__main__':
    import sys
    print(replay(sys.argv[1]))
