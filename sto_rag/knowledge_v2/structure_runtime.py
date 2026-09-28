"""Explicit reparse of an existing v2 source. Old evidence/snapshots are immutable.

New structural records intentionally use different kinds so the legacy extractor
cannot accidentally combine two parses of the same source. Stage 9.1.3 selects
an explicit parse_ref; changing the active normative release is never done here.
"""
import json
from pathlib import Path
import uuid
from .structure import parse_structure
from .ingest import sha256
from .store import checksum, Conflict


def reparse_source(store,set_id,source_id,*,ocr=True,cancel=lambda:False,structural_client=None,interpret_graphics=False,vision_client=None):
    with store.connection() as db:
        row=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind='source_revision'",(source_id,set_id)).fetchone()
        if not row:raise ValueError('Source does not belong to requested set')
        source=json.loads(row[0])
    original=(store.directory/source['original_key']).resolve()
    if not original.is_relative_to(store.directory.resolve()) or sha256(original)!=source['sha256']:
        raise Conflict('Source provenance mismatch')
    result,run=parse_structure(original,store.directory/'structure-runs',ocr=ocr,cancel=cancel,
        structural_client=structural_client,interpret_graphics=interpret_graphics,vision_client=vision_client)
    pid=str(uuid.uuid5(uuid.UUID(source_id),'parse:'+result['run_id']));ref=[pid,1]
    artifact=str((run/'result.json').relative_to(store.directory)).replace('\\','/')
    entries=[(set_id,'parse_run',pid,1,dict(source_revision=[source_id,1],parser_version=result['parser_version'],
        identity=result['identity'],manifest_digest=result['result_digest'],artifact_key=artifact,summary=result['summary']))]
    for t in result['tables']:
        tid=str(uuid.uuid5(uuid.UUID(pid),'table:'+t['id']))
        entries.append((set_id,'table_structure',tid,1,dict(parse_ref=ref,table=t)))
    for b in result['blocks']:
        fid=str(uuid.uuid5(uuid.UUID(pid),'fragment:'+b['locator']))
        entries.append((set_id,'structured_fragment',fid,1,dict(parse_ref=ref,source_revision=[source_id,1],
            locator=b['locator'],exact_text=b['exact_text'],context_hash=b['context_hash'],
            structure={k:v for k,v in b.items() if k not in ('locator','exact_text','context_hash')})))
    store.put_records_batch(entries)
    return dict(kind='source.structured',source_id=source_id,set_id=set_id,parse_ref=ref,
                manifest_digest=result['result_digest'],artifact_key=artifact,summary=result['summary'])
