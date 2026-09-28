"""Human workspace for the isolated v2 normative knowledge base."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from .models import Scope
from .access import allowed


@login_required
def workspace(request):
    scopes=[dict(id=str(s.pk),name=s.name,kind=s.kind,can_upload=allowed(request.user,s,'upload'),
                 can_publish=allowed(request.user,s,'publish'))
            for s in Scope.objects.select_related('owner').order_by('name','id') if allowed(request.user,s,'read')]
    return render(request,'knowledge_workspace.html',{'page':'rag','scopes':scopes,
                  'has_upload_scope':any(s['can_upload'] for s in scopes)})
