import html
import json

def view(result,run):
    """Local evidence explorer, escaped text. Never serves arbitrary filesystem paths."""
    e=lambda v:html.escape(str(v),quote=True)
    roles={'column_table':'Таблица данных','key_value':'Форма: поле и значение','glossary':'Глоссарий','contents':'Оглавление',
        'layout':'Реквизиты / макет','text_block':'Текст в рамке','unknown':'Структура не определена'}
    sections=[]
    for t in result['tables']:
        cells={(c['row'],c['column']):c for c in t['cells']};covered=set();rows=[]
        for r in range(1,t['rows']+1):
            row=[]
            for col in range(1,t['columns']+1):
                if (r,col) in covered:continue
                c=cells.get((r,col))
                if c:
                    for rr in range(r,r+c['rowspan']):
                        for cc in range(col,col+c['colspan']):covered.add((rr,cc))
                    button=''
                    if c.get('bbox') and t.get('page'):
                        button=f'<button data-image="assets/page-{int(t["page"])}.png" data-box="{e(json.dumps(c["bbox"]))}">Показать в оригинале</button>'
                    refs=' · '.join(f'<a href="#{e(x)}">{e(x)}</a>' for x in c.get('header_refs',[]))
                    refs+=' · Примечания: '+' · '.join(f'<a href="#{e(x)}">{e(x)}</a>' for x in c.get('note_refs',[]))
                    row.append(f'<td id="{e(c["id"])}" rowspan="{c["rowspan"]}" colspan="{c["colspan"]}" title="{e(c["id"])}">{e(c["exact_text"]) or "<i>пусто</i>"}<small>{e(c["id"])} · {e(c["method"])}<br>Шапка: {refs}</small>{button}</td>')
                else:row.append('<td class="gap">Не восстановлено</td>')
            rows.append('<tr>'+''.join(row)+'</tr>')
        image=''
        if t.get('page'):
            image=f'<a href="assets/page-{int(t["page"])}.png">Оригинал страницы {int(t["page"])}</a>'
        elif t.get('word_page'):
            image=f'<a href="{e(t["word_render_pdf"])}#page={int(t["word_page"])}">Контрольный PDF · стр. {int(t["word_page"])}</a>'
        decision=t.get('structural_interpretation',{}).get('decision',{})
        explanation=f'<p>Основание: {e(t.get("structural_review_basis"))} · {e(decision.get("reason",""))}</p>'
        sections.append(f'<details><summary>{e(t.get("caption") or t["id"])} · {e(roles.get(t.get("table_role"),"Таблица"))} · {t["rows"]} × {t["columns"]} · {e(t["structure_status"])}</summary><p>{e(" / ".join(t.get("heading_addresses") or t.get("heading_path",[])))}</p>{image}{explanation}<p class="warn">{e(", ".join(t["issues"]))}</p><div class="scroll"><table>'+''.join(rows)+'</table></div></details>')
    visuals=[]
    for v in result['visuals']:
        pages_html=[]
        for page in v.get('pages',[]):
            observed=page.get('interpretation',{});value=observed.get('value',{});items=[]
            if observed.get('validation_errors'):value={}
            for node in value.get('nodes',[]):items.append((node['text'],node['bbox']))
            for fact in value.get('constraints',[]):items.append((fact['exact_text'],fact['bbox']))
            for edge in value.get('relations',[]):items.append((f'{edge["source"]} → {edge["target"]} · {edge["direction"]} · {edge["label"]}',edge['bbox']))
            for proof in page.get('geometric_arrow_evidence',[]):
                edge=proof['candidate']
                items.append((f'Направление проверено по наконечнику и линии: {edge["source"]} → {edge["target"]} (не экспертное подтверждение нормы)',edge['bbox']))
            for review in page.get('arrow_reviews',[]):
                edge=review.get('candidate')
                if edge:items.append((f'Непроверенная гипотеза по вырезке (запрещена для автоизвлечения): {edge["source"]} → {edge["target"]} · {review["observation"]["value"]["evidence"]}',edge['bbox']))
            buttons=''.join(f'<p><button data-image="{e(page["render"])}" data-box="{e(json.dumps(box))}">Показать область</button> {e(label)}</p>' for label,box in items)
            pages_html.append(f'<h4>Страница объекта {page["number"]} · {e(value.get("kind",""))}</h4><div class="panels"><div><img loading="lazy" src="{e(page["render"])}" alt="Исходное изображение"></div><div><p>Наблюдения модели — требуется сверка с оригиналом</p>{buttons}</div></div><details><summary>OCR-текст</summary><pre>{e(page.get("ocr",{}).get("text",""))}</pre></details>')
        link=f'<p>Превью вложения <a href="#{e(v.get("content_evidence",""))}">{e(v.get("content_evidence",""))}</a>; текст прочитан из вложенного DOCX.</p>' if v.get('content_evidence') else ''
        visuals.append(f'<details id="{e(v["id"])}"><summary>{e(v["locator"])} · {e(v["kind"])} · {e(v["state"])}</summary><p>{e("; ".join(v["issues"]))}</p>{link}'+''.join(pages_html)+'</details>')
    pages=''.join(f'<a href="{e(p["image"])}">Стр. {p["number"]}</a> ' for p in result.get('pages',[]))
    content='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Чтение нормативного источника</title><style>body{background:#0d1521;color:#e4eef8;font:15px/1.6 system-ui;margin:30px auto;max-width:1450px;padding:0 20px}a{color:#71deed}details{background:#162234;border:1px solid #314258;border-radius:9px;padding:15px;margin:12px 0}summary{cursor:pointer}table{border-collapse:collapse;width:100%;white-space:pre-wrap}td{border:1px solid #557087;padding:10px;vertical-align:top;min-width:70px}small{display:block;color:#a7bed4;font-size:10px}i{color:#b8c4d0}img{max-width:100%;background:white}pre{white-space:pre-wrap}.gap,.warn{color:#ffce82}.scroll{overflow:auto}td:target{outline:3px solid #71deed}</style>'''
    content+=f'<h1>Структура и доказательства источника</h1><p>{e(result["source"]["filename"])} · {e(result["parser_version"])}</p><p>Это разбор источника, не нормоконтроль. Структурная готовность не означает экспертное подтверждение.</p><p><a href="{e(result["source"]["original"])}">Скачать исходник</a> · <a href="result.json">Полные данные JSON</a></p><h2>Покрытие</h2><pre>{e(json.dumps(result["summary"],ensure_ascii=False,indent=2))}</pre><h2>Страницы</h2>{pages}<h2>Таблицы</h2>'+''.join(sections)+'<h2>Изображения и вложения</h2>'+''.join(visuals)
    content+='<style>.panels{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px}.panels img{position:sticky;top:10px}button{background:#244256;color:#dff9ff;border:1px solid #53869a;border-radius:5px;padding:6px;cursor:pointer}@media(max-width:800px){.panels{grid-template-columns:1fr}}</style>'
    if result.get('word_render'):
        w=result['word_render'];links=[]
        for b in result['blocks']:
            if b.get('numbering_verified') and b.get('number_label') and b.get('word_page'):
                links.append(f'<p><a href="{e(w["pdf"])}#page={int(b["word_page"])}">{e(b["number_label"])} {e(b["exact_text"][:160])}</a> · {e(b["locator"])} · стр. {b["word_page"]}</p>')
        content+='<h2>Проверенные адреса Word</h2><p>Страницы контрольного PDF LibreOffice; могут отличаться от пагинации Microsoft Word.</p>'+''.join(links)
    content+='<h2>Ограничения чтения</h2>'+''.join('<p>'+e(x['locator'])+': '+e(x['reason'])+'</p>' for x in result['coverage'])
    content+='''<dialog id="original"><form method="dialog"><button>Закрыть</button></form><div style="position:relative;display:inline-block;max-width:100%"><img id="page" alt="Исходная страница" style="display:block;max-height:80vh"><div id="highlight" style="position:absolute;box-sizing:border-box;border:3px solid #f07000;background:#ffaa0030;pointer-events:none"></div></div></dialog><script>
document.querySelectorAll('[data-box]').forEach(b=>b.onclick=()=>{const d=document.getElementById('original'),im=document.getElementById('page'),h=document.getElementById('highlight');const [x,y,x1,y1]=JSON.parse(b.dataset.box);im.src=b.dataset.image;h.style.cssText+=';left:'+100*x+'%;top:'+100*y+'%;width:'+100*(x1-x)+'%;height:'+100*(y1-y)+'%';d.showModal();});
</script></html>'''
    (run/'viewer.html').write_text(content,encoding='utf-8')
