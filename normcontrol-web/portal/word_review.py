"""Immutable OOXML review plans. No LLM calls or free-text suggestion replacement."""
import copy,hashlib,json,re,posixpath
from datetime import datetime,timezone
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from lxml import etree as E

VERSION='word-review-v4'
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main';Q='{'+W+'}'
NS={'w':W};R='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
XML='{http://www.w3.org/XML/1998/namespace}'
class ReviewError(ValueError):pass
def sha(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for chunk in iter(lambda:f.read(65536),b''):h.update(chunk)
 return h.hexdigest()
def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def xml(raw):
 if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ReviewError('DTD и ENTITY запрещены')
 return E.fromstring(raw,E.XMLParser(resolve_entities=False,no_network=True,remove_blank_text=False))
def dump(root):return E.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)
def text(p):
 def walk(n):
  if n.tag in (Q+'del',Q+'moveFrom') or n is not p and n.tag==Q+'p':return ''
  if n.tag==Q+'t':return n.text or ''
  if n.tag in (Q+'tab',Q+'br',Q+'cr'):return '\t' if n.tag==Q+'tab' else '\n'
  return ''.join(walk(c) for c in n)
 return walk(p)
def load(path):
 if Path(path).stat().st_size>50*1024**2:raise ReviewError('DOCX превышает 50 МиБ')
 with ZipFile(path) as z:
  entries=z.infolist();names=[i.filename for i in entries]
  if len(entries)>10000 or sum(i.file_size for i in entries)>200*1024**2:raise ReviewError('Превышен лимит распаковки')
  if len(names)!=len(set(names)):raise ReviewError('Дубли частей OOXML')
  for i in entries:
   if i.filename.startswith('/') or '..' in i.filename.split('/') or i.filename.lower().endswith('vbaproject.bin'):raise ReviewError('Небезопасный пакет Word')
   if i.filename.endswith(('.xml','.rels')):
    if i.file_size>20*1024**2:raise ReviewError('XML-часть превышает 20 МиБ: '+i.filename)
    xml(z.read(i))
  if 'word/document.xml' not in names:raise ReviewError('Нужна рабочая копия DOCX; переименование DOC недопустимо')
  return xml(z.read('word/document.xml'))
def paragraphs(root):return root.xpath('.//w:body//w:p',namespaces=NS)
def normalize_map(value):
 result=[];mapping=[]
 for match in re.finditer(r'\s+|\S',value):
  result.append(' ' if match[0].isspace() else match[0]);mapping.append((match.start(),match.end()))
 return ''.join(result),mapping
def locate(p,e):
 value=text(p);quote=e.get('quote','')
 if not quote:return None,'Нет точной цитаты; примечание привязано к исходному абзацу'
 # Older evidence uses str.index even for duplicate quotes. Do not trust it.
 matches=list(re.finditer(re.escape(quote),value))
 if len(matches)==1:return (matches[0].start(),matches[0].end()),''
 if len(matches)>1:return None,'Повторяющаяся цитата: точный диапазон неоднозначен'
 normalized,back=normalize_map(value);needle=normalize_map(quote)[0].strip()
 matches=list(re.finditer(re.escape(needle),normalized)) if needle else []
 if len(matches)==1:
  m=matches[0];return (back[m.start()][0],back[m.end()-1][1]),'Пробелы сопоставлены с исходными символами'
 return None,'Цитата не подтверждена в указанном абзаце'
def safe_range(p,start,end):
 # Fields can span runs and paragraphs; paragraph-level field exclusion is conservative.
 if p.xpath('.//w:fldChar|.//w:fldSimple|.//w:instrText',namespaces=NS):return False
 depth=0
 for fld in p.xpath('preceding::w:fldChar',namespaces=NS):
  if fld.get(Q+'fldCharType')=='begin':depth+=1
  elif fld.get(Q+'fldCharType')=='end':depth=max(0,depth-1)
 if depth:return False
 offset=0;covered=0
 for n in p.iter():
  if n.tag not in (Q+'t',Q+'tab',Q+'br',Q+'cr'):continue
  if any(a.tag in (Q+'del',Q+'moveFrom') for a in n.iterancestors()):continue
  s=n.text or '' if n.tag==Q+'t' else ('\t' if n.tag==Q+'tab' else '\n')
  a,b=offset,offset+len(s);offset=b
  if max(a,start)<min(b,end) or start==end and a<=start<=b:
   r=n.getparent()
   if r.tag!=Q+'r' or r.getparent() is not p:return False
   if any(c.tag not in (Q+'rPr',Q+'t',Q+'tab',Q+'br',Q+'cr') for c in r):return False
   covered+=max(0,min(b,end)-max(a,start))
 return covered==end-start
