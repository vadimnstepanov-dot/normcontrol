from django.core.exceptions import PermissionDenied
from .models import Membership

ROLE_PERMISSIONS = {
    'reader': {'read'},
    'contributor': {'read','upload'},
    'reviewer': {'read','review'},
    'curator': {'read','upload','review','publish'},
    'manager': {'read','upload','review','publish','manage'},
}


def allowed(user, scope, action):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_staff:
        return True
    visited=set()
    while scope:
        if scope.pk in visited:
            return False
        visited.add(scope.pk)
        if scope.owner_id==user.pk:
            return True
        role=Membership.objects.filter(scope=scope,user=user).values_list('role',flat=True).first()
        if action in ROLE_PERMISSIONS.get(role,set()):
            return True
        scope=scope.parent
    return False


def require(user, scope, action):
    if not allowed(user,scope,action):
        raise PermissionDenied('Нет доступа к нормативной области')
