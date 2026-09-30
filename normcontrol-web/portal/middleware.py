from django.shortcuts import redirect
from .models import AccessProfile

class PasswordChangeMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        if request.user.is_authenticated and request.path not in ('/normcontol/account/password/','/normcontol/logout/') and not request.path.startswith('/normcontol/static/'):
            if AccessProfile.objects.filter(user=request.user,must_change_password=True).exists():return redirect('password')
        return self.get_response(request)

class HeadersMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        response['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
        response['X-Frame-Options']='SAMEORIGIN'
        response['Referrer-Policy']='same-origin'
        response['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        if not request.path.startswith('/normcontol/static/'):response['Cache-Control']='no-store'
        return response

class PresentationMiddleware:
    """Keep deep HTML links in the persistent shell; API/export rights stay intact."""
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        # The expert frame stays embedded after ordinary links and form redirects.
        if request.headers.get('Sec-Fetch-Dest')=='iframe':
            query=request.GET.copy();query['embedded']='1';request.GET=query
        return self.get_response(request)
    def process_view(self,request,view,args,kwargs):
        if request.method!='GET' or not request.user.is_authenticated or request.headers.get('Sec-Fetch-Dest')!='document':return None
        if request.GET.get('format') or request.GET.get('embedded')=='1':return None
        name=request.resolver_match.url_name or ''
        full={'new','batch','reports','word-review','batch-report','knowledge-check-start','knowledge-check-monitor','rag','knowledge-workspace','knowledge-expert','knowledge-profiles','llm','queue','users','audit'}
        if name not in full:return None
        from django.urls import reverse
        from .chat import page
        from .access import visible_batch
        query=request.GET.copy();query['embedded']='1'
        request.chat_expert_url=request.path+'?'+query.urlencode()
        request.chat_presentation='expert'
        query=request.GET.copy()
        batch_id=kwargs.get('pk') if name in ('batch','word-review','batch-report') else kwargs.get('batch_id')
        if name=='knowledge-check-monitor':
            from knowledge.models import KnowledgeCheck
            from knowledge.checks import visible
            from django.shortcuts import get_object_or_404
            job=get_object_or_404(KnowledgeCheck,pk=kwargs['job_id']);visible(request.user,job);batch_id=job.batch_id
        if batch_id:
            batch=visible_batch(request.user,batch_id)
            query['batch']=str(batch_id)
        request.GET=query
        return page(request)
