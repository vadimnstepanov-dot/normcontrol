"""Freeze editor selections; the local worker validates their canonical versions."""
from django.db import transaction
from django.utils import timezone
from knowledge_v2.curation import VERSION,fingerprint,context_digest,profile_graph
from .models import ExpertCard,DocumentProfile,SourceUpload,Command,AnalysisChunk
from .profiles import snapshot_profiles
from .services import digest,Conflict,NotReady
from .access import require


def inventory(user,dataset):
    profiles=snapshot_profiles(user,list(DocumentProfile.objects.filter(scope=dataset.scope).values_list('id',flat=True)))
    rows=list(ExpertCard.objects.filter(source__normative_set__scope=dataset.scope,latest_analysis=True).select_related('source'))
    items=[fingerprint(str(c.pk),str(c.source_id),c.profile_key,c.payload,c.status) for c in rows]
    return profiles,rows,items


def current_context(user,card):
    profiles,rows,items=inventory(user,card.source.normative_set)
    item=next((x for x in items if x['id']==str(card.pk)),fingerprint(str(card.pk),str(card.source_id),card.profile_key,card.payload,card.status))
    return context_digest(item,profiles,items)


def selection(user,dataset,source_ids):
    profiles,rows,items=inventory(user,dataset);selected=set(map(str,source_ids))
    selected_rows=[c for c in rows if str(c.source_id) in selected]
    if not selected_rows:raise NotReady('No accepted cards')
    if any(c.pending_id and c.pending.state in ('pending','delivering') for c in selected_rows):
        raise Conflict('Wait for expert decisions to finish')
    families={}
    for source in SourceUpload.objects.filter(pk__in=selected,normative_set=dataset):
        family=source;seen=set()
        while family.supersedes_id:
            if family.pk in seen:raise Conflict('Source revision cycle')
            seen.add(family.pk);family=family.supersedes
            if family.normative_set_id!=dataset.pk:raise Conflict('Source family ownership')
        families[str(source.pk)]=str(family.pk)
    if len(set(families.values()))!=len(families):
        raise Conflict('Choose one revision of each normative source')
    # Include only profiles bound to selected material and their full connected DAG.
    index,closure=profile_graph(profiles)
    owners={p['id'] for p in profiles if any(b['source_id'] in selected for b in p['definition']['bindings'])}
    related=set().union(*(closure[p] for p in index if closure[p]&owners)) if owners else set()
    from .trace_links import frozen
    links=frozen(dataset,source_ids)
    return dict(**({'links':links} if links else {}),version=VERSION,profiles=[p for p in profiles if p['id'] in related],
        context_items=sorted(items,key=lambda x:x['id']),families=families,
        cards=[dict(id=str(c.pk),source_id=str(c.source_id),base_id=str(c.base_id),revision=c.revision,status=c.status,
            digest=digest(c.payload),derived=c.history.filter(action__in=['split','merge','refine']).exists())
               for c in sorted(selected_rows,key=lambda x:str(x.pk))])


@transaction.atomic
def add_material(worker_id,command_id,lease,sequence,entries,entry_hash):
    if type(sequence) is not int or sequence<0 or not isinstance(entries,list) or not 1<=len(entries)<=100:
        raise ValueError('Release material chunk')
    if digest(entries)!=entry_hash or any(x.get('kind') not in ('requirement','diff') for x in entries):raise ValueError('Material checksum/kind')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='release.prepare')
    if c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Stale preparation')
    require(c.actor,c.normative_set.scope,'upload')
    row,new=AnalysisChunk.objects.get_or_create(command=c,sequence=sequence,defaults=dict(entries=entries,digest=entry_hash))
    if not new and row.digest!=entry_hash:raise Conflict('Material changed on retry')
    return row


def release_command(release):
    return Command.objects.filter(normative_set=release.normative_set,kind='release.prepare',state='done',payload__release_id=str(release.pk)).first()


def material_page(release,kind,page=1,size=25,quality='',profile=''):
    if not 1<=page<=100000:raise ValueError('Page')
    if quality not in ('','ready','candidate','reference','duplicate'):raise ValueError('Quality filter')
    command=release_command(release)
    if not command:return dict(entries=[],total=0,pages=1,page=page)
    total=0;entries=[];start=(page-1)*size
    for chunk in command.analysis_chunks.order_by('sequence').iterator():
        for row in chunk.entries:
            if row.get('kind')!=kind:continue
            if quality and (row.get('quality') or {}).get('status')!=quality:continue
            if profile and profile not in row.get('profiles',[]):continue
            if start<=total<start+size:entries.append(row)
            total+=1
    return dict(entries=entries,total=total,pages=max(1,(total+size-1)//size),page=page)


def summary(release):
    c=release_command(release)
    return c.result.get('trust_summary',{}) if c else {}


def release_catalog(release):
    c=release_command(release)
    return [row for chunk in c.analysis_chunks.order_by('sequence').iterator() for row in chunk.entries
            if row.get('kind')=='requirement'] if c else []


@transaction.atomic
def fail_preparation(worker_id,command_id,lease,reason,permanent):
    if type(permanent) is not bool:raise ValueError('Permanent flag')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='release.prepare')
    if c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Stale preparation')
    require(c.actor,c.normative_set.scope,'upload')
    c.state='failed' if permanent or c.attempts>=c.max_attempts else 'pending'
    c.result={'reason':reason};c.lease=None;c.lease_until=None
    c.save(update_fields=['state','result','lease','lease_until'])
    return dict(accepted=True,state=c.state)
