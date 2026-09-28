import uuid
from django.conf import settings
from django.db import models


class Scope(models.Model):
    KINDS = [('personal','Личная'),('project','Проект'),('team','Команда'),('organization','Организация')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=16, choices=KINDS)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT)
    acl_revision = models.PositiveBigIntegerField(default=1)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner'], condition=models.Q(kind='personal'), name='knowledge_personal_owner')]


class Membership(models.Model):
    ROLES = [(x,x) for x in ('reader','contributor','reviewer','curator','manager')]
    scope = models.ForeignKey(Scope, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=ROLES)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['scope','user'], name='knowledge_scope_user')]


class NormativeSet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.ForeignKey(Scope, on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    purpose = models.CharField(max_length=16, choices=[('normative','Нормативы'),('experience','Опыт')], default='normative')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    metadata_revision = models.PositiveBigIntegerField(default=1)
    state = models.CharField(max_length=16, default='empty')
    active_release = models.ForeignKey('Release', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created = models.DateTimeField(auto_now_add=True)
    description = models.TextField(blank=True)
    automatic = models.BooleanField(default=False)


class Release(models.Model):
    """Read-only projection of the canonical manifest, updated only via worker events."""
    id = models.UUIDField(primary_key=True, editable=False)
    normative_set = models.ForeignKey(NormativeSet, on_delete=models.PROTECT, related_name='releases')
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)
    attestation = models.JSONField()
    state = models.CharField(max_length=16, default='ready')
    created = models.DateTimeField(auto_now_add=True)


class Snapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_id = models.UUIDField(unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    data = models.JSONField()
    digest = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)


class Command(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    normative_set = models.ForeignKey(NormativeSet, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=32)
    idempotency_key = models.CharField(max_length=160, unique=True)
    payload = models.JSONField()
    digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default='pending', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    lease = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    worker_id = models.CharField(max_length=80, blank=True)
    result = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)

class Receipt(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    command = models.ForeignKey(Command, on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    result = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=48)
    aggregate_id = models.CharField(max_length=80)
    data = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)


class SourceUpload(models.Model):
    """Portal-side opaque original; parsing and normative content stay local."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    normative_set = models.ForeignKey(NormativeSet, on_delete=models.PROTECT, related_name='sources')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    filename = models.CharField(max_length=240)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    storage_key = models.CharField(max_length=160)
    identification = models.JSONField(default=dict)
    identification_revision = models.PositiveIntegerField(default=1)

    @property
    def display_name(self):
        fields=self.identification.get('fields',{})
        title=fields.get('short_title',{}).get('value','').strip()
        date=fields.get('approval_date',{}).get('value','').strip()
        return (title+(' · утв. '+date if date else '')) if title else self.filename

    supersedes = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default='queued')
    result = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['normative_set', 'sha256'], name='knowledge_source_set_sha')]


class CoverageChunk(models.Model):
    source = models.ForeignKey(SourceUpload, on_delete=models.PROTECT, related_name='coverage_chunks')
    sequence = models.PositiveIntegerField()
    entries = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'sequence'], name='knowledge_source_coverage_chunk')]


class AnalysisChunk(models.Model):
    command = models.ForeignKey(Command, on_delete=models.PROTECT, related_name='analysis_chunks')
    sequence = models.PositiveIntegerField()
    entries = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['command', 'sequence'], name='knowledge_analysis_chunk')]


class DocumentProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.ForeignKey(Scope, on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    revision = models.PositiveIntegerField(default=1)
    definition = models.JSONField()
    updated = models.DateTimeField(auto_now=True)
    normative_set = models.ForeignKey(NormativeSet, null=True, blank=True, on_delete=models.PROTECT, related_name='profiles')
    archived = models.BooleanField(default=False)


class ProfileRevision(models.Model):
    profile = models.ForeignKey(DocumentProfile, on_delete=models.PROTECT, related_name='revisions')
    revision = models.PositiveIntegerField()
    definition = models.JSONField()
    digest = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['profile', 'revision'], name='knowledge_profile_revision')]


class ExperienceReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    normative_set = models.ForeignKey(NormativeSet, on_delete=models.PROTECT, related_name='experience_reviews')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    submission = models.JSONField()
    state = models.CharField(max_length=32, default='submitted')
    revision = models.PositiveIntegerField(default=1)
    result = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)


class KnowledgeCheck(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey('portal.Batch', on_delete=models.PROTECT, related_name='knowledge_checks')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    snapshot = models.ForeignKey(Snapshot, on_delete=models.PROTECT)
    experience_release = models.ForeignKey(Release, null=True, blank=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=20, default='queued')
    pause_requested = models.BooleanField(default=False)
    progress = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)


class KnowledgeFindingChunk(models.Model):
    job = models.ForeignKey(KnowledgeCheck, on_delete=models.PROTECT, related_name='finding_chunks')
    sequence = models.PositiveIntegerField()
    entries = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints=[models.UniqueConstraint(fields=['job','sequence'],name='knowledge_check_finding_chunk')]


class CheckLogChunk(models.Model):
    job = models.ForeignKey(KnowledgeCheck, on_delete=models.PROTECT, related_name='log_chunks')
    sequence = models.PositiveIntegerField()
    entries = models.JSONField()
    digest = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['job','sequence'],name='knowledge_check_log_chunk')]


class ExpertCard(models.Model):
    """Indexed UI projection. Canonical expert versions remain on the local worker."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(SourceUpload, on_delete=models.PROTECT)
    analysis = models.ForeignKey(Command, on_delete=models.PROTECT, related_name='expert_cards')
    base_id = models.UUIDField()
    revision = models.PositiveIntegerField(default=1)
    payload = models.JSONField()
    contexts = models.JSONField(default=list)
    description = models.TextField()
    entity_type = models.CharField(max_length=24, db_index=True)
    profile_key = models.CharField(max_length=80, db_index=True)
    section = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=24, default='unreviewed', db_index=True)
    latest_analysis = models.BooleanField(default=True, db_index=True)
    pending = models.ForeignKey(Command, null=True, on_delete=models.PROTECT, related_name='+')
    updated = models.DateTimeField(auto_now=True)
    class Meta:
        indexes = [models.Index(fields=['source','latest_analysis','status'], name='expert_source_state')]


