"""Deduplicate trace normative citations; canonical pins remain in the saved plan."""
import copy
from .store import checksum

POLICY=('В transport нормативные цитаты связи вынесены в trace_citations. '
    'link.basis_citation_refs и normative_basis[].citation_refs раскрываются по этому справочнику; '
    'все цитаты и их locator сохранены дословно и обязательны к рассмотрению. '
    'source_ref раскрывается в sources; document_name_ref — точное имя документа из document_names. '
    'Служебные хеши и смещения проверяются программой по исходному закреплённому плану; '
    'отсутствие их повторов не означает отсутствия нормативного основания. Цитаты нормативной базы не являются доказательством исполнения в documents.')

def compact_links(rows,sources):
    pool={};keys={}
    def refs(citations):
        result=[]
        for original in citations:
            c=copy.deepcopy(original)
            for key in ('context_hash','start','end'):c.pop(key,None)
            if 'source_sha256' in c:c['source_ref']=sources.add(c.pop('source_sha256'))
            key=checksum(c)
            if key not in keys:
                name='TC'+str(len(pool)+1);keys[key]=name;pool[name]=c
            result.append(keys[key])
        return result
    for row in rows:
        link=row.get('link')
        if not isinstance(link,dict):continue
        if 'basis_citations' in link:link['basis_citation_refs']=refs(link.pop('basis_citations'))
        for basis in link.get('normative_basis',[]):
            if 'citations' in basis:basis['citation_refs']=refs(basis.pop('citations'))
    return pool

def compact_document_names(structures):
    names={};keys={}
    for structure in structures.values():
        if not isinstance(structure.get('document_name'),str):continue
        if 'document_name_ref' in structure:raise ValueError('Reserved document_name_ref already exists')
        name=structure.pop('document_name')
        if name not in keys:
            ref='DN'+str(len(names)+1);keys[name]=ref;names[ref]=name
        structure['document_name_ref']=keys[name]
    return names
