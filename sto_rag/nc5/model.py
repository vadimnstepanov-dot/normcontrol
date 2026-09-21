import json
import copy
import time
import urllib.request
import os
import ssl
from .common import dumps,digest

from .prompts import POLICY, stage_policy

def obj(props):return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}
STR={'type':'string'};ARR=lambda schema:{'type':'array','items':schema}
EVIDENCE=obj({'document':STR,'locator':STR,'quote':STR})
FINDING=obj({'category':STR,'severity':STR,'issue':STR,'explanation':STR,'suggestion':STR,'kind':{'type':'string','enum':['violation','question','style']},'evidence':ARR(EVIDENCE),'requirement_id':STR,'search_query':STR})
FACT=obj({'entity':STR,'parameter':STR,'value':STR,'operator':STR,'unit':STR,'scope':STR,'environment':STR,'conditions':STR,'time_basis':STR,'evidence':ARR(EVIDENCE)})
SCHEMA=obj({'findings':ARR(FINDING),'facts':ARR(FACT),'coverage':ARR(obj({'requirement_id':STR,'state':{'type':'string','enum':['checked','not_applicable','unknown','insufficient']},'reason':STR})), 'decisions':ARR(obj({'id':STR,'verdict':{'type':'string','enum':['confirmed','rejected','question']},'reason':STR,'suggestion':STR})), 'limitations':ARR(STR)})

class BudgetError(ValueError):pass
class OutputError(ValueError):
    def __init__(self,message,response=None,seconds=0):
        super().__init__(message);self.response=response or {};self.metrics={'seconds':seconds,'usage':self.response.get('usage',{}),'timings':self.response.get('timings',{})}

def output_budget(config,payload):
    # Real answers are normally a few hundred tokens.  Smaller stage budgets leave
    # room for larger source batches; an overflow is split and retried by Engine.
    cap={'language':1024,'logic':2048,'sto':1792,'cross':2048,'inter':2048,'verify':1280,'feedback':1280}.get(payload['stage'],config['output'])
    if payload['stage']=='verify' and config.get('verify_reasoning'):cap=2048
    return min(config['output'],max(cap,int(payload.get('_output_budget',0))))

