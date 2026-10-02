"""Persisted ordinary dialogue through the configured, shared model gateway."""
import json,os,ssl,time,uuid,threading,urllib.request,urllib.error
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from django.conf import settings
from django.db import transaction,close_old_connections
from django.utils import timezone
from .models import ChatMessage,ChatResponse,LLMConfig,LLMRuntime
from .llm_connection import settings_payload,NoRedirect
from . import chat_knowledge

POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix='chat-model')
_future=None
_kick_lock=threading.Lock()
SYSTEM='Ты помощник NormControl. Отвечай на сообщение пользователя прямо, понятно и на его языке. Учитывай историю диалога. Ты не управляешь приложением: не утверждай, что запустил, остановил или завершил проверку, изменил файл или нормативную базу. Обсуждение не меняет сохранённые результаты нормоконтроля. В диалоге доступны текстовые сообщения и переданные выдержки RAG с каталогом опубликованной базы. Содержимое прикреплённых документов не передано. Старые ответы о недоступности СТО не описывают текущий доступ: ориентируйся на RAG_CONTEXT_JSON текущего сообщения. Не выдавай предположение за результат проверки документа. Если для ответа не хватает сведений, уточни их. '+chat_knowledge.RULES
class Cancelled(Exception):pass

def enqueue(c,key,text):
    if ChatResponse.objects.filter(user_message__conversation=c,state__in=['queued','running']).exists():
        raise ValueError('Дождитесь ответа модели или отмените текущий запрос.')
    user,_=c.messages.update_or_create(key=key,defaults={'role':'user','text':text,'metadata':{'kind':'dialogue'}})
    answer=c.messages.create(key=uuid.uuid5(key,'model-answer'),role='assistant',text='Ответ ожидает обработки.',metadata={'kind':'model','state':'queued'})
    response=ChatResponse.objects.create(user_message=user,assistant_message=answer)
    answer.metadata.update(response_id=str(response.pk),started_at=timezone.now().timestamp());answer.save(update_fields=['metadata'])
    if not c.batch_id and c.title=='Новая проверка':c.title=text[:160]
    c.draft={};c.save(update_fields=['title','draft','updated'])
    return response

def kick():
    global _future
    if not getattr(settings,'CHAT_MODEL_BACKGROUND',True):return
    with _kick_lock:
        if _future is None or _future.done():_future=POOL.submit(drain)
        return _future

def update(pk,lease,*,text=None,state=None,extra=None):
    with transaction.atomic():
        row=ChatResponse.objects.select_for_update().select_related('assistant_message').get(pk=pk)
        if row.state!='running' or row.lease!=lease:raise Cancelled()
        row.lease_until=timezone.now()+timedelta(seconds=120)
        if state:row.state=state
        row.save(update_fields=['lease_until','state','updated'])
        message=row.assistant_message;meta=dict(message.metadata,state=row.state)
        if extra:meta.update(extra)
        if state in ('done','failed','cancelled'):meta['finished_at']=timezone.now().timestamp()
        if text is not None and (message.text!=text or message.metadata!=meta):
            message.text=text;message.metadata=meta;message.save(update_fields=['text','metadata'])

def recover():
    """Unknown interrupted generations are never silently repeated."""
    now=timezone.now()
    for row in ChatResponse.objects.filter(state='running',lease_until__lt=now).select_related('assistant_message'):
        with transaction.atomic():
            changed=ChatResponse.objects.filter(pk=row.pk,state='running',lease=row.lease,lease_until__lt=now).update(state='failed',updated=now)
            if changed:
                meta=dict(row.assistant_message.metadata,state='failed',finished_at=now.timestamp())
                ChatMessage.objects.filter(pk=row.assistant_message_id).update(text='Ответ прерван после потери связи с обработчиком. Можно повторить запрос.',metadata=meta)

def claim():
    with transaction.atomic():
        recover()
        if ChatResponse.objects.filter(state='running').exists():return None
        row=ChatResponse.objects.filter(state='queued').order_by('created').first()
        if not row:return None
        row.state='running';row.lease=uuid.uuid4();row.lease_until=timezone.now()+timedelta(seconds=120)
        row.save(update_fields=['state','lease','lease_until','updated'])
        return row.pk,row.lease

