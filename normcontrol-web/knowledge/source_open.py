"""Read-only Office handoff: scoped, expiring permission for the original file."""
from urllib.parse import urlencode
from django.core import signing
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.http import JsonResponse, FileResponse
from django.urls import reverse
from .models import SourceUpload
from .access import require
from .views import boundary, fields
from . import uploads

SALT='normcontrol-original-word-v1'
MAX_AGE=300

@boundary({'POST'})
def open_document(request,set_id,source_id):
    fields(request,set())
    source=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
    require(request.user,source.normative_set.scope,'read')
    if not (uploads.incoming_directory()/source.storage_key).is_file():
        return JsonResponse({'error':'source_unavailable'},status=503)
    if not source.filename.lower().endswith(('.doc','.docx')):
        return JsonResponse({'error':'word_format_required'},status=400)
    token=signing.dumps(dict(user=request.user.pk,source=str(source.pk),area=str(set_id),sha256=source.sha256),salt=SALT)
    path=reverse('knowledge-source-word',kwargs=dict(set_id=set_id,source_id=source_id))
    url=request.build_absolute_uri(path)+'?'+urlencode({'token':token})
    r=JsonResponse(dict(word_url='ms-word:ofv|u|'+url,expires_in=MAX_AGE))
    r['Cache-Control']='private, no-store';return r

def word_original(request,set_id,source_id):
    if request.method not in ('GET','HEAD'):return JsonResponse({'error':'method_not_allowed'},status=405)
    try:
        token=signing.loads(request.GET.get('token',''),salt=SALT,max_age=MAX_AGE)
        if not isinstance(token,dict):raise signing.BadSignature()
        if token.get('source')!=str(source_id) or token.get('area')!=str(set_id):raise signing.BadSignature()
        source=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
        if token.get('sha256')!=source.sha256:raise signing.BadSignature()
        user=get_user_model().objects.get(pk=token.get('user'),is_active=True)
        require(user,source.normative_set.scope,'read')
        if not source.filename.lower().endswith(('.doc','.docx')):raise PermissionDenied()
        r=FileResponse((uploads.incoming_directory()/source.storage_key).open('rb'),as_attachment=False,filename=source.filename)
        r['Cache-Control']='private, no-store';r['Referrer-Policy']='no-referrer';r['X-Content-Type-Options']='nosniff'
        return r
    except (signing.BadSignature,PermissionDenied,ObjectDoesNotExist,TypeError,ValueError):
        return JsonResponse({'error':'access_denied_or_link_expired'},status=403)
    except OSError:return JsonResponse({'error':'source_unavailable'},status=503)
