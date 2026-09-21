"""Administrator-configured, allowlisted llama.cpp endpoint. Never follow redirects."""
import json
import os
import urllib.request
import ssl
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from django.utils import timezone

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None

def settings_payload(c,secret=False):
    url=c.endpoint.rstrip('/')
    if url.endswith('/v1'):url=url[:-3]
    parts=urlsplit(url);origin=parts.scheme+'://'+parts.netloc
    allowed=os.getenv('NORMCONTROL_LLM_ALLOWED_ORIGINS','http://127.0.0.1:8098,http://localhost:8098').split(',')
    if origin not in allowed or parts.username or parts.password or parts.query or parts.fragment:raise ValueError('Адрес не входит в список разрешённых подключений сервера')
    if c.concurrency!=1:raise ValueError('Для этого обработчика используется один запрос одновременно')
    data={'endpoint':url,'model':c.model or 'local-qwen','context':c.context_tokens,'output':c.output_tokens,'timeout':c.timeout_seconds,'concurrency':1}
    if secret and c.key_encrypted:
        data['api_key']=Fernet(os.environ['APP_CRYPT_KEY'].encode()).decrypt(c.key_encrypted.encode()).decode()
    return data

def probe(c):
    result={'at':timezone.now().isoformat(),'ok':False}
    try:
        data=settings_payload(c,True);headers={'Accept':'application/json'}
        if data.get('api_key'):headers['Authorization']='Bearer '+data['api_key']
        req=urllib.request.Request(data['endpoint']+'/props',headers=headers)
        ca=os.getenv('NORMCONTROL_LLM_CA_FILE');context=ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        with urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=context)).open(req,timeout=12) as response:
            raw=response.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('Ответ превышает лимит')
            props=json.loads(raw)
        actual=int(props['default_generation_settings']['n_ctx'])
        result.update(ok=True,actual_context=actual,effective_context=min(actual,data['context']),model=props.get('model_path'),slots=props.get('total_slots'))
    except ValueError as e:result['error']=str(e)
    except Exception as e:result['error']='Соединение не установлено: '+type(e).__name__
    c.last_probe=result;c.save(update_fields=['last_probe']);return result