def history(row):
    c=row.user_message.conversation
    c.owner.refresh_from_db(fields=['is_active'])
    if not c.owner.is_active:raise ValueError('Доступ к диалогу прекращён.')
    if c.batch_id:
        from .access import visible_batch
        visible_batch(c.owner,c.batch_id)
    messages=[]
    recent=list(c.messages.filter(id__lte=row.user_message_id).order_by('-id')[:60])
    for m in reversed(recent):
        if m.role=='user' or m.metadata.get('kind')=='model' and m.metadata.get('state')=='done':
            messages.append({'role':m.role,'content':m.text})
    # Keep whole recent turns; the exact model tokenizer trims further if needed.
    omitted=len(recent)==60 or len(messages)>25;messages=messages[-25:]
    while messages and messages[0]['role']!='user':messages.pop(0);omitted=True
    while sum(len(m['content']) for m in messages)>60000 and len(messages)>1:
        messages.pop(0);omitted=True
        while messages and messages[0]['role']!='user':messages.pop(0)
    return [{'role':'system','content':SYSTEM}]+messages,omitted

class Gateway:
    def __init__(self,config,pipeline,notice):
        self.config=config;self.pipeline_id=pipeline;self.pipeline_endpoint=config['endpoint'];self._queue_wait=notice
        ca=os.getenv('NORMCONTROL_LLM_CA_FILE')
        context=ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        self.context=context
    def http(self,path,value=None,timeout=15):
        if path!='/pipeline/ticket' and getattr(self,'guard',None):self.guard()
        headers={'Content-Type':'application/json','Accept':'application/json'}
        if self.config.get('api_key'):headers['Authorization']='Bearer '+self.config['api_key']
        req=urllib.request.Request(self.config['endpoint']+path,data=None if value is None else json.dumps(value,ensure_ascii=False).encode(),headers=headers)
        opener=urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=self.context))
        with opener.open(req,timeout=timeout) as response:
            raw=response.read(2*1024**2+1)
            if len(raw)>2*1024**2:raise ValueError('Ответ модели превышает лимит.')
            return json.loads(raw)
    def pipeline_request(self,value):return self.http('/pipeline/ticket',value)

