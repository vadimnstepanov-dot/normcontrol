"""Operator-owned planned work notice; worker transport remains available."""
import json
from pathlib import Path
from django.conf import settings
from django.http import HttpResponse
from django.utils.html import escape

def notice_html(value):
    message=escape(value.get('message','Обновляем сервис NormControl.'))
    end=escape(value.get('planned_end_label','Время окончания уточняется.'))
    duration=escape(value.get('minutes','—'))
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30"><title>NormControl — технические работы</title><link rel="stylesheet" href="/normcontol/static/maintenance.css"></head><body><main><div class="brand">NormControl</div><h1>Идут технические работы</h1><p>{message}</p><p>Плановая длительность: <strong>{duration} мин</strong>.<br>Ожидаемое окончание: <strong>{end}</strong>.</p><p>Прогресс проверок и результаты сохранены. Проверки продолжатся после завершения работ.</p><small>Страница обновляется каждые 30 секунд.</small></main></body></html>'''

class MaintenanceMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        if not request.path.startswith('/normcontol/') or request.path.startswith(('/normcontol/worker/','/normcontol/knowledge/worker/','/normcontol/static/','/normcontol/health')):return self.get_response(request)
        marker=Path(settings.DATA_DIR)/'maintenance.json'
        try:
            if marker.stat().st_size>8192:raise ValueError('Notice size')
            value=json.loads(marker.read_text(encoding='utf-8'))
            if not isinstance(value,dict):raise ValueError('Notice format')
        except (FileNotFoundError,ValueError,OSError):return self.get_response(request)
        if not value.get('active'):return self.get_response(request)
        response=HttpResponse(notice_html(value),status=503)
        response['Retry-After']='30';response['Cache-Control']='no-store'
        return response
