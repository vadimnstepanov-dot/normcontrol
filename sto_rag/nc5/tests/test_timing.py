import unittest
from nc5.timing import remaining_seconds


class TimingTests(unittest.TestCase):
    def test_pending_dependencies_prevent_false_near_zero_forecast(self):
        job={'state':'running','data':{'derived':[],'options':{'check_logic':True},'relationships':{'groups':[]}}}
        evidence=lambda p:[{'document':'d','locator':p,'quote':'Текст'}]
        tasks=[{'id':'done','stage':'logic','state':'done','started':1,'ended':20,
            'result':{'facts':[{'parameter':'Срок','evidence':evidence('p1')},{'parameter':'Срок','evidence':evidence('p2')}]}}]
        docs=[{'blocks':[{'text':'См. приложение 3','toc':False}]}]
        value,future=remaining_seconds(job,tasks,[],docs,{},set(),25)
        self.assertGreater(value,60);self.assertEqual(future['cross'],2)

    def test_token_budget_and_cache_affect_estimate(self):
        job={'state':'running','data':{'derived':['cross','inter','verify'],'options':{}}}
        sample={'id':'done','stage':'logic','state':'done','started':1,'ended':10,'result':{'metrics':{
            'usage':{'prompt_tokens':1000,'completion_tokens':100},'timings':{'prompt_per_second':1000,'predicted_per_second':100}}}}
        pending={'id':'todo','cache_key':'key','stage':'logic','state':'pending','result':None}
        short,_=remaining_seconds(job,[sample,pending],[],[],{'todo':{'input_tokens':1000}},set(),11)
        long,_=remaining_seconds(job,[sample,pending],[],[],{'todo':{'input_tokens':10000}},set(),11)
        cached,_=remaining_seconds(job,[sample,pending],[],[],{'todo':{'input_tokens':10000}},{'key'},11)
        self.assertGreater(long,short);self.assertLess(cached,short)


if __name__=='__main__':unittest.main()