def comment_message(f,reason,reference_evidence):
 labels={'confirmed':'Подтверждено','question':'Вопрос','candidate':'Кандидат','verifying':'На перепроверке','style':'Редакторское предложение'}
 suggestion=str(f.get('suggestion') or 'Уточнить содержание и зафиксировать решение специалиста.')
 # A prior review may describe a completed edit; here it is still a proposal.
 if suggestion.startswith('Заменено:'):suggestion='Заменить:'+suggestion[len('Заменено:'):]
 lines=[labels.get(f.get('status'),str(f.get('status','')))+' | '+str(f.get('issue','')),
  str(f.get('explanation','')),'Предложение: '+suggestion]
 # Routine export diagnostics belong to the application report, not balloons.
 if reason and reason!='Нет проверенной структурированной редакции; свободное предложение не применяется' and reason!='Пробелы сопоставлены с исходными символами':lines.append('Ограничение: '+reason)
 if f.get('status')=='style':lines.append('Необязательное редакторское предложение.')
 if f.get('status') in ('candidate','verifying'):lines.append('Предварительная гипотеза; не подтверждённое нарушение.')
 source=f.get('source') or {}
 if source.get('validation_status')=='confirmed' and source.get('source_sha256') and source.get('source_quote'):
  lines.append('Основание: '+str(source.get('document_name',''))+'; '+str(source.get('clause',''))+'; «'+source['source_quote'][:700]+'»')
 elif source.get('source_quote') or source.get('quote'):
  lines.append('Основание: '+str(source.get('document_name') or source.get('name',''))+'; '+str(source.get('clause') or source.get('locator',''))+'; «'+str(source.get('source_quote') or source.get('quote'))[:700]+'». Ссылка из сохранённого результата; подтверждение источника не передано.')
 for e in reference_evidence:lines.append('Основание: требование утверждённого эталона, '+str(e.get('address') or e.get('locator',''))+' — «'+str(e.get('quote',''))[:700]+'»')
 if not any(line.startswith('Основание:') for line in lines):
  evidence=next((e for e in f.get('evidence',[]) if e.get('quote')),None)
  if evidence:lines.append('Основание: исходный текст проверяемого документа, '+str(evidence.get('address') or evidence.get('locator',''))+' — «'+str(evidence['quote'])[:240]+'». Отдельное нормативное основание в результате не указано.')
  else:lines.append('Основание: в результате проверки не указано; требуется уточнение специалиста.')
 return '\n'.join(x for x in lines if x)

