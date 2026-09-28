"""Fresh portal ACL check for every normative quote returned by the worker."""
from django.contrib.auth import get_user_model
from .access import allowed
from .models import NormativeSet


def reader_for_user(user_id):
    def authorize(set_id):
        user=get_user_model().objects.filter(pk=user_id,is_active=True).first()
        if not user:return False
        dataset=NormativeSet.objects.select_related('scope').filter(pk=set_id).first()
        return bool(dataset and allowed(user,dataset.scope,'read'))
    return authorize
