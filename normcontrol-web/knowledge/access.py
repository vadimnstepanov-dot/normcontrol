from django.core.exceptions import PermissionDenied
from .models import Membership, NormativeSet

ROLE_PERMISSIONS = {
    'reader': {'read'},
    'contributor': {'read','upload'},
    'reviewer': {'read','review'},
    'curator': {'read','upload','review','publish'},
    'manager': {'read','upload','review','publish','manage'},
}


def is_expert(user):
    return bool(user and user.is_authenticated and user.is_active
                and getattr(getattr(user,'accessprofile',None),'is_expert',False))


def allowed(user, scope, action):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_staff:
        return True
    # Existing normative areas may live in the uploader's personal scope.
    # The expert role covers normative-base work there too, but not empty
    # private/experience-only scopes or scope membership administration.
    if is_expert(user) and action in ROLE_PERMISSIONS['curator']:
        if scope.kind!='personal' or NormativeSet.objects.filter(scope=scope,purpose='normative').exists():
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
