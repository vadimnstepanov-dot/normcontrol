"""Text geometry plus table-aware OCR for scanned AND mixed pages."""
import re
from pathlib import Path
import subprocess
from .ingest import normalize
from .tables import cell, finalize, continuations
from .document_ocr import cluster


def render_page(path,page,output,dpi=220):
    output=Path(output)
    subprocess.run(['pdftoppm','-f',str(page),'-l',str(page),'-singlefile','-png','-r',str(dpi),str(path),str(output.with_suffix(''))],
                   check=True,timeout=120,capture_output=True)
    return output


def text_table(page,table,index):
    boxes=table.cells
    xs=cluster([b[i] for b in boxes for i in (0,2)],tolerance=2)
    ys=cluster([b[i] for b in boxes for i in (1,3)],tolerance=2)
    cs=[]
    for box in boxes:
        c0,c1=[min(range(len(xs)),key=lambda i:abs(xs[i]-box[k])) for k in (0,2)]
        r0,r1=[min(range(len(ys)),key=lambda i:abs(ys[i]-box[k])) for k in (1,3)]
        if r0==r1 or c0==c1:continue
        # Crop centre containment avoids words on a shared border leaking to both cells.
        words=[w for w in page.extract_words() if box[0]<=(w['x0']+w['x1'])/2<box[2] and box[1]<=(w['top']+w['bottom'])/2<box[3]]
        lines={}
        for w in words:lines.setdefault(round(w['top']/3),[]).append(w['text'])
        text='\n'.join(' '.join(v) for v in lines.values())
        cs.append(cell(f'page/{page.page_number}/table/{index}/r{r0+1}/c{c0+1}',r0+1,c0+1,text,
                       rowspan=r1-r0,colspan=c1-c0,method='pdfplumber',
                       bbox=[box[0]/page.width,box[1]/page.height,box[2]/page.width,box[3]/page.height]))
    top=table.bbox[1]
    preceding=page.crop((0,max(0,top-85),page.width,max(1,top))).extract_text() or ''
    caption=next((line for line in reversed(preceding.splitlines()) if re.search(r'таблиц',line,re.I)),'')
    depth=max((c['rowspan'] for c in cs if c['row']==1),default=1)
    return finalize(dict(id=f'page/{page.page_number}/table/{index}',page=page.page_number,rows=len(ys)-1,columns=len(xs)-1,
                         cells=cs,header_rows=list(range(1,depth+1)),header_basis='geometry_candidate',
                         caption=caption,heading_path=[],note_refs=[],issues=[],method='pdfplumber',
                         bbox=[table.bbox[0]/page.width,table.bbox[1]/page.height,table.bbox[2]/page.width,table.bbox[3]/page.height]))


def read_pdf(path,run,cache,*,max_pages=None,ocr=True,cancel=lambda:False):
    import pdfplumber
    from .document_ocr import RuledOCR
    blocks=[];tables=[];visuals=[];coverage=[];pages=[]
    with pdfplumber.open(path) as doc:
        if len(doc.pages)>2000:raise ValueError('PDF page limit')
        for page in doc.pages:
            i=page.page_number
            if cancel():raise InterruptedError('Parse paused at page boundary; cached pages preserved')
            if max_pages and i>max_pages:
                coverage.append(dict(locator=f'page/{i}..{len(doc.pages)}',state='unreadable',reason='Explicit page limit; remaining pages not read'));break
            if page.width*page.height*(220/72)**2>45_000_000:raise ValueError('PDF rendered page exceeds pixel limit')
            pagekey=cache.key('pdf-page',dict(page=i,ocr=ocr))
            old=cache.get(pagekey)
            if old is None:
                image=run/'assets'/f'page-{i}.png';render_page(path,i,image)
                pageblocks=[];pagetables=[];pagecoverage=[]
                words=page.extract_words();text=page.extract_text() or ''
                for j,word in enumerate(words,1):
                    pageblocks.append(dict(locator=f'page/{i}/word/{j}',kind='pdf_text',exact_text=word['text'],search_text=normalize(word['text']).casefold(),
                         heading_path=[],page=i,bbox=[word['x0']/page.width,word['top']/page.height,word['x1']/page.width,word['bottom']/page.height],
                         read_method='pdf_text',recognition_confidence=None,render_id=pagekey))
                from .pdf_context import borderless_candidates,borderless_table,adjacent_notes
                ruled=page.find_tables()
                for j,t in enumerate(ruled,1):pagetables.append(text_table(page,t,j))
                for j,t in enumerate(borderless_candidates(page,ruled),len(ruled)+1):
                    candidate=borderless_table(page,t,j)
                    if candidate:pagetables.append(candidate)
                for t in pagetables:pageblocks+=adjacent_notes(page,t,pagetables)
                # Full-page OCR on image-bearing pages catches mixed pages with some selectable text.
                raster = bool(page.images) or not words
                ocr_result=None
                if raster and ocr:
                    try:ocr_result=RuledOCR().analyze(image,{'page':i,'text_context':text[:3000]})
                    except (OSError,ValueError,subprocess.SubprocessError) as error:
                        pagecoverage.append(dict(locator=f'page/{i}',state='unreadable',reason='OCR failed: '+type(error).__name__))
                    if ocr_result:
                        # Keep a separate observation: do not duplicate it as exact native PDF evidence.
                        for j,w in enumerate(ocr_result['words'],1):
                            w0,h0=ocr_result['size'];b=w['bbox']
                            pageblocks.append(dict(locator=f'page/{i}/ocr/{j}',kind='ocr_text',exact_text=w['text'],search_text=normalize(w['text']).casefold(),
                                heading_path=[],page=i,bbox=[b[0]/w0,b[1]/h0,b[2]/w0,b[3]/h0],
                                read_method='tesseract',recognition_confidence=w['confidence'],render_id=pagekey,
                                independent_observation=True))
                        pagetables+=ocr_result['tables']
                        pagecoverage.append(dict(locator=f'page/{i}/ocr',state='needs_review',reason='OCR observation requires visual/independent verification'))
                if not text.strip() and not (ocr_result or {}).get('text','').strip():
                    pagecoverage.append(dict(locator=f'page/{i}',state='unreadable',reason='No readable text from text layer or OCR'))
                if raster and not ocr:pagecoverage.append(dict(locator=f'page/{i}/images',state='unreadable',reason='OCR explicitly disabled'))
                old=dict(blocks=pageblocks,tables=pagetables,coverage=pagecoverage,
                    page=dict(number=i,width=page.width,height=page.height,rotation=page.rotation,render_id=pagekey,
                              image=f'assets/page-{i}.png',text_layer=bool(words),contains_images=bool(page.images),
                              ocr=bool(ocr_result),coordinate_system='normalized_top_left'))
                cache.put(pagekey,old)
            blocks+=old['blocks'];tables+=old['tables'];coverage+=old['coverage'];pages.append(old['page'])
            cache.progress(dict(completed_pages=i,total_pages=len(doc.pages),state='running'))
            page.close()
    continuations([t for t in tables if t['method']=='pdfplumber'])
    return dict(blocks=blocks,tables=tables,visuals=visuals,pages=pages,coverage=coverage,
                counts=dict(pages=len(doc.pages),read_pages=len(pages),tables=len(tables)))