class ExpertCardRevision(models.Model):
    card = models.ForeignKey(ExpertCard, on_delete=models.PROTECT, related_name='history')
    revision = models.PositiveIntegerField()
    payload = models.JSONField()
    digest = models.CharField(max_length=64)
    action = models.CharField(max_length=24)
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['card','revision'], name='expert_card_revision')]


class GlossaryGroup(models.Model):
    normative_set = models.ForeignKey(NormativeSet,on_delete=models.PROTECT,related_name='glossary_groups')
    key = models.CharField(max_length=520)
    active = models.ForeignKey(ExpertCard,null=True,on_delete=models.PROTECT,related_name='+')
    revision = models.PositiveIntegerField(default=1)
    expert_selected = models.BooleanField(default=False)
    conflict = models.BooleanField(default=False)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['normative_set','key'],name='knowledge_glossary_key')]


class GlossaryVariant(models.Model):
    card = models.OneToOneField(ExpertCard,on_delete=models.PROTECT,related_name='glossary_variant')
    group = models.ForeignKey(GlossaryGroup,on_delete=models.PROTECT,related_name='variants')
    term = models.CharField(max_length=500)
    kind = models.CharField(max_length=16)


class NormativeLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    normative_set = models.ForeignKey(NormativeSet, on_delete=models.PROTECT, related_name='trace_links')
    revision = models.PositiveIntegerField(default=1)
    payload = models.JSONField()
    updated = models.DateTimeField(auto_now=True)

class NormativeLinkRevision(models.Model):
    link = models.ForeignKey(NormativeLink, on_delete=models.PROTECT, related_name='history')
    revision = models.PositiveIntegerField()
    payload = models.JSONField()
    digest = models.CharField(max_length=64)
    request_key = models.CharField(max_length=64,unique=True)
    intent_digest = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['link','revision'],name='trace_link_revision')]


class ObjectControl(models.Model):
    """Expert lifecycle overlay; immutable releases and original evidence are retained."""
    normative_set = models.ForeignKey(NormativeSet, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    object_id = models.UUIDField()
    enabled = models.BooleanField(default=True)
    deleted = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['kind','object_id'],name='knowledge_object_control')]
