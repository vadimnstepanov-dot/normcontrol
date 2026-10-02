import json, ssl, urllib.request, urllib.error
from pathlib import Path

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Bridge redirects are forbidden')

class Transport:
    def __init__(self, endpoint, token, certificate):
        if not endpoint.startswith('https://'):raise ValueError('TLS bridge required')
        self.endpoint=endpoint.rstrip('/');self.token=token
        self.context=ssl.create_default_context(cafile=certificate)
    def open(self,path,data=None,headers=None,timeout=20):
        request=urllib.request.Request(self.endpoint+path,data=data,
            headers={'Authorization':'Bearer '+self.token,**(headers or {})})
        opener=urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=self.context))
        return opener.open(request,timeout=timeout)
    def json(self,path,value=None,timeout=20,headers=None):
        raw=None if value is None else json.dumps(value,ensure_ascii=False).encode()
        with self.open(path,raw,{'Content-Type':'application/json',**(headers or {})},timeout) as response:
            body=response.read(4*1024**2+1)
            if len(body)>4*1024**2:raise ValueError('Bridge response too large')
            return json.loads(body)

def secret(path):return Path(path).read_text(encoding='utf-8-sig').strip()
