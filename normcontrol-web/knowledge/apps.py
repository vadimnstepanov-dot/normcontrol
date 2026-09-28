from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    name = 'knowledge'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        import sys
        from pathlib import Path
        local=Path(__file__).resolve().parents[2]/'sto_rag'
        if local.is_dir() and str(local) not in sys.path:sys.path.insert(0,str(local))
