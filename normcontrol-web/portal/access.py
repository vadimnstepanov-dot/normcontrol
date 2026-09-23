from django.shortcuts import get_object_or_404

from .models import AccessProfile, Batch


def visible_batches(user):
    if user.is_staff or AccessProfile.objects.filter(user=user, can_view_others=True).exists():
        return Batch.objects.all()
    return Batch.objects.filter(owner=user)


def editable_batches(user):
    return Batch.objects.all() if user.is_staff else Batch.objects.filter(owner=user)


def visible_batch(user, pk):
    return get_object_or_404(visible_batches(user), pk=pk)


def editable_batch(user, pk):
    return get_object_or_404(editable_batches(user), pk=pk)


def can_edit(user, batch):
    return user.is_staff or batch.owner_id == user.pk