def plan(source,working,document,result_version,findings,decisions=None,proposals=None,include_preliminary=False,include_style=False,*,root=None):
 """Programmatically validate specialist-approved structured proposals before rendering."""
 decisions=decisions or {};proposals=proposals or {};source_hash=sha(source);working_hash=sha(working)
 if source_hash!=document['source_sha256'] or working_hash!=document['working_sha256']:raise ReviewError('Хеш исходника или рабочей копии изменился; требуется новая проверка')
 root=load(working) if root is None else root;ps=paragraphs(root);operations=[];outcomes=[]
 aliases=set(document['aliases']);references=set(document.get('reference_aliases',[]))
 for f in findings:
  fid=str(f['id']);status=f.get('status');evidence=[e for e in f.get('evidence',[]) if str(e.get('document',e.get('document_id',''))) in aliases]
  if not evidence:continue
  if source_hash!=working_hash and f.get('_origin')=='normative':
   outcomes.append({'finding_id':fid,'kind':'skip','reason':'Для нормативного DOC нет подтверждения хеша той же рабочей DOCX-копии'});continue
  disposition=decisions.get(fid,{}).get('state','new')
  if status not in ('confirmed','question') and not (include_style and status=='style') and not (include_preliminary and status in ('candidate','verifying')) or disposition in ('fixed','disputed'):
   outcomes.append({'finding_id':fid,'kind':'skip','reason':'Статус или решение специалиста исключает применение'});continue
  for e in evidence:
   m=re.fullmatch(r'p([1-9]\d*)',str(e.get('locator','')))
   if not m or int(m[1])>len(ps):
    outcomes.append({'finding_id':fid,'kind':'skip','reason':'Неподдерживаемый или отсутствующий адрес блока: '+str(e.get('locator',''))});continue
   p=ps[int(m[1])-1];span,reason=locate(p,e)
   if span is None and reason=='Цитата не подтверждена в указанном абзаце':
    outcomes.append({'finding_id':fid,'kind':'skip','reason':reason});continue
   op={'finding_ids':[fid],'document_id':document['id'],'result_version':result_version,'source_sha256':source_hash,'working_sha256':working_hash,
    'part':'word/document.xml','locator':e['locator'],'block_sha256':fingerprint(text(p)),'start':span[0] if span else 0,'end':span[1] if span else len(text(p)),
    'original':text(p)[span[0]:span[1]] if span else text(p),'context':text(p),'type':'comment','proposed':'','validation':'comment_only','reason':reason}
   proposal=proposals.get(fid)
   if status=='confirmed' and span and proposal:
    keys=('document_id','result_version','source_sha256','working_sha256','part','locator','start','end','original')
    valid=all(proposal.get(k)==op[k] for k in keys) and proposal.get('verification')=='specialist_confirmed' and proposal.get('type') in ('replace','insert','delete')
    proposed=proposal.get('proposed','')
    if valid and isinstance(proposed,str) and len(proposed)<=10000 and not any(c in proposed for c in '\t\r\n') and safe_range(p,*span):
     op.update(type=proposal['type'],proposed=proposed,validation='verified_source_and_specialist',basis=proposal.get('basis','Решение специалиста'))
     if op['type']=='insert':op.update(start=op['end'],original='')
     if op['type']=='delete':op['proposed']=''
    else:op['reason']='Неподдерживаемый диапазон: поле, прежняя правка или вложенный объект' if valid and not safe_range(p,*span) else 'Редакция, версия или диапазон не подтверждены'
   elif status=='confirmed':op['reason']=op['reason'] or 'Нет проверенной структурированной редакции; свободное предложение не применяется'
   op['comment']=comment_message(dict(f,evidence=[e]),op['reason'],[x for x in f.get('evidence',[]) if str(x.get('document',x.get('document_id',''))) in references])
   if op['type']=='comment' and not safe_range(p,op['start'],op['end']):op.update(start=0,end=len(text(p)),anchor_scope='paragraph',reason=op['reason']+'; привязка к абзацу: поля, прежние правки или вложенные объекты')
   operations.append(op)
 # Merge equivalent edits; all conflicting edits become comments.
 merged=[];seen={}
 for op in operations:
  key=(op['locator'],op['start'],op['end'],op['type'],op['original'],op['proposed'])
  if op['type']!='comment' and key in seen:
   prior=seen[key];prior['finding_ids']+=op['finding_ids'];prior['comment']+='\n\n'+op['comment'];continue
  seen[key]=op;merged.append(op)
 for a in merged:
  if a['type']=='comment':continue
  for b in merged:
   if a is b or b['type']=='comment' or a['locator']!=b['locator']:continue
   if max(a['start'],b['start'])<min(a['end'],b['end']) or a['start']==a['end'] and b['start']<=a['start']<=b['end'] or b['start']==b['end'] and a['start']<=b['start']<=a['end']:
    a['conflict']=b['conflict']=True
 for op in merged:
  if op.get('conflict'):op.update(type='comment',validation='comment_only',reason='Пересекающиеся или противоречащие редакции требуют уточнения');op['comment']+='\nОграничение: '+op['reason']
 return {'generator':VERSION,'document':document,'result_version':result_version,'source_sha256':source_hash,'working_sha256':working_hash,'operations':merged,'outcomes':outcomes,'created':datetime.now(timezone.utc).isoformat()}

