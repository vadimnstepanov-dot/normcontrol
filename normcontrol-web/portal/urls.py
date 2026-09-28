from django.urls import path,include
from django.http import HttpResponse
from . import views as v
from . import worker_api as w
routes=[path('',v.dashboard,name='dashboard'),path('login/',v.sign_in,name='login'),path('register/',v.self_register,name='register'),path('logout/',v.sign_out,name='logout'),
path('batches/new/',v.new_batch,name='new'),path('batches/<uuid:pk>/',v.detail,name='batch'),path('batches/<uuid:pk>/action/',v.batch_action,name='batch-action'),path('batches/<uuid:pk>/documents/add/',v.add_documents,name='batch-add-documents'),path('documents/<int:pk>/download/',v.download,name='download'),
path('reports/',v.reports,name='reports'),path('rag/',v.rag,name='rag'),path('settings/llm/',v.llm,name='llm'),path('settings/queue/',v.queue,name='queue'),path('settings/queue/<uuid:pk>/action/',v.queue_action,name='queue-action'),path('settings/users/',v.users,name='users'),path('settings/users/<int:pk>/toggle/',v.toggle_user,name='toggle-user'),path('settings/users/<int:pk>/password/',v.reset_user,name='reset-user'),path('settings/audit/',v.audit_log,name='audit'),path('account/password/',v.password,name='password'),path('health/',v.health)]
routes += [path('worker/claim/',w.claim),path('worker/<uuid:lease>/files/<int:pk>/',w.file),path('worker/<uuid:lease>/update/',w.update),path('worker/<uuid:lease>/feedback/<int:pk>/',w.feedback_result),path('batches/<uuid:pk>/status/',w.status,name='batch-status'),path('batches/<uuid:pk>/report/',w.report,name='batch-report'),path('batches/<uuid:pk>/feedback/',w.feedback,name='batch-feedback')]
routes.append(path('batches/<uuid:pk>/register/',w.register,name='batch-register'))
routes.append(path('batches/<uuid:pk>/wake/',w.wake,name='batch-wake'))
routes.append(path('batches/<uuid:pk>/findings/<str:finding_id>/disposition/',w.disposition,name='finding-disposition'))
routes.append(path('worker/feedback/',w.pending_feedback))
routes.extend([path('worker/config/',w.configuration),path('settings/llm/probe/',v.llm_probe,name='llm-probe')])
routes.append(path('worker/ping/',w.ping))
routes.extend([path('worker/llm/telemetry/',w.llm_telemetry),path('worker/llm/command/',w.llm_command),path('llm/status/',w.llm_status,name='llm-status'),path('llm/action/',w.llm_action,name='llm-action')])
routes.append(path('batches/<uuid:pk>/delete/',v.delete_batch,name='batch-delete'))
routes.append(path('settings/users/<int:pk>/view-others/',v.toggle_view_others,name='user-view-others'))
routes.append(path('settings/users/<int:pk>/admin/',v.toggle_admin,name='user-admin'))

def legacy(request,rest=''):
    location='/normcontol/'+rest
    if request.META.get('QUERY_STRING'):location+='?'+request.META['QUERY_STRING']
    response=HttpResponse(status=308);response['Location']=location;return response

urlpatterns=[path('normcontol/',include(routes)),path('LLM/',legacy),path('LLM/<path:rest>',legacy)]
from django.conf import settings
if settings.KNOWLEDGE_V2_ENABLED:
    from knowledge.trace_views import workspace as trace_workspace
    urlpatterns.append(path('normcontol/knowledge/trace/',trace_workspace,name='knowledge-trace'))
    from knowledge.profile_editor import editor as profile_editor
    from knowledge.workspace import workspace as knowledge_workspace
    from knowledge.expert_views import workspace as expert_workspace, release_workspace
    urlpatterns.append(path('normcontol/knowledge/expert/',expert_workspace,name='knowledge-expert'))
    urlpatterns.append(path('normcontol/knowledge/releases/<uuid:release_id>/',release_workspace,name='knowledge-release'))
    from knowledge.check_ui import start as knowledge_check_start, monitor as knowledge_check_monitor
    urlpatterns.append(path('normcontol/knowledge/',knowledge_workspace,name='knowledge-workspace'))
    urlpatterns.append(path('normcontol/knowledge/check/<uuid:batch_id>/',knowledge_check_start,name='knowledge-check-start'))
    urlpatterns.append(path('normcontol/knowledge/checks/<uuid:job_id>/',knowledge_check_monitor,name='knowledge-check-monitor'))
    urlpatterns.append(path('normcontol/knowledge/profiles/',profile_editor,name='knowledge-profiles'))
    urlpatterns.append(path('normcontol/api/v2/',include('knowledge.urls')))
