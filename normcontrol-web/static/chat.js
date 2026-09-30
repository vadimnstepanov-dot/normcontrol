'use strict';
(() => {
  const initial=JSON.parse(document.getElementById('chat-initial').textContent), base='/normcontol/chat/';
  const $=id=>document.getElementById(id), uid=document.documentElement.dataset.uiUser;
  const token=document.querySelector('[name=csrfmiddlewaretoken]').value;
  let current=initial.conversation, config={}, files=[], sending=false, key=crypto.randomUUID(), conversationKey=crypto.randomUUID(), timer, draftTimer;
  let expert=false, rendered='', frameBatch='', lastFocus=null, refreshing=false;
  let preferenceTimer,preferenceBusy=false,pendingPreferences={};
  let historyLoading=false,historyLoaded=false;
  let clockNode=null,clockValue=null,clockReceived=0;
  const prefs=$('expert-toggle'), frame=$('expert-frame');
  async function api(path,method='GET',body=null){
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),method==='GET'?15000:60000);
    const options={method,headers:{'X-CSRFToken':token},credentials:'same-origin',signal:controller.signal};
    if(body instanceof FormData)options.body=body;
    else if(body!==null){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
    let response;try{response=await fetch(base+path,options);}catch(error){if(error.name==='AbortError')throw Error('Сервер не ответил вовремя. Проверьте связь.');throw error;}finally{clearTimeout(timeout);}
    if(response.redirected&&response.url.includes('/login/'))throw Object.assign(Error('Сессия завершена. Войдите снова.'),{status:401});
    const data=await response.json().catch(()=>({error:'Сервер не подтвердил доступ. Проверьте сеанс входа.'}));
    if(!response.ok)throw Object.assign(Error(data.error||data.clarification||'Запрос не принят'),{data,status:response.status});
    return data;
  }
  function savePreferences(value){
    Object.assign(pendingPreferences,value);clearTimeout(preferenceTimer);
    preferenceTimer=setTimeout(flushPreferences,250);
  }
  async function flushPreferences(){
    if(preferenceBusy||!Object.keys(pendingPreferences).length)return;
    preferenceBusy=true;const value=pendingPreferences;pendingPreferences={};
    try{await api('preferences/','POST',value);}catch(error){notify('Настройки применены в этом окне. '+error.message);}
    finally{preferenceBusy=false;if(Object.keys(pendingPreferences).length)flushPreferences();}
  }
  function node(tag,text,cls){const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;}
  function notify(text){$('connection').textContent=text;}
  function cacheKey(){return 'normcontrol-chat-draft:'+uid+':'+(current?.id||'new');}
  function draft(){return {text:$('message').value,...config};}
  function saveDraft(){
    try{localStorage.setItem(cacheKey(),JSON.stringify(draft()));}catch(e){notify('Не удалось сохранить черновик');}
    clearTimeout(draftTimer);
    if(current&&!current.batch){const cid=current.id,value=draft();draftTimer=setTimeout(()=>api('conversations/'+cid+'/','POST',value).catch(e=>notify(e.message)),600);}
  }
  function restoreDraft(){
    let saved=current?.draft||{};try{saved={...saved,...JSON.parse(localStorage.getItem(cacheKey())||'{}')};}catch(e){}
    $('message').value=saved.text||'';config={checks:saved.checks||[],normative_sets:saved.normative_sets||[],experience:saved.experience||'',roles:[]};
    if(saved.roles?.length)notify('Для восстановления черновика прикрепите файлы повторно.');
  }
  function attachmentList(){
    $('attachments').replaceChildren();
    files.forEach((f,i)=>{const row=node('div',null,'attachment'),name=node('span',f.name);const select=node('select');select.setAttribute('aria-label','Роль '+f.name);
      [['','Выберите роль'],['target','Проверить'],['approved_reference','Как основание']].forEach(([value,label])=>{const option=node('option',label);option.value=value;select.append(option);});
      select.value=config.roles[i]||'';select.addEventListener('change',()=>{config.roles[i]=select.value;saveDraft();});
      const remove=node('button','×');remove.type='button';remove.setAttribute('aria-label','Убрать '+f.name);remove.onclick=()=>{files.splice(i,1);config.roles.splice(i,1);attachmentList();saveDraft();};
      row.append(name,select,remove);$('attachments').append(row);});
    $('doc-review-warning').hidden=!files.some(f=>/\.doc$/i.test(f.name));
  }
  function conditions(){
    $('conditions').replaceChildren();
    initial.choices.checks.forEach(c=>{const label=node('label'),input=node('input');input.type='checkbox';input.value=c.id;input.checked=config.checks.includes(c.id);input.onchange=()=>{config.checks=[...$('conditions').querySelectorAll('input:checked')].map(x=>x.value);saveDraft();};label.append(input,node('span',c.name));$('conditions').append(label);});
    const norms=node('select');norms.setAttribute('aria-label','Нормативная база');norms.multiple=initial.choices.norms.length>1;
    norms.append(node('option','Выберите базу'));norms.firstChild.value='';
    initial.choices.norms.forEach(n=>{const option=node('option',n.name);option.value=n.id;option.selected=config.normative_sets.includes(n.id);norms.append(option);});
    norms.onchange=()=>{config.normative_sets=[...norms.selectedOptions].map(x=>x.value).filter(Boolean);saveDraft();};$('conditions').append(norms);
  }
  function duration(seconds){
    const total=Math.max(0,Math.floor(seconds)),hours=Math.floor(total/3600),minutes=Math.floor(total%3600/60),rest=total%60;
    return (hours?hours+' ч ':'')+(minutes||hours?minutes+' мин ':'')+rest+' с';
  }
  function updateClock(){
    if(!clockNode||!clockValue)return;
    const seconds=clockValue.seconds+(clockValue.live?(performance.now()-clockReceived)/1000:0);
    clockNode.textContent=clockValue.label+' '+duration(seconds);
  }
  function render(){
    const data=current;clockValue=data?.state?.clock;clockReceived=performance.now();
    // Polling clock samples must not rebuild messages or steal control focus.
    const signature=JSON.stringify(data?{messages:data.messages,state:{...data.state,clock:null}}:null);if(signature===rendered){updateClock();return;}rendered=signature;clockNode=null;
    const main=$('chat-main'),nearBottom=main.scrollHeight-main.scrollTop-main.clientHeight<80,position=main.scrollTop;
    document.body.classList.toggle('chat-empty',!data?.messages?.length);$('empty').hidden=!!data?.messages?.length;$('messages').replaceChildren();
    for(const m of data?.messages||[]){const row=node('article',null,'message '+m.role),label=node('div',null,'message-label');label.append(node('span',m.role==='user'?'Вы':'NormControl'));row.append(label,node('div',m.text));
      for(const doc of m.metadata.documents||[])row.append(node('div',doc.name+' · '+(doc.role==='approved_reference'?'Как основание':'Проверить'),'doc-name'));
      if(m.metadata.kind==='progress'&&data.state){const state=data.state;
        if(state.clock?.known){clockNode=node('span',null,'chat-elapsed');clockNode.setAttribute('aria-live','off');clockNode.title=state.clock.note;label.append(clockNode);updateClock();}
        if(state.progress)row.append(node('div','Выполнено задач: '+state.progress.completed+' из '+state.progress.total+'. План может уточняться.','progress'));
        const controls=node('div',null,'actions');const labels={pause:'Приостановить',resume:'Продолжить',cancel:'Отменить',rerun:'Повторить'};
        for(const action of state.actions){const b=node('button',labels[action]);b.onclick=async()=>{b.disabled=true;try{const c=await api('conversations/'+data.id+'/control/','POST',{action});if(c.id!==current.id)open(c);else{current=c;render();}}catch(e){notify(e.message);}finally{b.disabled=false;}};controls.append(b);}row.append(controls);
        if(state.terminal){const exports=node('div',null,'files');for(const [kind,label] of [['word','Word с правками'],['xlsx','Excel'],['summary','Краткий отчёт']]){
          if(kind==='word'){const button=node('button',label);button.onclick=async()=>{button.disabled=true;notify('Формирование Word…');try{await api('conversations/'+data.id+'/export/word/','POST',{});await refresh();}catch(e){notify(e.message);}finally{button.disabled=false;}};exports.append(button);}
          else{const link=node('a',label);link.href=base+'conversations/'+data.id+'/export/'+kind+'/';exports.append(link);}}
          for(const exp of state.exports){if(exp.url){const link=node('a','Скачать Word с правками');link.href=exp.url;exports.append(link);}else exports.append(node('span',exp.error||({queued:'Word в очереди на формирование',building:'Word формируется',planned:'Word подготовлен к формированию',waiting_ram:'Word ожидает свободной памяти',failed:'Ошибка формирования Word'})[exp.state]||exp.state,'progress'));}row.append(exports);}
      }$('messages').append(row);
    }
    if(nearBottom)main.scrollTop=main.scrollHeight;else main.scrollTop=position;
  }
  function updateURL(push=false){const url=new URL(location.href);url.pathname=base;url.search='';if(current)url.searchParams.set('conversation',current.id);history[push?'pushState':'replaceState']({},'',url);}
  function open(c,navigate=true){current=c;files=[];$('documents').value='';key=crypto.randomUUID();rendered='';restoreDraft();attachmentList();conditions();render();if(navigate)updateURL(true);if(expert)loadExpert();}
  async function refresh(){if(!current||refreshing)return;refreshing=true;const cid=current.id;
    try{const data=await api('conversations/'+cid+'/');if(cid!==current?.id)return;current=data;render();}
    catch(e){if(cid!==current?.id)return;if([401,403,404].includes(e.status)){current=null;files=[];frame.removeAttribute('src');setExpert(false,false);attachmentList();render();}notify(e.message);}
    finally{refreshing=false;}
  }
  function loadExpert(){const batch=current?.batch||'';
    if(!batch&&!config.checks.length){const text=($('message').value.trim()||$('message').placeholder).toLowerCase();const patterns={sto:/сто|норматив/,logic:/логик/,language:/граммат|грамот|язык|терминолог|орфограф/,formatting:/оформлен/};config.checks=Object.keys(patterns).filter(k=>patterns[k].test(text));}
    if(!frame.getAttribute('src')||frameBatch!==batch){frameBatch=batch;frame.src=initial.expert_url||current?.state?.expert_url||'/normcontol/batches/new/?presentation=expert&embedded=1';initial.expert_url='';}
  }
  function setExpert(value,save=true){
    if(!value&&expert&&!current?.batch){try{const doc=frame.contentDocument,form=doc?.getElementById('launch-form');if(form){const input=doc.getElementById('documents');if(input?.files.length)files=[...input.files];$('message').value=form.elements.namedItem('user_prompt')?.value||$('message').value;config.checks=[...form.querySelectorAll('[name=checks]:checked')].map(x=>x.value);config.normative_sets=[...form.querySelectorAll('[name=normative_sets]:checked')].map(x=>x.value);config.experience=form.elements.namedItem('experience')?.value||'';config.roles=JSON.parse(form.elements.namedItem('document_roles')?.value||'[]');attachmentList();conditions();saveDraft();}}catch(e){notify('Не удалось прочитать параметры полного интерфейса');}}
    lastFocus=document.activeElement;expert=value;$('chat-main').hidden=value;$('composer-pane').hidden=value;$('expert-pane').hidden=!value;
    $('bottom').hidden=value;setSidebar(false);$('chat-sidebar-toggle').hidden=value;
    prefs.setAttribute('aria-checked',String(value));$('expert-state').textContent=value?'вкл.':'выкл.';
    if(value)loadExpert();prefs.focus();
    if(save){try{localStorage.setItem('normcontrol-presentation:'+uid,value?'expert':'chat');}catch(e){}savePreferences({presentation:value?'expert':'chat'});}

  }
  new ResizeObserver(()=>{$('chat-sidebar').style.top=document.querySelector('.chat-topbar').getBoundingClientRect().height+'px';}).observe(document.querySelector('.chat-topbar'));
  function setSidebar(open){$('chat-sidebar').hidden=!open;document.body.classList.toggle('chat-sidebar-open',open);const button=$('chat-sidebar-toggle');button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',open?'Свернуть системную панель':'Развернуть системную панель');button.title=button.getAttribute('aria-label');}
  $('chat-sidebar-toggle').onclick=()=>{if(expert){try{frame.contentDocument.getElementById('brand-sidebar-toggle')?.click();}catch(e){notify('Панель ещё загружается');}}else setSidebar($('chat-sidebar').hidden);};
  $('brand-sidebar-toggle').onclick=()=>setSidebar(false);
  prefs.onclick=()=>setExpert(!expert);
  $('attach').onclick=()=>$('documents').click();
  function attach(incoming){if(sending)return;for(const f of incoming){if(!/\.docx?$/i.test(f.name)||f.size>50*1024**2){notify('Word .doc/.docx, до 50 МБ на документ.');continue;}if(files.length>=20||files.reduce((n,x)=>n+x.size,0)+f.size>100*1024**2){notify('До 20 документов, общий размер до 100 МБ.');break;}files.push(f);config.roles.push('');}attachmentList();}
  $('documents').onchange=e=>attach([...e.target.files]);
  $('chat-main').ondragover=e=>e.preventDefault();$('chat-main').ondrop=e=>{e.preventDefault();attach([...e.dataTransfer.files]);};
  $('message').oninput=saveDraft;
  $('composer').onsubmit=async e=>{e.preventDefault();if(sending)return;sending=true;$('send').disabled=true;notify('Передача запроса…');
    try{if(!current){current=await api('conversations/','POST',{key:conversationKey});updateURL();}
      const form=new FormData();form.append('text',$('message').value.trim()||$('message').placeholder);form.append('key',key);form.append('config',JSON.stringify(config));for(const f of files)form.append('documents',f);
      current=await api('conversations/'+current.id+'/send/','POST',form);
      try{localStorage.removeItem(cacheKey());localStorage.removeItem('normcontrol-chat-draft:'+uid+':new');}catch(e){}
      files=[];$('documents').value='';$('message').value='';config={checks:[],normative_sets:[],roles:[]};key=crypto.randomUUID();attachmentList();$('conditions').hidden=true;render();notify('');
    }catch(e){notify(e.message);if(e.data?.clarification){if(e.data.conversation){current=e.data.conversation;render();} $('conditions').hidden=false;conditions();}}finally{sending=false;$('send').disabled=false;}
  };
  $('new-chat').onclick=async()=>{if(sending){notify('Дождитесь передачи запроса');return;}saveDraft();try{const next=await api('conversations/','POST',{});frame.removeAttribute('src');frameBatch='';initial.expert_url='';open(next);}catch(e){notify(e.message);}};
  function showHistory(open){$('history').hidden=!open;$('history-toggle').setAttribute('aria-expanded',String(open));}
  async function loadHistory(){
    if(historyLoading)return;historyLoading=true;
    if(!historyLoaded)$('history-items').replaceChildren(node('p','Загружаем историю…'));
    try{const data=await api('conversations/');$('history-items').replaceChildren();for(const item of data.items){
      const b=node('button',item.title);b.append(node('small',item.state));b.onclick=async()=>{if(sending)return;saveDraft();showHistory(false);try{open(item.id?await api('conversations/'+item.id+'/'):await api('conversations/','POST',{batch:item.batch}));}catch(e){notify(e.message);}};$('history-items').append(b);
    }historyLoaded=true;}catch(e){if(!historyLoaded)$('history-items').replaceChildren(node('p',e.message));notify(e.message);}finally{historyLoading=false;}
  }
  $('history-toggle').onclick=()=>{const open=$('history').hidden;showHistory(open);if(open)loadHistory();};
  $('history-close').onclick=()=>{showHistory(false);$('history-toggle').focus();};
  window.addEventListener('online',refresh);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  window.addEventListener('popstate',()=>{const cid=new URL(location.href).searchParams.get('conversation');if(cid)api('conversations/'+cid+'/').then(c=>open(c,false)).catch(e=>notify(e.message));else open(null,false);});
  // Same-origin expert navigation shares context, without touching either DOM's draft.
  frame.addEventListener('load',()=>{try{const doc=frame.contentDocument,url=new URL(frame.contentWindow.location.href);if(url.origin!==location.origin)return;
    const themeButton=doc.querySelector('[data-theme-toggle]');if(themeButton)themeButton.hidden=true;
    doc.addEventListener('submit',event=>{if(sending){event.preventDefault();notify('Дождитесь передачи запроса');}},true);
    const batch=url.searchParams.get('batch')||url.pathname.match(/batches\/([a-f0-9-]{36})\//)?.[1];
    if(batch&&batch!==current?.batch){api('conversations/','POST',{batch}).then(c=>{current=c;frameBatch=batch;render();updateURL();}).catch(e=>notify(e.message));return;}
    const form=doc.getElementById('launch-form'),input=doc.getElementById('documents');
    const prompt=form?.elements.namedItem('user_prompt');if(prompt){if(!prompt.value)prompt.value=$('message').value;prompt.addEventListener('input',()=>{$('message').value=prompt.value;saveDraft();});}
    doc.addEventListener('normcontrol-launch-state',event=>{sending=!!event.detail;$('send').disabled=sending;});
    if(form&&!current?.batch){
      if(config.checks.length)[...form.querySelectorAll('[name=checks]')].forEach(x=>x.checked=config.checks.includes(x.value));
      if(config.normative_sets.length)[...form.querySelectorAll('[name=normative_sets]')].forEach(x=>x.checked=config.normative_sets.includes(x.value));
      form.dispatchEvent(new frame.contentWindow.Event('change',{bubbles:true}));
    }
    if(form&&input&&!input.files.length&&files.length){const dt=new frame.contentWindow.DataTransfer();files.forEach(f=>dt.items.add(f));input.files=dt.files;input.dispatchEvent(new frame.contentWindow.Event('change',{bubbles:true}));
      [...doc.querySelectorAll('#file-list select')].forEach((select,i)=>{select.value=config.roles[i]||'target';select.dispatchEvent(new frame.contentWindow.Event('change'));});
      if(config.checks.length)[...form.querySelectorAll('[name=checks]')].forEach(x=>x.checked=config.checks.includes(x.value));
      if(config.normative_sets.length)[...form.querySelectorAll('[name=normative_sets]')].forEach(x=>x.checked=config.normative_sets.includes(x.value));
      form.dispatchEvent(new frame.contentWindow.Event('change',{bubbles:true}));}
  }catch(e){notify('Полный интерфейс требует действующей сессии');} });
  window.addEventListener('normcontrol-theme',e=>savePreferences({theme:e.detail}));
  restoreDraft();attachmentList();conditions();render();setExpert(initial.presentation==='expert',false);
  timer=setInterval(()=>{if(!document.hidden&&!sending)refresh();},7000);
  setInterval(()=>{if(!document.hidden)updateClock();},1000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){updateClock();refresh();}});
})();
