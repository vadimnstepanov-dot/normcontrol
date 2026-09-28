"""Authenticated host profile switching while holding the shared inference turn."""
import os,time,urllib.request,urllib.error,json

def enabled():return os.getenv('KNOWLEDGE_LLM_PROFILE_CONTROL','0')=='1'

def ensure(client,profile):
    if profile not in ('text','vision'):raise ValueError('Model profile')
    if not enabled():return
    if not getattr(client,'_model_ticket',None):raise RuntimeError('Profile switch requires an exclusive model turn')
    headers={'Content-Type':'application/json'}
    key=getattr(client,'api_key','') or os.getenv('NORMCONTROL_LLM_API_KEY','')
    if key:headers['Authorization']='Bearer '+key
    request=urllib.request.Request(client.endpoint+'/model/profile',data=json.dumps({'profile':profile}).encode(),headers=headers)
    deadline=time.monotonic()+420
    while True:
        try:
            with urllib.request.urlopen(request,timeout=420) as response:reply=json.load(response)
            if reply.get('profile')!=profile or reply.get('ready') is not True:raise RuntimeError('Profile not confirmed ready')
            return
        except urllib.error.HTTPError as error:
            if error.code!=409 or time.monotonic()>deadline:raise
            time.sleep(1)
