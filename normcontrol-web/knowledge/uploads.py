"""Authenticated, streaming intake of normative originals on the portal."""
import hashlib
import os
from pathlib import Path
import tempfile
import uuid
from zipfile import ZipFile, BadZipFile

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from .access import require
from .models import NormativeSet, SourceUpload
from .services import command, audit, Conflict

MAX_FILE=50*1024*1024
MAX_GROUP=20
MAX_GROUP_BYTES=200*1024*1024
OLE=b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


def incoming_directory():
    path=Path(settings.DATA_DIR)/'knowledge-v2-inbox'
    path.mkdir(parents=True,exist_ok=True)
    return path


def _stage(file):
    name=file.name
    if not isinstance(name,str) or not name or len(name)>240 or '/' in name or '\\' in name or '\x00' in name:
        raise ValueError('Unsafe filename')
    kind=Path(name).suffix.casefold()
    if kind not in ('.docx','.doc','.pdf'):raise ValueError('Unsupported format')
    h=hashlib.sha256();size=0
    with tempfile.NamedTemporaryFile(dir=incoming_directory(),prefix='.upload-',delete=False) as out:
        path=Path(out.name)
        try:
            for chunk in file.chunks(1024*1024):
                size+=len(chunk)
                if size>MAX_FILE:raise ValueError('File too large')
                out.write(chunk);h.update(chunk)
            out.flush();os.fsync(out.fileno())
        except BaseException:path.unlink(missing_ok=True);raise
    try:
        if not size:raise ValueError('Empty file')
        with path.open('rb') as source:signature=source.read(8)
        if kind=='.pdf' and not signature.startswith(b'%PDF-'):raise ValueError('PDF signature')
        if kind=='.doc' and signature!=OLE:raise ValueError('DOC signature')
        if kind=='.docx':
            if not signature.startswith(b'PK'):raise ValueError('DOCX signature')
            try:
                with ZipFile(path) as archive:
                    entries=archive.infolist()
                    if (len(entries)>10000 or sum(x.file_size for x in entries)>200*1024*1024
                        or 'word/document.xml' not in archive.namelist()):raise ValueError('DOCX structure')
                    for info in entries:
                        if (info.filename.startswith('/') or '\\' in info.filename or '..' in info.filename.split('/')
                            or info.filename.lower().endswith('vbaproject.bin') or info.flag_bits & 1):
                            raise ValueError('Unsafe DOCX member')
                        if info.filename.endswith(('.xml','.rels')):
                            tail=b''
                            with archive.open(info) as member:
                                for chunk in iter(lambda:member.read(1024*1024),b''):
                                    scanned=(tail+chunk).upper()
                                    if b'<!DOCTYPE' in scanned or b'<!ENTITY' in scanned:raise ValueError('XML entity')
                                    tail=scanned[-16:]
            except BadZipFile as exc:raise ValueError('Invalid DOCX') from exc
        return dict(path=path,name=name,sha=h.hexdigest(),size=size)
    except BaseException:path.unlink(missing_ok=True);raise


def upload(user,set_id,files,supersedes=None):
    if not 1<=len(files)<=MAX_GROUP:raise ValueError('Upload 1–20 documents')
    dataset=NormativeSet.objects.select_related('scope').get(pk=set_id)
    if dataset.purpose!='normative':raise ValueError('Upload normative sources to a normative set')
    if dataset.state=='archived':raise ValueError('Restore archived set before upload')
    require(user,dataset.scope,'upload')
    if supersedes and len(files)!=1:raise ValueError('A revision replaces one source at a time')
    prior=None
    if supersedes:
        prior=SourceUpload.objects.get(pk=supersedes,normative_set=dataset)
        if prior.state not in ('prepared','partial'):raise ValueError('Prior source is not prepared')
    staged=[];created=[]
    try:
        for file in files:
            staged.append(_stage(file))
            if sum(item['size'] for item in staged)>MAX_GROUP_BYTES:raise ValueError('Upload group too large')
        with transaction.atomic():
            dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
            require(user,dataset.scope,'upload')
            if dataset.state=='archived':raise ValueError('Restore archived set before upload')
            rows=[]
            for item in staged:
                old=SourceUpload.objects.filter(normative_set=dataset,sha256=item['sha']).first()
                if old:
                    if prior and old.supersedes_id!=prior.pk:raise Conflict('Source already uploaded with another revision relation')
                    rows.append((old,True));continue
                sid=uuid.uuid4();key=f'{sid.hex}{Path(item["name"]).suffix.casefold()}'
                destination=incoming_directory()/key
                os.replace(item['path'],destination);created.append(destination)
                source=SourceUpload.objects.create(id=sid,normative_set=dataset,actor=user,filename=item['name'],
                    sha256=item['sha'],size=item['size'],storage_key=key,supersedes=prior)
                payload=dict(set_id=str(dataset.pk),source_id=str(sid),sha256=item['sha'],filename=item['name'],
                             size=item['size'],supersedes=str(prior.pk) if prior else None,parser_version='structure-v2.2')
                command(user,dataset,'source.ingest',hashlib.sha256(('source.ingest:'+str(sid)).encode()).hexdigest(),payload)
                audit(user,'source.uploaded',sid,{'sha256':item['sha'],'size':item['size']})
                rows.append((source,False))
        return rows
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        raise
    finally:
        for item in staged:item['path'].unlink(missing_ok=True)