def node(tag,**attrs):return E.Element(Q+tag,{Q+k:str(v) for k,v in attrs.items()})
def comment_reference(cid):
 r=node('r');rp=node('rPr');rp.append(node('rStyle',val='CommentReference'));r.append(rp);r.append(node('commentReference',id=cid));return r
def run(template,value,deleted=False):
 r=node('r');r.attrib.update(template.attrib);rp=template.find(Q+'rPr')
 if rp is not None:r.append(copy.deepcopy(rp))
 t=node('delText' if deleted else 't');t.set(XML+'space','preserve');t.text=value;r.append(t);return r

def generate(source,working,saved,destination):
 if saved.get('generator')!=VERSION or sha(source)!=saved['source_sha256'] or sha(working)!=saved['working_sha256']:raise ReviewError('Устаревший план: версия генератора или хеш исходника изменился')
 root=load(working);ps=paragraphs(root);changed={};report=[]
 with ZipFile(working) as z:
  rels=xml(z.read('word/_rels/document.xml.rels')) if 'word/_rels/document.xml.rels' in z.namelist() else E.Element('{'+R+'}Relationships',nsmap={None:R})
  types=xml(z.read('[Content_Types].xml'));comments_rel=[r for r in rels if r.get('Type','').endswith('/comments')]
  if len(comments_rel)>1:raise ReviewError('Несколько связей comments; требуется уточнение')
  comments_name=posixpath.normpath(posixpath.join('word',comments_rel[0].get('Target'))) if comments_rel else 'word/comments.xml'
  if not comments_name.startswith('word/') or '..' in comments_name.split('/'):raise ReviewError('Небезопасная часть comments')
  comments=xml(z.read(comments_name)) if comments_name in z.namelist() else E.Element(Q+'comments',nsmap={'w':W})
  ids=[int(v) for x in (root,comments) for v in x.xpath('//@w:id',namespaces=NS) if v.isdigit()]
  for name in z.namelist():
   if name.startswith('word/') and name.endswith('.xml') and name not in ('word/document.xml',comments_name):ids.extend(int(v) for v in xml(z.read(name)).xpath('//@w:id',namespaces=NS) if v.isdigit())
  uid=max(ids+[0])+1
  date=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
  def nextid():
   nonlocal uid
   value=uid;uid+=1;return str(value)
  def revision(kind,r):
   n=node(kind,id=nextid(),author='NormControl',date=date);n.append(r);return n
  groups={}
  for op in saved['operations']:groups.setdefault(op['locator'],[]).append(copy.deepcopy(op))
  new_comments=[];new_revisions=[]
  for locator,ops in groups.items():
   p=ps[int(locator[1:])-1]
   if any(o['block_sha256']!=fingerprint(text(p)) for o in ops):raise ReviewError('Изменился исходный блок '+locator)
   for op in ops:
    cid=nextid();op['comment_ids']=[cid];op['revision_ids']=[]
    c=node('comment',id=cid,author='NormControl',initials='NC',date=date)
    for line in op['comment'].splitlines():cp=node('p');cp.append(run(node('r'),line));c.append(cp)
    comments.append(c);new_comments.append(cid)
   # Whole-paragraph anchors for unsupported structures; original children stay intact.
   fallback=[o for o in ops if not safe_range(p,o['start'],o['end'])]
   exact=[o for o in ops if o not in fallback]
   for op in fallback:
    cid=op['comment_ids'][0];start=node('commentRangeStart',id=cid);end=node('commentRangeEnd',id=cid);rr=comment_reference(cid)
    p.insert(1 if p.find(Q+'pPr') is not None else 0,start);p.append(end);p.append(rr)
   if exact:
    boundaries={o['start'] for o in exact}|{o['end'] for o in exact};offset=0;emitted=set();starts={};ends={};templates={}
    for o in exact:starts.setdefault(o['start'],[]).append(o);ends.setdefault(o['end'],[]).append(o)
    def markers(position,template):
     values=[]
     if position in emitted:return values
     emitted.add(position)
     for o in ends.get(position,[]):
      if o['start']==o['end']:continue
      cid=o['comment_ids'][0]
      if o['type']=='replace' and o['proposed']:
       ins=revision('ins',run(templates[cid],o['proposed']));values.append(ins);o['revision_ids'].append(ins.get(Q+'id'));new_revisions.append(ins.get(Q+'id'))
      values.append(node('commentRangeEnd',id=cid));values.append(comment_reference(cid))
     for o in starts.get(position,[]):
      cid=o['comment_ids'][0];values.append(node('commentRangeStart',id=cid))
      templates[cid]=copy.deepcopy(template)
      if o['type']=='insert' and o['proposed']:
       ins=revision('ins',run(template,o['proposed']));values.append(ins);o['revision_ids'].append(ins.get(Q+'id'));new_revisions.append(ins.get(Q+'id'))
      if o['start']==o['end']:
       values.append(node('commentRangeEnd',id=cid));values.append(comment_reference(cid))
     return values
    for child in list(p):
     if child.tag!=Q+'r':
      offset+=len(text(child));continue
     parts=[]
     for t in child:
      if t.tag==Q+'rPr':continue
      if t.tag not in (Q+'t',Q+'tab',Q+'br',Q+'cr'):
       parts+=markers(offset,child);r=node('r');rp=child.find(Q+'rPr')
       if rp is not None:r.append(copy.deepcopy(rp))
       r.append(copy.deepcopy(t));parts.append(r);continue
      value=t.text or '' if t.tag==Q+'t' else ('\t' if t.tag==Q+'tab' else '\n')
      cuts=sorted({0,len(value)}|{v-offset for v in boundaries if offset<=v<=offset+len(value)})
      for a,b in zip(cuts,cuts[1:]):
       parts+=markers(offset+a,child)
       active=next((o for o in exact if o['type'] in ('replace','delete') and o['start']<=offset+a<o['end']),None)
       if t.tag!=Q+'t' and not active:
        r=node('r');rp=child.find(Q+'rPr')
        if rp is not None:r.append(copy.deepcopy(rp))
        r.append(copy.deepcopy(t));parts.append(r)
       else:
        r=run(child,value[a:b],deleted=bool(active))
        if active:
         deletion=revision('del',r);parts.append(deletion);active['revision_ids'].append(deletion.get(Q+'id'));new_revisions.append(deletion.get(Q+'id'))
        else:parts.append(r)
      offset+=len(value)
     pos=p.index(child);p.remove(child)
     for j,n in enumerate(parts):p.insert(pos+j,n)
    template=next((c for c in reversed(list(p)) if c.tag==Q+'r'),node('r'))
    for n in markers(offset,template):p.append(n)
   for op in ops:report.append({'finding_ids':op['finding_ids'],'kind':'edit' if op['type']!='comment' else 'comment','reason':op['reason'],'locator':locator,'revision_ids':op['revision_ids'],'comment_ids':op['comment_ids']})
  if saved['operations']:
   if not comments_rel:
    existing={r.get('Id') for r in rels};i=1
    while 'rIdNC'+str(i) in existing:i+=1
    rels.append(E.Element('{'+R+'}Relationship',Id='rIdNC'+str(i),Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments',Target=posixpath.relpath(comments_name,'word')))
   if not any(x.get('PartName')=='/'+comments_name for x in types):types.append(E.Element('{'+CT+'}Override',PartName='/'+comments_name,ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml'))
   changed[comments_name]=dump(comments);changed['word/_rels/document.xml.rels']=dump(rels);changed['[Content_Types].xml']=dump(types)
  settings=xml(z.read('word/settings.xml')) if 'word/settings.xml' in z.namelist() else E.Element(Q+'settings',nsmap={'w':W})
  if 'word/settings.xml' not in z.namelist():
   existing={r.get('Id') for r in rels};i=1
   while 'rIdNCSettings'+str(i) in existing:i+=1
   rels.append(E.Element('{'+R+'}Relationship',Id='rIdNCSettings'+str(i),Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings',Target='settings.xml'))
   types.append(E.Element('{'+CT+'}Override',PartName='/word/settings.xml',ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml'))
  if settings is not None:
   for tag in ('trackRevisions','revisionView'):
    old=settings.find(Q+tag)
    if old is None:old=node(tag);settings.append(old)
    if tag=='trackRevisions':old.set(Q+'val','true')
    else:
     for k in ('markup','comments','insDel','formatting'):old.set(Q+k,'true')
   changed['word/settings.xml']=dump(settings)
  core=xml(z.read('docProps/core.xml')) if 'docProps/core.xml' in z.namelist() else E.Element('{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties',nsmap={'cp':'http://schemas.openxmlformats.org/package/2006/metadata/core-properties','dc':'http://purl.org/dc/elements/1.1/'})
  if 'docProps/core.xml' not in z.namelist():
   package_rels=xml(z.read('_rels/.rels')) if '_rels/.rels' in z.namelist() else E.Element('{'+R+'}Relationships',nsmap={None:R})
   existing={r.get('Id') for r in package_rels};i=1
   while 'rIdNCCore'+str(i) in existing:i+=1
   package_rels.append(E.Element('{'+R+'}Relationship',Id='rIdNCCore'+str(i),Type='http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties',Target='docProps/core.xml'))
   changed['_rels/.rels']=dump(package_rels)
   types.append(E.Element('{'+CT+'}Override',PartName='/docProps/core.xml',ContentType='application/vnd.openxmlformats-package.core-properties+xml'))
  for uri,tag in [('http://purl.org/dc/elements/1.1/','creator'),('http://schemas.openxmlformats.org/package/2006/metadata/core-properties','lastModifiedBy')]:
   n=core.find('{'+uri+'}'+tag)
   if n is None:n=E.SubElement(core,'{'+uri+'}'+tag)
   n.text='NormControl'
  changed['docProps/core.xml']=dump(core)
  changed['word/_rels/document.xml.rels']=dump(rels);changed['[Content_Types].xml']=dump(types)
  changed['word/document.xml']=dump(root)
  with ZipFile(destination,'w',ZIP_DEFLATED) as out:
   for i in z.infolist():out.writestr(i,changed.pop(i.filename) if i.filename in changed else z.read(i))
   for name,raw in changed.items():out.writestr(name,raw)
 validate(destination,new_comments,new_revisions)
 if sha(source)!=saved['source_sha256'] or sha(working)!=saved['working_sha256']:raise ReviewError('Исходник изменился во время формирования; файл не публикуется')
 return {'operations':report,'outcomes':saved['outcomes'],'edits':sum(r['kind']=='edit' for r in report),'comments':len(new_comments),'skips':len(saved['outcomes']),'new_revision_ids':new_revisions,'new_comment_ids':new_comments}

def validate(path,comment_ids=(),revision_ids=()):
 root=load(path)
 with ZipFile(path) as z:
  rels=xml(z.read('word/_rels/document.xml.rels'));matches=[r for r in rels if r.get('Type','').endswith('/comments')]
  comments=xml(z.read(posixpath.normpath(posixpath.join('word',matches[0].get('Target'))))) if matches else None
  for cid in comment_ids:
   for tag in ('commentRangeStart','commentRangeEnd','commentReference'):
    if len(root.xpath('.//w:'+tag+'[@w:id="'+cid+'"]',namespaces=NS))!=1:raise ReviewError('Некорректная граница комментария '+cid)
   if comments is None or len(comments.xpath('w:comment[@w:id="'+cid+'"]',namespaces=NS))!=1:raise ReviewError('Нет комментария '+cid)
  for rid in revision_ids:
   nodes=root.xpath('.//w:ins[@w:id="'+rid+'"]|.//w:del[@w:id="'+rid+'"]',namespaces=NS)
   if len(nodes)!=1:raise ReviewError('Некорректная правка '+rid)
   if nodes[0].tag==Q+'del' and nodes[0].find('.//'+Q+'t') is not None:raise ReviewError('Удаление содержит w:t')