def execute(pk,lease):
    row=ChatResponse.objects.select_related('user_message__conversation__owner','assistant_message').get(pk=pk)
    history(row)
    config=LLMConfig.objects.first()
    if not config:raise ValueError('Подключение модели ещё не настроено администратором.')
    connection=settings_payload(config,True)
    def search_notice(text):
        if text:update(pk,lease,text=text)
        elif not ChatResponse.objects.filter(pk=pk,state='running',lease=lease).exists():raise Cancelled()
    knowledge=chat_knowledge.retrieve(row,search_notice)
    last_notice=[0]
    def notice(ticket=None):
        if time.monotonic()-last_notice[0]<2:return False
        text='Ожидание свободной модели.'
        if ticket and ticket.get('waiting_reason')=='ram':
            text=f"Ожидание памяти: доступно {ticket.get('available_mb',0)} МиБ, резерв {ticket.get('reserve_mb',6144)} МиБ."
        update(pk,lease,text=text);last_notice[0]=time.monotonic();return False
    gateway=Gateway(connection,str(row.user_message.conversation_id),notice)
    def guard():
        if not ChatResponse.objects.filter(pk=pk,state='running',lease=lease).exists():raise Cancelled()
    gateway.guard=guard
    # Model demand uses the existing automatic desktop startup mechanism.
    while True:
        update(pk,lease,text='Ожидание подключения модели.')
        try:
            if gateway.http('/health').get('status')=='ok':break
        except urllib.error.HTTPError as error:
            if error.code in (401,403,404):raise ValueError('Подключение модели недоступно. Администратору нужно проверить настройки подключения.')
        except (OSError,ValueError):pass
        time.sleep(3)
    from pipeline import remote_turn
    with remote_turn(gateway,'chat',ram_mb=128):
        # Profile changes are serialized by the same GPU ticket as reviews.
        gateway.http('/model/profile',{'profile':'text'},timeout=connection['timeout'])
        props=gateway.http('/props');model=gateway.http('/v1/models')['data'][0]['id']
        context=min(connection['context'],int(props['default_generation_settings']['n_ctx']))
        output=min(connection['output'],2048)
        messages,omitted=history(row)
        chat_knowledge.validate(row.user_message.conversation.owner,knowledge)
        while True:
            prompt=chat_knowledge.compose(messages,knowledge)
            template=gateway.http('/apply-template',{'messages':prompt,'chat_template_kwargs':{'enable_thinking':False}})['prompt']
            tokens=len(gateway.http('/tokenize',{'content':template,'add_special':False})['tokens'])
            if tokens+output+512<=context:break
            if len(messages)<=2:
                if chat_knowledge.reduce(knowledge):continue
                raise ValueError('Сообщение не помещается в контекст модели. Сократите его.')
            messages.pop(1);omitted=True
            while len(messages)>2 and messages[1]['role']!='user':messages.pop(1)
        update(pk,lease,text='Модель готовит ответ.',extra={'context_omitted':omitted})
        answer=gateway.http('/v1/chat/completions',{'model':model,'messages':prompt,'temperature':0.3,'max_tokens':output,'stream':False,'chat_template_kwargs':{'enable_thinking':False}},timeout=connection['timeout'])
        value=answer['choices'][0]['message']['content']
        if not isinstance(value,str) or not value.strip() or len(value)>32000:raise ValueError('Модель не вернула допустимый текст ответа.')
        # Recheck access after the potentially long model turn.
        history(row)
        chat_knowledge.validate(row.user_message.conversation.owner,knowledge)
        value=chat_knowledge.citations(value.strip(),knowledge)
        update(pk,lease,text=value,state='done',extra={'model':model,'usage':answer.get('usage',{}),'truncated':answer['choices'][0].get('finish_reason')=='length',**chat_knowledge.metadata(knowledge)})

def drain():
    close_old_connections()
    try:
        while item:=claim():
            pk,lease=item
            stopped=threading.Event()
            def renew(response_pk=pk,response_lease=lease,response_stop=stopped):
                close_old_connections()
                try:
                    while not response_stop.wait(20):
                        try:update(response_pk,response_lease)
                        except Exception:return
                finally:close_old_connections()
            thread=threading.Thread(target=renew,daemon=True);thread.start()
            try:execute(pk,lease)
            except Cancelled:pass
            except Exception as error:
                # Do not expose gateway URLs, credentials, payloads or raw errors.
                text=str(error) if isinstance(error,ValueError) and not isinstance(error,json.JSONDecodeError) else 'Не удалось получить ответ модели. Можно повторить запрос.'
                try:update(pk,lease,text=text[:400],state='failed')
                except Cancelled:pass
            finally:stopped.set();thread.join(2)
    finally:close_old_connections()

def action(c,pk,action):
    with transaction.atomic():
        row=ChatResponse.objects.select_for_update().select_related('assistant_message').get(pk=pk,user_message__conversation=c)
        if action=='cancel' and row.state in ('queued','running'):
            row.state='cancelled'
        elif action=='retry' and row.state in ('failed','cancelled'):
            if ChatResponse.objects.filter(user_message__conversation=c,state__in=['queued','running']).exclude(pk=row.pk).exists():raise ValueError('В этой переписке уже ожидается ответ модели.')
            row.state='queued'
        else:raise ValueError('Это действие сейчас недоступно.')
        row.lease=None;row.lease_until=None;row.save(update_fields=['state','lease','lease_until','updated'])
        message=row.assistant_message;message.text='Запрос отменён.' if action=='cancel' else 'Ответ ожидает обработки.'
        message.metadata=dict(message.metadata,state=row.state,started_at=timezone.now().timestamp())
        message.metadata.pop('finished_at',None);message.save(update_fields=['text','metadata'])
