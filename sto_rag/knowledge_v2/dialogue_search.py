"""Read-only dialogue retrieval resolved from a published canonical release."""
import json
from .search import HybridSearch
from .store import Conflict

VERSION='dialogue-rag-v1'
KINDS=('requirement','term_definition','fragment','structured_fragment','review_case','clarification')

def reference(store,encoder,vector,authorize,payload):
    query=payload['query'];limit=payload.get('limit',6)
    if not isinstance(query,str) or not 1<=len(query.strip())<=1200 or type(limit) is not int or not 1<=limit<=8:
        raise ValueError('Dialogue search budget')
    found=HybridSearch(store,encoder,vector,authorize).reference(payload['release_id'],query,kinds=KINDS,limit=limit)
    # Search already verifies each canonical quote and its dependencies. Add
    # frozen trust and exact source identity, never the latest editor projection.
    with store.connection() as db:
        manifest=json.loads(db.execute('SELECT manifest FROM releases WHERE id=?',(payload['release_id'],)).fetchone()[0])
        if manifest['set_id']!=payload['set_id']:raise Conflict('Dialogue search set')
        refs={(x['id'],x['version']) for x in manifest['items']}
        policy=next((x for x in manifest['items'] if x['kind']=='publication_policy'),None)
        catalog={}
        if policy:
            frozen=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',(policy['id'],policy['version'])).fetchone()[0])
            catalog={tuple(x['ref']):x for x in frozen.get('catalog',[])}
        for row in found:
            value=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',(row['record_id'],row['version'])).fetchone()[0])
            # Imported cards can have several source citations and no single
            # card.locator. Recover addresses only from verified exact context;
            # this presentation adapter does not change review selection.
            if not row.get('locator'):
                locations=[]
                for citation in value.get('card',{}).get('citations',[]):
                    locator=citation.get('locator');quote=citation.get('quote')
                    if locator and quote and any(x.get('locator')==locator and quote in x.get('exact_text','') for x in row.get('context',[])):
                        if locator not in locations:locations.append(locator)
                row['locator']='; '.join(locations[:3])
            origin=value.get('source_revision')
            row['source_id']=origin[0] if origin else None
            row['source_name']=''
            if origin:
                if tuple(origin) not in refs:raise Conflict('Dialogue source outside release')
                source=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',origin).fetchone()[0])
                row['source_name']=source.get('filename','')
            material=catalog.get((row['record_id'],row['version']),{})
            row['quality']=material.get('quality') or value.get('card',{}).get('quality') or {}
            row['trust']=material.get('trust') or {}
            row['material_type']='experience' if row['kind'] in ('review_case','clarification') else 'requirement' if material else 'source_excerpt'
    if not authorize(payload['set_id']):raise PermissionError('Dialogue read grant revoked')
    return found
