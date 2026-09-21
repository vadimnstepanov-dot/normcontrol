"""Local conversion of legacy binary Word documents into inspected DOCX."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from .common import DATA,ROOT
from .documents import inspect_file

OLE_SIGNATURE=b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'

def inspect_legacy(path,max_file=50*1024**2):
    path=Path(path).resolve(strict=True)
    if path.suffix.casefold()!='.doc':raise ValueError('Ожидался документ Word .doc')
    if path.stat().st_size>max_file:raise ValueError('Превышен лимит размера файла')
    with path.open('rb') as source:
        if source.read(8)!=OLE_SIGNATURE:raise ValueError('Файл не является корректным двоичным документом Word .doc')
    return path

def checksum(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()

def prepare_word(path,cfg=None):
    """Return an inspected DOCX path, caching conversion by source checksum."""
    cfg=cfg or {};path=Path(path).resolve(strict=True);suffix=path.suffix.casefold()
    if suffix=='.docx':return inspect_file(path,cfg.get('max_file_bytes',50*1024**2),cfg.get('max_unpacked_bytes',200*1024**2))
    inspect_legacy(path,cfg.get('max_file_bytes',50*1024**2))
    folder=DATA/'conversions';folder.mkdir(parents=True,exist_ok=True);source_hash=checksum(path)
    target=folder/(source_hash+'.docx')
    if target.exists():
        try:return inspect_file(target,cfg.get('max_file_bytes',50*1024**2),cfg.get('max_unpacked_bytes',200*1024**2))
        except (ValueError,OSError):target.unlink(missing_ok=True)
    temporary=target.with_name(target.stem+'.tmp.docx');temporary.unlink(missing_ok=True)
    # Windows PowerShell 5 can corrupt non-ASCII command-line paths under a
    # non-system Python runtime.  Stage the immutable input under an ASCII hash.
    staged=folder/(source_hash+'.source.doc');shutil.copyfile(path,staged)
    script=ROOT/'sto_rag/nc5/word_export.ps1'
    command=['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-Source',str(staged),'-Destination',str(temporary),'-Format','docx']
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    try:
        subprocess.run(command,check=True,timeout=180,capture_output=True,creationflags=flags)
        if checksum(path)!=source_hash:raise ValueError('Исходный документ изменился во время преобразования')
        inspect_file(temporary,cfg.get('max_file_bytes',50*1024**2),cfg.get('max_unpacked_bytes',200*1024**2))
        os.replace(temporary,target)
    except (subprocess.SubprocessError,OSError,ValueError) as error:
        temporary.unlink(missing_ok=True)
        raise ValueError('Не удалось безопасно преобразовать документ .doc в .docx. Проверьте, что файл открывается в Microsoft Word и не защищён паролем.') from error
    finally:staged.unlink(missing_ok=True)
    return inspect_file(target,cfg.get('max_file_bytes',50*1024**2),cfg.get('max_unpacked_bytes',200*1024**2))
