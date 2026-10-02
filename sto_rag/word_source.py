"""Read-only format boundary. DOC is read by Word, never saved as a DOCX.

The common analyzers consume XML parts, not a converted working file. The
identity and paragraph addresses always belong to the immutable source.
"""
import base64, hashlib, io, threading
from collections import OrderedDict
from pathlib import Path
from zipfile import ZipFile, ZipInfo
from lxml import etree as E

VERSION = 'native-doc-v1'
OLE = bytes.fromhex('d0cf11e0a1b11ae1')
PKG = '{http://schemas.microsoft.com/office/2006/xmlPackage}'
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
reader = None
_cache = OrderedDict()
_lock = threading.RLock()
MAX_PARTS = 150 * 1024**2
MAX_CACHE = 128 * 1024**2

def is_doc(path):
    with open(path, 'rb') as stream: return stream.read(8) == OLE

def checksum(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''): h.update(chunk)
    return h.hexdigest()

def parts(snapshot):
    if snapshot.get('adapter') != VERSION: raise ValueError('DOC adapter version')
    raw = snapshot['flat_xml'].encode('utf-8')
    if len(raw) > 200 * 1024**2 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper(): raise ValueError('Unsafe Word XML')
    root = E.fromstring(raw, E.XMLParser(resolve_entities=False, no_network=True))
    if root.tag != PKG+'package': raise ValueError('Word XML package')
    result = {}; size = 0
    for item in root:
        name = item.get(PKG+'name', '').removeprefix('/')
        if not name or name in result or '..' in name.split('/') or '\\' in name or name.lower().endswith('vbaproject.bin'): raise ValueError('Unsafe Word part')
        data = item.find(PKG+'xmlData'); binary = item.find(PKG+'binaryData')
        if data is not None and len(data) == 1: value = E.tostring(data[0], encoding='UTF-8', xml_declaration=True)
        elif binary is not None: value = base64.b64decode(''.join((binary.text or '').split()), validate=True)
        else: raise ValueError('Word part data')
        size += len(value)
        if len(result) >= 10000 or size > MAX_PARTS: raise ValueError('Word projection exceeds limit')
        result[name] = value
    if 'word/document.xml' not in result: raise ValueError('Word document part absent')
    return result

def projection(path):
    digest = checksum(path)
    with _lock:
        if digest in _cache:
            _cache.move_to_end(digest); return _cache[digest][0]
        if reader is None: raise ValueError('Прямой обработчик DOC не подключён')
        snapshot = reader(Path(path))
        if snapshot.get('source_sha256') != digest or checksum(path) != digest: raise ValueError('DOC source identity changed')
        values = parts(snapshot); size = sum(map(len, values.values()))
        while _cache and sum(v[1] for v in _cache.values()) + size > MAX_CACHE: _cache.popitem(last=False)
        if size <= MAX_CACHE: _cache[digest] = (values, size)
        return values

class PartsArchive:
    def __init__(self, values): self.values = values
    def __enter__(self): return self
    def __exit__(self, *unused): pass
    def namelist(self): return list(self.values)
    def read(self, member): return self.values[member.filename if hasattr(member, 'filename') else member]
    def open(self, member, mode='r'):
        if mode != 'r': raise ValueError('Read-only DOC projection')
        return io.BytesIO(self.read(member))
    def getinfo(self, name):
        info = ZipInfo(name); info.file_size = len(self.values[name]); return info
    def infolist(self): return [self.getinfo(name) for name in self.values]

def open_archive(path, mode='r', *args, **kwargs):
    if mode == 'r' and isinstance(path, (str, Path)) and is_doc(path): return PartsArchive(projection(path))
    return ZipFile(path, mode, *args, **kwargs)

def anchor_map(snapshot, ps, text):
    """Map repeated paragraphs only when occurrence counts agree exactly.

    The main story ordering is authoritative. Text boxes and other unmapped
    stories are not edited by guessing an ordinal.
    """
    from collections import defaultdict
    main=[(i,p) for i,p in enumerate(ps,1) if not any(a.tag==W+'txbxContent' for a in p.iterancestors())]
    if [text(p) for _,p in main]==[row['text'] for row in snapshot['paragraphs']]:
        return {'p'+str(i):row for (i,p),row in zip(main,snapshot['paragraphs'])}
    xml_rows = defaultdict(list); native_rows = defaultdict(list)
    for i, p in main: xml_rows[text(p)].append(i)
    for row in snapshot['paragraphs']: native_rows[row['text']].append(row)
    result = {}
    for value, indices in xml_rows.items():
        rows = native_rows[value]
        # Without a complete order proof, repeated text is ambiguous.
        if len(rows) != 1 or len(indices) != 1: continue
        for i, row in zip(indices, rows): result['p'+str(i)] = row
    return result