class Client:
    def __init__(self,config):self.config=config;self.context=config['context'];self.props={};self.signature='';self.count_cache={}
    def http(self,path,payload=None,timeout=None):
        headers={'Content-Type':'application/json'}
        key=os.getenv('NORMCONTROL_LLM_API_KEY')
        if key:headers['Authorization']='Bearer '+key
        req=urllib.request.Request(self.config['endpoint'].rstrip('/')+path,data=dumps(payload).encode() if payload is not None else None,headers=headers)
        ca=os.getenv('NORMCONTROL_LLM_CA_FILE');context=ssl.create_default_context(cafile=ca) if ca else None
        with urllib.request.urlopen(req,timeout=timeout or self.config['timeout'],context=context) as r:return json.load(r)
    def probe(self):
        self.props=self.http('/props',timeout=15)
        real=int(self.props['default_generation_settings']['n_ctx']);self.context=min(real,self.config['context'])
        self.signature=digest({'path':self.props.get('model_path'),'meta':self.props.get('model_meta'),'template':self.props.get('chat_template'),'params':self.props.get('default_generation_settings')})
        return {'context':self.context,'actual_context':real,'model':self.props.get('model_path'),'signature':self.signature,'slots':self.props.get('total_slots')}
    def request(self,payload):
        # Serialize shared table headings once. Repeated per-cell copies can cost more than the source text.
        # Keys prefixed with '_' are planner metadata and must never consume model
        # context. They are retained in the task only for safe evidence-aware splits.
        payload={k:v for k,v in payload.items() if k not in ('_evidence_map','_document_id')};table_context={};section_context={};blocks=[]
        for b in payload.get('blocks',[]):
            b=dict(b);t=b.get('table')
            path=b.pop('heading_path',None)
            if path:
                key=digest([b['document'],path])[:12];section_context[key]=path;b['section_context']=key
            if t:
                t=dict(t);key=b['document']+':'+str(t.get('table'))
                ctx=table_context.setdefault(key,{'columns':{},'title':t.pop('title','')})
                col=str(t.get('column'));name=t.pop('column_name','')
                if name:ctx['columns'][col]=name
                t['context']=key;b['table']=t
            blocks.append(b)
        payload['blocks']=blocks
        if table_context:payload['table_context']=table_context
        if section_context:payload['section_context']=section_context
        schema=copy.deepcopy(SCHEMA);rids=[r['requirement_id'] for r in payload.get('requirements',[]) if r.get('requirement_id')]
        schema['properties']['findings']['items']['properties']['category']={'type':'string','enum':['грамотность','оформление','техническая логика','структура СТО','соответствие СТО','межраздельная логика','междокументная логика','арифметика']}
        if payload['stage']=='sto':schema['properties']['findings']['items']['properties']['category']['enum']=['соответствие СТО','структура СТО','оформление']
        if not rids:
            category={'language':['грамотность'],'logic':['техническая логика','грамотность','арифметика'],'cross':['межраздельная логика','техническая логика','арифметика'],'inter':['междокументная логика','арифметика']}.get(payload['stage'])
            if category:schema['properties']['findings']['items']['properties']['category']['enum']=category
        schema['properties']['facts']['maxItems']=8
        schema['properties']['findings']['items']['properties']['requirement_id']={'type':'string','enum':['']+list(dict.fromkeys(rids))}
        if not rids:schema['properties']['coverage']['maxItems']=0
        else:
            schema['properties']['coverage']['items']['properties']['requirement_id']={'type':'string','enum':list(dict.fromkeys(rids))}
            schema['properties']['coverage']['maxItems']=len(set(rids))
        if payload.get('validation_retry'):schema['properties']['coverage']['maxItems']=0
        if payload['stage'] in ('language','verify','sto','feedback','inter','cross'):schema['properties']['facts']['maxItems']=0
        if payload.get('images'):
            schema['properties']['facts']['maxItems']=0
            schema['properties']['findings']['maxItems']=3
            schema['properties']['findings']['items']['properties']['category']={'type':'string','enum':['техническая логика']}
            schema['properties']['findings']['items']['properties']['kind']={'type':'string','enum':['violation','question']}
            schema['properties']['analysis_nodes']=ARR(obj({'node':STR,'visible_outgoing':ARR(STR),'missing_outcome':STR,'readable':{'type':'boolean'}}))
            schema['properties']['analysis_nodes']['maxItems']=16
            schema['required'].append('analysis_nodes')
        if payload['stage']=='verify':schema['properties']['findings']['maxItems']=0
        if payload['stage']=='verify':schema['properties']['decisions']['maxItems']=len(payload.get('candidates',[]))
        if payload['stage'] not in ('verify','feedback'):schema['properties']['decisions']['maxItems']=0
        system=stage_policy(payload['stage']); user=dumps(payload)
        if payload.get('images'):
            from .vision import content,VISUAL
            system=VISUAL
            clean={k:v for k,v in payload.items() if k!='images'}
            user=[{'type':'text','text':dumps(clean)}]+[content(i) for i in payload['images']]
        return {'model':self.config['model'],'messages':[{'role':'system','content':system},{'role':'user','content':user}],
            'tools':[],'tool_choice':'none','stream':False,'temperature':0.1,'max_tokens':output_budget(self.config,payload),
            'chat_template_kwargs':{'enable_thinking':bool(payload['stage']=='verify' and self.config.get('verify_reasoning')),'preserve_thinking':False},
            'response_format':{'type':'json_schema','json_schema':{'name':'review_v5','strict':True,'schema':schema}}}
    def count(self,payload):
        key=digest(payload)
        if key in self.count_cache:return self.count_cache[key]
        req=self.request(payload);args={k:req[k] for k in ('messages','tools','tool_choice','chat_template_kwargs')}
        if payload.get('images'):
            args=copy.deepcopy(args)
            for message in args['messages']:
                if isinstance(message['content'],list):message['content']='\n'.join(x['text'] for x in message['content'] if x['type']=='text')
        prompt=self.http('/apply-template',args,60)['prompt']
        n=len(self.http('/tokenize',{'content':prompt,'add_special':False,'parse_special':True},60)['tokens'])
        # Schema is server grammar, but count it conservatively in case the runtime also injects it.
        schema_tokens=len(self.http('/tokenize',{'content':dumps(req['response_format']['json_schema']['schema']),'add_special':False},60)['tokens'])
        n+=sum(i['tokens_reserve'] for i in payload.get('images',[]))
        self.count_cache[key]=n+schema_tokens
        if len(self.count_cache)>2000:self.count_cache.clear()
        return n+schema_tokens
    def generate(self,payload):
        tokens=self.count(payload)
        budget=output_budget(self.config,payload)
        if tokens+budget+self.config['margin']>self.context:raise BudgetError(f'Пакет {tokens} + ответ {budget} + резерв {self.config["margin"]} > {self.context}')
        from .runtime import Lease
        from .common import DATA
        start=time.time()
        attempts=[]
        with Lease(DATA/'model.lock'):
            r=self.http('/v1/chat/completions',self.request(payload))
            attempts.append({'usage':r.get('usage',{}),'timings':r.get('timings',{}),'budget':budget})
            # Retry the same evidence once before fragmenting it. The reserve and
            # configured output ceiling remain hard limits, including for images.
            enlarged=min(self.config['output'],budget*2,self.context-tokens-self.config['margin'])
            if r['choices'][0].get('finish_reason')=='length' and enlarged>budget:
                r=self.http('/v1/chat/completions',self.request({**payload,'_output_budget':enlarged}))
                attempts.append({'usage':r.get('usage',{}),'timings':r.get('timings',{}),'budget':enlarged})
        c=r['choices'][0]
        def failed(message):
            error=OutputError(message,r,time.time()-start)
            error.metrics['generation_attempts']=attempts
            error.metrics['usage']={k:sum(a['usage'].get(k,0) for a in attempts) for k in ('prompt_tokens','completion_tokens','total_tokens')}
            return error
        if c.get('finish_reason')!='stop' or c['message'].get('tool_calls'):raise failed('Незавершённый ответ: '+str(c.get('finish_reason')))
        try:value=json.loads(c['message']['content'])
        except (ValueError,TypeError):raise failed('Некорректный JSON в ответе')
        if not isinstance(value,dict) or any(not isinstance(value.get(k),list) for k in SCHEMA['required']):raise failed('Ответ не соответствует схеме')
        usage={k:sum(a['usage'].get(k,0) for a in attempts) for k in ('prompt_tokens','completion_tokens','total_tokens')}
        timings=dict(r.get('timings',{}))
        for key in ('prompt_n','prompt_ms','predicted_n','predicted_ms'):
            if any(key in a['timings'] for a in attempts):timings[key]=sum(a['timings'].get(key,0) for a in attempts)
        for prefix in ('prompt','predicted'):
            if timings.get(prefix+'_ms'):timings[prefix+'_per_second']=1000*timings.get(prefix+'_n',0)/timings[prefix+'_ms']
        return value,{'estimated_tokens':tokens,'usage':usage,'timings':timings,'seconds':time.time()-start,'context':self.context,'generation_attempts':attempts}
