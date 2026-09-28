"""Isolated LibreOffice/UNO helper. Run with system Python in the document container."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def probe(source, output):
    import uno
    from com.sun.star.beans import PropertyValue
    from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
    def prop(name,value):
        p=PropertyValue();p.Name=name;p.Value=value;return p
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='office-probe-') as profile:
        pipe='kv2_'+uuid.uuid4().hex
        process=subprocess.Popen(['soffice','-env:UserInstallation='+Path(profile).as_uri(),'--headless','--norestore',
            '--nodefault','--nofirststartwizard','--accept=pipe,name='+pipe+';urp;StarOffice.ComponentContext'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        desktop=None;doc=None
        try:
            local=uno.getComponentContext()
            resolver=local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver',local)
            deadline=time.monotonic()+25
            while True:
                try:ctx=resolver.resolve('uno:pipe,name='+pipe+';urp;StarOffice.ComponentContext');break
                except Exception:
                    if time.monotonic()>deadline:raise RuntimeError('Office probe connection timeout')
                    time.sleep(.2)
            desktop=ctx.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop',ctx)
            doc=desktop.loadComponentFromURL(Path(source).resolve().as_uri(),'_blank',0,
                (prop('Hidden',True),prop('ReadOnly',True),prop('MacroExecutionMode',NEVER_EXECUTE),prop('UpdateDocMode',0)))
            if not doc:raise RuntimeError('Office probe could not open source')
            pdf=output/'source-render.pdf'
            doc.storeToURL(pdf.resolve().as_uri(),(prop('FilterName','writer_pdf_Export'),prop('Overwrite',True)))
            view=doc.getCurrentController().getViewCursor()
            enum=doc.Text.createEnumeration();paragraphs=[];tables=[]
            while enum.hasMoreElements():
                part=enum.nextElement()
                if part.supportsService('com.sun.star.text.TextTable'):
                    table=dict(name=part.Name,cells=[])
                    try:view.gotoRange(part.getAnchor(),False);table['page']=view.getPage()
                    except Exception:table['page']=None
                    for name in part.getCellNames():
                        c=part.getCellByName(name);e=c.createEnumeration();paras=[]
                        while e.hasMoreElements():
                            p=e.nextElement()
                            if p.supportsService('com.sun.star.text.Paragraph'):
                                paras.append(dict(text=p.String,label=getattr(p,'ListLabelString','')))
                        table['cells'].append(dict(name=name,text=c.String,paragraphs=paras))
                    tables.append(table);continue
                if not part.supportsService('com.sun.star.text.Paragraph'):continue
                label=getattr(part,'ListLabelString','');outline=getattr(part,'OutlineLevel',0)
                page=None
                if label or outline:
                    try:view.gotoRange(part.getStart(),False);page=view.getPage()
                    except Exception:pass
                paragraphs.append(dict(text=part.String,label=label,outline=outline,page=page))
            result=dict(method='libreoffice-uno/1',paragraphs=paragraphs,tables=tables,pdf=pdf.name,
                        page_basis='derived_libreoffice_render_not_original_word_pagination')
            (output/'office-probe.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf8')
            return result
        finally:
            if doc:
                try:doc.close(True)
                except Exception:pass
            if desktop:
                try:desktop.terminate()
                except Exception:pass
            if process.poll() is None:
                process.terminate()
                try:process.wait(10)
                except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':
    result=probe(sys.argv[1],sys.argv[2])
    print(json.dumps(dict(paragraphs=len(result['paragraphs']),tables=len(result['tables']))))
