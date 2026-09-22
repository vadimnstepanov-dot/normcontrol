from django.urls import path,include
from django.http import HttpResponse
from . import views as v
from . import worker_api as w
routes=[path('',v.dashboard,name='dashboard'),path('login/',v.sign_in,name='login'),path('register/',v.self_register,name='register'),path('logout/',v.sign_out,name='logout'),
path('batches/new/',v.new_batch,name='new'),path('batches/<uuid:pk>/',v.detail,name='batch'),path('batches/<uuid:pk>/action/',v.batch_action,name='batch-action'),path('documents/<int:pk>/download/',v.download,name='download'),
path('reports/',v.reports,name='reports'),path('rag/',v.rag,name='rag'),path('settings/llm/',v.llm,name='llm'),path('settings/queue/',v.queue,name='queue'),path('settings/queue/<uuid:pk>/action/',v.queue_action,name='queue-action'),path('settings/users/',v.users,name='users'),path('settings/users/<int:pk>/toggle/',v.toggle_user,name='toggle-user'),path('settings/users/<int:pk>/password/',v.reset_user,name='reset-user'),path('settings/audit/',v.audit_log,name='audit'),path('account/password/',v.password,name='password'),path('health/',v.health)]
routes += [path('worker/claim/',w.claim),path('worker/<uuid:lease>/files/<int:pk>/',w.file),path('worker/<uuid:lease>/update/',w.update),path('worker/<uuid:lease>/feedback/<int:pk>/',w.feedback_result),path('batches/<uuid:pk>/status/',w.status,name='batch-status'),path('batches/<uuid:pk>/report/',w.report,name='batch-report'),path('batches/<uuid:pk>/feedback/',w.feedback,name='batch-feedback')]
routes.append(path('batches/<uuid:pk>/wake/',w.wake,name='batch-wake'))
routes.append(path('batches/<uuid:pk>/findings/<str:finding_id>/disposition/',w.disposition,name='finding-disposition'))
routes.append(path('worker/feedback/',w.pending_feedback))
routes.extend([path('worker/config/',w.configuration),path('settings/llm/probe/',v.llm_probe,name='llm-probe')])
routes.append(path('worker/ping/',w.ping))
routes.append(path('batches/<uuid:pk>/delete/',v.delete_batch,name='batch-delete'))

def legacy(request,rest=''):
    location='/normcontol/'+rest
    if request.META.get('QUERY_STRING'):location+='?'+request.META['QUERY_STRING']
    response=HttpResponse(status=308);response['Location']=location;return response

urlpatterns=[path('normcontol/',include(routes)),path('LLM/',legacy),path('LLM/<path:rest>',legacy)]
