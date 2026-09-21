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
        response['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response['Referrer-Policy']='same-origin'
        response['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        if not request.path.startswith('/normcontol/static/'):response['Cache-Control']='no-store'
        return response
