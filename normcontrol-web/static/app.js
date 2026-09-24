'use strict';

const llmPanel=document.getElementById('llm-monitor');
if(llmPanel){
  const labels={online:'Доступность модели',vram_used_mb:'Память GPU, МиБ',gpu_percent:'Загрузка GPU, %',generation_tps:'Генерация, ток/с',prefill_tps:'Prefill, ток/с'};
  const fields={online:'llm-online',vram_used_mb:'llm-vram',gpu_percent:'llm-gpu',generation_tps:'llm-generation',prefill_tps:'llm-prefill'};
  let history=[],opened='',timer=null,busy=false;
  const setText=(id,value)=>{const element=document.getElementById(id);if(element)element.textContent=value;};
  const metric=value=>typeof value==='number'?new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(value):'—';
  function draw(){
    const box=document.getElementById('llm-chart'),svg=document.getElementById('llm-chart-svg');box.hidden=!opened;
    if(!opened)return;
    setText('llm-chart-title',labels[opened]);svg.replaceChildren();
    const values=history.map(item=>opened==='online'?(item.online?1:0):item[opened]);
    const finite=values.filter(value=>typeof value==='number'&&Number.isFinite(value));
    if(!finite.length){setText('llm-chart-min','Нет замеров');setText('llm-chart-max','');return;}
    const minimum=opened==='online'?0:Math.min(...finite),maximum=opened==='online'?1:Math.max(...finite);
    const range=maximum-minimum||1;
    for(let y=25;y<=125;y+=50){const line=document.createElementNS('http://www.w3.org/2000/svg','line');line.setAttribute('x1','0');line.setAttribute('x2','720');line.setAttribute('y1',String(y));line.setAttribute('y2',String(y));line.setAttribute('class','grid-line');svg.append(line);}
    let path='';values.forEach((value,index)=>{if(typeof value!=='number'||!Number.isFinite(value))return;const x=history.length===1?360:index*720/(history.length-1);const y=130-(value-minimum)/range*110;path+=(path?' L':'M')+x.toFixed(1)+','+y.toFixed(1);});
    const line=document.createElementNS('http://www.w3.org/2000/svg','path');line.setAttribute('d',path);line.setAttribute('class','data-line');svg.append(line);
    setText('llm-chart-min',opened==='online'?'Выкл':metric(minimum));setText('llm-chart-max',opened==='online'?'Вкл':metric(maximum));
  }
  async function refresh(){
    if(busy)return;busy=true;clearTimeout(timer);
    let pending=false;
    try{
      const response=await fetch(llmPanel.dataset.statusUrl,{cache:'no-store'});if(!response.ok)throw Error('status');
      const data=await response.json(),sample=data.sample||{};history=data.history||[];
      const on=data.telemetry_fresh&&sample.online;
      setText(fields.online,data.telemetry_fresh?(on?'Включена':'Выключена'):'Нет связи');
      const statusCard=llmPanel.querySelector('[data-llm-chart=online]');statusCard.classList.toggle('is-online',!!on);statusCard.classList.toggle('is-offline',!on);
      setText(fields.vram_used_mb,sample.vram_used_mb==null?'—':`${metric(sample.vram_used_mb)} / ${metric(sample.vram_total_mb)} МиБ`);
      setText(fields.gpu_percent,sample.gpu_percent==null?'—':metric(sample.gpu_percent)+'%');
      setText(fields.generation_tps,metric(sample.generation_tps));setText(fields.prefill_tps,metric(sample.prefill_tps));
      const latest=history.at(-1);setText('llm-sampled-at',latest&&data.telemetry_fresh?'Замер '+new Date(latest.at).toLocaleTimeString('ru'):'Нет свежего замера');
      const command=data.command||{};pending=command.state==='pending';
      const note=!data.telemetry_fresh?'Локальный монитор недоступен. Управление моделью появится после восстановления связи.':
        pending?'Команда передана локальному компьютеру. Активная проверка будет сохранена в контрольной точке.':
        command.state==='failed'?'Не удалось выполнить команду: '+(command.message||'причина неизвестна'):
        sample.note||'Замеры раз в минуту. Скорости — по последнему рабочему запросу.';
      setText('llm-monitor-note',note);
      const controls=document.getElementById('llm-controls');if(controls)for(const button of controls.querySelectorAll('[data-llm-action]')){
        const action=button.dataset.llmAction;button.disabled=!data.telemetry_fresh||pending||(action==='start'?on:!on);
      }
      draw();
    }catch(error){setText('llm-monitor-note','Не удалось получить показатели LLM.');}
    finally{busy=false;timer=setTimeout(refresh,pending?5000:60000);}
  }
  llmPanel.querySelectorAll('[data-llm-chart]').forEach(button=>button.addEventListener('click',()=>{
    const selected=button.dataset.llmChart;opened=opened===selected?'':selected;
    llmPanel.querySelectorAll('[data-llm-chart]').forEach(item=>item.setAttribute('aria-expanded',String(item.dataset.llmChart===opened)));draw();
  }));
  llmPanel.querySelectorAll('[data-llm-action]').forEach(button=>button.addEventListener('click',async()=>{
    const action=button.dataset.llmAction;button.disabled=true;
    try{const response=await fetch(llmPanel.dataset.actionUrl,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify({action})});
      const value=await response.json();if(!response.ok)throw Error(value.error||'Команда не принята');
      setText('llm-monitor-note','Команда передана. Текущая задача будет завершена и сохранена перед переключением модели.');
    }catch(error){setText('llm-monitor-note',error.message);}finally{refresh();}
  }));
  refresh();
}

const stateLabels={waiting:'Ожидает локального обработчика',preparing:'Чтение и планирование',running:'Проверяется',paused:'Приостановлена',partial:'Завершена с непроверенными областями',completed:'Проверка завершена',failed:'Ошибка выполнения',cancelled:'Отменена'};
const stageLabels={language:'Грамотность',logic:'Техническая логика',sto:'Требования СТО',cross:'Связи разделов',inter:'Связи документов',verify:'Перепроверка'};
let findingFilter='confirmed';
let liveFindings=[];
let liveTaskErrors=[];
let findingDispositions={};
let selectedFinding='';
const findingTypeLabels={sto:'СТО',grammar:'Грамотность',formatting:'Оформление',logic:'Техническая логика',cross:'Межраздельная логика',inter:'Междокументная логика',arithmetic:'Арифметика',other:'Прочее'};
function findingType(category){const value=String(category||'').toLocaleLowerCase('ru');if(value.includes('сто')||['sto','нормативное нарушение'].includes(value))return 'sto';if(value.includes('грамот')||value.includes('граммат')||['language','grammar'].includes(value))return 'grammar';if(value.includes('оформлен'))return 'formatting';if(value.includes('междокумент'))return 'inter';if(value.includes('межраздел'))return 'cross';if(value.includes('арифмет')||value.includes('числов'))return 'arithmetic';if(value.includes('логик')||value==='logic')return 'logic';return 'other';}
function updateFindingTypes(types){const select=document.getElementById('finding-type');if(!select)return;const current=select.value,available=new Set(types.map(item=>item.value));select.replaceChildren(new Option('Все типы',''));for(const item of types)select.add(new Option(item.label,item.value));select.value=available.has(current)?current:'';}
function updateRegisterExports(){const panel=document.getElementById('findings-register');if(!panel?.dataset.exportUrl)return;const query=document.getElementById('finding-search')?.value.trim()||'',type=document.getElementById('finding-type')?.value||'';for(const link of panel.querySelectorAll('[data-export-format]')){const url=new URL(panel.dataset.exportUrl,window.location.href);url.searchParams.set('status',findingFilter);if(type&&findingFilter!=='task-error')url.searchParams.set('type',type);if(query)url.searchParams.set('q',query);url.searchParams.set('format',link.dataset.exportFormat);link.href=url.toString();}}

function node(tag,className,text){const element=document.createElement(tag);if(className)element.className=className;if(text!==undefined)element.textContent=text;return element;}

async function submitFeedback(form){
  const status=form.querySelector('[role=status]');
  try{
    const response=await fetch(form.dataset.url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':form.csrfmiddlewaretoken.value},body:JSON.stringify({finding_id:form.dataset.finding,comment:form.comment.value})});
    status.textContent=response.ok?'Комментарий сохранён для перепроверки':'Не удалось сохранить комментарий';
    if(response.ok)form.comment.value='';
  }catch(error){status.textContent='Нет связи с сервером';}
}
document.addEventListener('submit',event=>{const form=event.target.closest('.review-feedback');if(!form)return;event.preventDefault();submitFeedback(form);});

function findingText(finding){return [finding.issue,finding.category,finding.explanation,...(finding.evidence||[]).flatMap(e=>[e.document,e.address,e.quote])].join(' ').toLocaleLowerCase('ru');}
function shortAddress(finding){const evidence=(finding.evidence||[])[0]||{};return evidence.address||evidence.locator||evidence.document||'Место уточняется';}
function csrf(){return document.querySelector('[name=csrfmiddlewaretoken]')?.value||'';}
function renderFindingDetail(finding){
  const box=document.getElementById('finding-detail');if(!box)return;box.replaceChildren();
  const article=node('article','finding-detail-card'),meta=node('div','finding-meta');meta.append(node('span','badge '+(finding.severity||''),finding.category||'Замечание'),node('span','',finding.severity||''));article.append(meta,node('h3','',finding.issue||'Замечание'),node('p','',finding.explanation||''));
  for(const evidence of finding.evidence||[]){article.append(node('div','finding-address',evidence.address||''),node('blockquote','',evidence.quote||''));}
  const suggestion=node('p');suggestion.append(node('strong','','Предложение: '),document.createTextNode(finding.suggestion||''));article.append(suggestion);
  if(finding.source){const source=document.createElement('details');source.className='finding-source';source.append(node('summary','','Основание СТО'),node('p','muted',`${finding.source.document_name||''} · ${finding.source.clause||''} · ${finding.source.source_locator||''}`));article.append(source);}
  if(finding.verification){const verification=document.createElement('details');verification.className='finding-source';verification.append(node('summary','','Результат перепроверки'),node('p','muted',finding.verification));article.append(verification);}
  if(reviewProgress?.dataset.canManage==='0'){
    const disposition=findingDispositions[finding.id];
    if(disposition?.author)article.append(node('p','muted',`Решение специалиста: ${disposition.state||''} · ${disposition.author}`));
    box.append(article);return;
  }
  const disposition=findingDispositions[finding.id]||{state:'new',comment:''};const workflow=node('div','finding-workflow');workflow.append(node('strong','','Решение специалиста'));
  const states=[['new','Не рассмотрено'],['in_work','В работу'],['fixed','Исправлено'],['disputed','Не согласен']];
  for(const [value,label] of states){const button=node('button','button '+(disposition.state===value?'primary':'subtle'),label);button.type='button';button.onclick=async()=>{let comment='';if(value==='disputed'){comment=window.prompt('Укажите обоснование несогласия',disposition.comment||'')||'';if(comment.length<8)return;}const url=reviewProgress.dataset.dispositionUrl.replace('__FINDING__',encodeURIComponent(finding.id));const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify({state:value,comment})});if(!response.ok)return;findingDispositions[finding.id]={...(await response.json()),comment};drawFindingRegister();};workflow.append(button);}
  if(disposition.author)workflow.append(node('small','muted',`${disposition.author} · ${disposition.updated?new Date(disposition.updated).toLocaleString('ru'):''}`));article.append(workflow);
  const details=node('details','feedback-box'),summary=node('summary','','Отправить замечание модели на перепроверку'),form=node('form','review-feedback');form.dataset.url=reviewProgress.dataset.feedbackUrl;form.dataset.finding=finding.id||'';const token=document.createElement('input');token.type='hidden';token.name='csrfmiddlewaretoken';token.value=csrf();form.append(token);const textarea=node('textarea','feedback-text');textarea.name='comment';textarea.minLength=8;textarea.maxLength=6000;textarea.required=true;textarea.placeholder='Укажите, что следует перепроверить, и приведите доказательство.';const button=node('button','button secondary','Отправить');button.type='submit';const status=node('span','muted');status.setAttribute('role','status');form.append(textarea,button,status);details.append(summary,form);article.append(details);box.append(article);
}

function renderTaskErrorDetail(error){
  const box=document.getElementById('finding-detail');if(!box)return;box.replaceChildren();
  const article=node('article','finding-detail-card task-error-detail'),meta=node('div','finding-meta');
  meta.append(node('span','badge major','Ошибка задачи'),node('span','',stageLabels[error.stage]||error.stage||'Конвейер'));
  article.append(meta,node('h3','',error.stage==='system'?'Ошибка конвейера':`Не выполнен этап «${stageLabels[error.stage]||error.stage||'не определён'}»`));
  const id=node('p','muted',`Задача: ${error.id||'не указан'} · попыток: ${error.attempts||0} · состояние: ${error.state||'failed'}`);
  const reason=node('pre','task-error-message',error.error||'Причина не записана');
  const note=node('div','notice compact error');note.append(node('span','notice-symbol','!'),node('div','', 'Результат этой задачи не учитывается как успешно проверенная область. Исправьте причину и повторите пакет или соответствующий этап.'));
  article.append(id,node('h3','','Техническая причина'),reason,note);box.append(article);
}

function drawFindingRegister(){
  const container=document.getElementById('live-findings');if(!container)return;const query=(document.getElementById('finding-search')?.value||'').trim().toLocaleLowerCase('ru');
  const type=document.getElementById('finding-type')?.value||'';updateRegisterExports();
  if(findingFilter==='task-error'){
    const visible=liveTaskErrors.filter(item=>!query||[item.id,item.stage,stageLabels[item.stage],item.error].join(' ').toLocaleLowerCase('ru').includes(query));container.replaceChildren();
    if(!visible.some(item=>'error:'+item.id===selectedFinding))selectedFinding=visible[0]?'error:'+visible[0].id:'';
    for(const error of visible){const row=node('button','finding-row task-error-row'+('error:'+error.id===selectedFinding?' selected':''));row.type='button';row.append(node('span','finding-row-top'),node('strong','',stageLabels[error.stage]||error.stage||'Ошибка конвейера'),node('small','',error.error||'Причина не записана'));row.firstChild.append(node('span','badge major','Ошибка задачи'),node('span','disposition-state',`${error.attempts||0} попыток`));row.onclick=()=>{selectedFinding='error:'+error.id;drawFindingRegister();};container.append(row);}
    const chosen=visible.find(item=>'error:'+item.id===selectedFinding);if(chosen)renderTaskErrorDetail(chosen);else document.getElementById('finding-detail')?.replaceChildren(node('div','empty-inline','Ошибок задач нет.'));
    const empty=document.getElementById('no-live-findings');if(empty){empty.hidden=visible.length!==0;empty.textContent='Ошибок задач нет.';}return;
  }
  const visible=liveFindings.filter(f=>(findingFilter==='all'||f.status===findingFilter||(findingFilter==='candidate'&&f.status==='verifying'))&&(!type||findingType(f.category)===type)&&(!query||findingText(f).includes(query)));container.replaceChildren();
  if(!visible.some(f=>f.id===selectedFinding))selectedFinding=visible[0]?.id||'';
  for(const finding of visible){const row=node('button','finding-row'+(finding.id===selectedFinding?' selected':' '));row.type='button';row.dataset.findingStatus=finding.status||'candidate';const top=node('span','finding-row-top');top.append(node('span','badge '+(finding.severity||''),finding.category||'Замечание'),node('span','disposition-state',({'new':'Не рассмотрено','in_work':'В работе','fixed':'Исправлено','disputed':'Не согласен'})[findingDispositions[finding.id]?.state||'new']));row.append(top,node('strong','',finding.issue||'Замечание'),node('small','',shortAddress(finding)));row.onclick=()=>{selectedFinding=finding.id;drawFindingRegister();};container.append(row);}
  if(findingFilter==='all')for(const error of liveTaskErrors){const row=node('button','finding-row task-error-row'+('error:'+error.id===selectedFinding?' selected':''));row.type='button';row.append(node('span','finding-row-top'),node('strong','',stageLabels[error.stage]||error.stage||'Ошибка конвейера'),node('small','',error.error||'Причина не записана'));row.firstChild.append(node('span','badge major','Ошибка задачи'));row.onclick=()=>{selectedFinding='error:'+error.id;drawFindingRegister();};container.append(row);}
  const chosen=visible.find(f=>f.id===selectedFinding),chosenError=liveTaskErrors.find(e=>'error:'+e.id===selectedFinding);
  if(chosen)renderFindingDetail(chosen);else if(chosenError)renderTaskErrorDetail(chosenError);else document.getElementById('finding-detail')?.replaceChildren(node('div','empty-inline','Выберите замечание слева.'));
  const empty=document.getElementById('no-live-findings');if(empty){empty.hidden=visible.length!==0||(findingFilter==='all'&&liveTaskErrors.length!==0);empty.textContent='По выбранным условиям записей нет.';}
}

const registerPanel=document.getElementById('findings-register');
let registerPage=1,registerRequest=0,registerSignature='',registerLoadedAt=0;
async function loadRegister(){
  if(!registerPanel?.dataset.registerUrl)return;
  const current=++registerRequest,url=new URL(registerPanel.dataset.registerUrl,window.location.href);
  url.searchParams.set('status',findingFilter);url.searchParams.set('page',registerPage);
  const type=document.getElementById('finding-type')?.value||'',query=document.getElementById('finding-search')?.value.trim()||'';
  if(type&&findingFilter!=='task-error')url.searchParams.set('type',type);if(query)url.searchParams.set('q',query);
  try{
    const response=await fetch(url);if(!response.ok)throw new Error('Реестр пока недоступен');const data=await response.json();if(current!==registerRequest)return;
    registerPanel.querySelectorAll('.register-exports [data-export-format]').forEach(link=>link.hidden=!data.report_available);
    liveFindings=data.records.filter(item=>item.kind==='finding').map(item=>item.value);
    liveTaskErrors=data.records.filter(item=>item.kind==='task-error').map(item=>item.value);
    findingDispositions=data.dispositions||{};updateFindingTypes(data.types||[]);
    registerPage=data.page;registerLoadedAt=Date.now();
    const label=document.getElementById('register-page-label');if(label)label.textContent=`Найдено ${data.total} · страница ${data.page} из ${data.pages}`;
    const previous=document.getElementById('register-previous'),next=document.getElementById('register-next');if(previous)previous.disabled=data.page<=1;if(next)next.disabled=data.page>=data.pages;
    drawFindingRegister();
  }catch(error){if(current===registerRequest){const label=document.getElementById('register-page-label');if(label)label.textContent='Реестр появится после начала проверки';}}
}

function applyFindingFilter(){
  drawFindingRegister();
}
function selectRegisterFilter(filter,scroll=false){
  findingFilter=filter;registerPage=1;selectedFinding='';const type=document.getElementById('finding-type');if(type){type.disabled=filter==='task-error';type.closest('label').hidden=filter==='task-error';}document.querySelectorAll('[data-finding-filter]').forEach(item=>{const selected=item.dataset.findingFilter===filter;item.classList.toggle('selected',selected);item.setAttribute('aria-selected',selected?'true':'false');});updateRegisterExports();loadRegister();
  if(scroll)document.getElementById('findings-register')?.scrollIntoView({behavior:'smooth',block:'start'});
}
document.querySelectorAll('[data-finding-filter]').forEach(button=>button.addEventListener('click',()=>selectRegisterFilter(button.dataset.findingFilter)));
document.querySelectorAll('[data-register-filter]').forEach(button=>button.addEventListener('click',()=>selectRegisterFilter(button.dataset.registerFilter,true)));
applyFindingFilter();
let registerSearchTimer;
document.getElementById('finding-search')?.addEventListener('input',()=>{registerPage=1;updateRegisterExports();clearTimeout(registerSearchTimer);registerSearchTimer=setTimeout(loadRegister,250);});
document.getElementById('finding-type')?.addEventListener('change',()=>{registerPage=1;updateRegisterExports();loadRegister();});
document.getElementById('register-previous')?.addEventListener('click',()=>{if(registerPage>1){registerPage--;loadRegister();}});
document.getElementById('register-next')?.addEventListener('click',()=>{registerPage++;loadRegister();});
loadRegister();

const historyToggle=document.getElementById('batch-history-toggle');
if(historyToggle){
  const extraRows=[...document.querySelectorAll('#batch-history-rows tr[data-history-extra]')];
  const count=document.getElementById('batch-history-count');
  historyToggle.addEventListener('click',()=>{
    const expanded=historyToggle.getAttribute('aria-expanded')!=='true';
    extraRows.forEach(row=>{row.hidden=!expanded;});
    historyToggle.setAttribute('aria-expanded',String(expanded));
    document.getElementById('batch-history')?.classList.toggle('is-expanded',expanded);
    historyToggle.querySelector('.batch-history-toggle-label').textContent=expanded?'Свернуть список':`Показать ещё ${historyToggle.dataset.extra}`;
    count.textContent=expanded?`Показаны все ${historyToggle.dataset.total} проверок`:`Показаны 3 из ${historyToggle.dataset.total} проверок`;
  });
}

const reviewProgress=document.getElementById('review-progress');
if(reviewProgress){
  let reviewActive=false,lastWake=0;
  const wake=async force=>{if(reviewProgress.dataset.canManage==='0'||!reviewActive||!reviewProgress.dataset.wakeUrl)return;if(!force&&Date.now()-lastWake<600000)return;const response=await fetch(reviewProgress.dataset.wakeUrl,{method:'POST',headers:{'X-CSRFToken':csrf()}});if(response.ok)lastWake=Date.now();};
  const refresh=async()=>{
    try{
      const response=await fetch(reviewProgress.dataset.url);if(!response.ok)return;
      const data=await response.json(),snapshot=data.snapshot||{},findings=snapshot.findings||{};const wasActive=reviewActive;reviewActive=['preparing','running'].includes(data.state);if(reviewActive)wake(!wasActive).catch(()=>{});
      const state=document.getElementById('review-state');state.textContent=(stateLabels[data.state]||data.state)+(data.stale?' · нет свежего сигнала обработчика':'');
      document.querySelectorAll('[data-states]').forEach(button=>button.hidden=!button.dataset.states.split(' ').includes(data.state));
      const report=document.getElementById('live-report-link');if(report)report.hidden=!data.report_available;
      let total=0,done=0,failed=0;const stageValues=Object.entries(snapshot.stages||{});
      for(const [,value] of stageValues){total+=Object.values(value).reduce((sum,item)=>sum+(Number(item)||0),0);done+=(value.done||0)+(value.split||0)+(value.failed||0);failed+=value.failed||0;}
      const stageBox=document.getElementById('review-stages');stageBox.replaceChildren();
      for(const [key,value] of stageValues){const count=Object.values(value).reduce((sum,item)=>sum+(Number(item)||0),0),stage=node('div','review-stage '+((value.running||value.done)?'active':''));stage.append(node('strong','',stageLabels[key]||key),node('span','',count?`${value.done||0} из ${count}`:(['completed','partial','failed','cancelled'].includes(data.state)?'Не требовалось':'Ещё не запланировано')));if(value.running)stage.append(node('small','','выполняется'));if(value.failed)stage.append(node('small','stage-error',`ошибок: ${value.failed}`));stageBox.append(stage);}
      const bar=document.getElementById('review-progress-bar');if(bar)bar.style.width=(total?Math.min(100,done/total*100):0).toFixed(1)+'%';
      const percent=total?Math.min(100,done/total*100):(['completed','partial'].includes(data.state)?100:0),rounded=Math.round(percent);
      const currentRing=document.getElementById('current-review-ring');if(currentRing)currentRing.style.setProperty('--progress',percent.toFixed(1));
      const currentPercent=document.getElementById('current-review-percent');if(currentPercent)currentPercent.textContent=rounded+'%';
      const currentState=document.getElementById('current-review-button-state');if(currentState)currentState.textContent=stateLabels[data.state]||data.state;
      const currentButton=document.getElementById('current-review-button');if(currentButton)currentButton.classList.toggle('is-active',['waiting','preparing','running'].includes(data.state));
      const progressText=document.getElementById('review-progress-text');if(progressText)progressText.textContent=`Завершено ${done} из ${total} задач${snapshot.eta_provisional?' · план уточняется':''}`;
      const eta=document.getElementById('review-eta');if(eta)eta.textContent=snapshot.eta_seconds?`${snapshot.eta_provisional?'Предварительно: ':'Осталось: '}${Math.ceil(snapshot.eta_seconds[0]/60)}–${Math.ceil(snapshot.eta_seconds[1]/60)} мин`:(['completed','partial','failed','cancelled'].includes(data.state)?'Обработка остановлена или завершена':'Прогноз появится после первых задач');
      const counts=document.getElementById('review-counts');if(counts)counts.textContent=`Подтверждено: ${findings.confirmed||0} · Кандидаты: ${(findings.candidate||0)+(findings.verifying||0)} · Вопросы: ${findings.question||0}`;
      for(const [id,value] of [['count-confirmed',findings.confirmed||0],['count-candidate',(findings.candidate||0)+(findings.verifying||0)],['count-question',findings.question||0],['count-failed',failed]]){const element=document.getElementById(id);if(element)element.textContent=value;}
      const current=document.getElementById('review-current');if(current){const running=(snapshot.running||[]).map(task=>`${stageLabels[task.stage]||task.stage}, ${Math.floor(task.seconds||0)} с`).join('; '),heartbeat=data.heartbeat?new Date(data.heartbeat).toLocaleTimeString('ru'):'нет данных';current.textContent=snapshot.fatal_error||(running?'Сейчас: '+running:`Последнее продвижение: ${heartbeat}`);}
      const signature=JSON.stringify([data.state,findings,failed]);if(registerPanel&&(signature!==registerSignature||Date.now()-registerLoadedAt>30000)){registerSignature=signature;loadRegister();}
    }catch(error){const state=document.getElementById('review-state');if(state)state.textContent='Не удалось обновить состояние';}
  };
  refresh();setInterval(refresh,7000);setInterval(()=>wake(false).catch(()=>{}),60000);
}

document.querySelectorAll('[data-reveal]').forEach(button=>button.addEventListener('click',()=>{const input=document.getElementById(button.dataset.reveal);const show=input.type==='password';input.type=show?'text':'password';button.textContent=show?'Скрыть':'Показать';button.setAttribute('aria-label',show?'Скрыть пароль':'Показать пароль');}));

const input=document.getElementById('documents');
if(input){let selected=[];const list=document.getElementById('file-list'),error=document.getElementById('upload-error'),drop=document.getElementById('dropzone'),form=document.getElementById('batch-form');
  const render=()=>{list.replaceChildren();const dt=new DataTransfer();selected.forEach((file,index)=>{dt.items.add(file);const row=node('div','file-item'),label=node('span','',file.name),size=node('small','',(file.size/1024/1024).toFixed(1)+' МБ'),remove=node('button','', 'Удалить');remove.type='button';remove.setAttribute('aria-label','Удалить '+file.name);remove.onclick=()=>{selected.splice(index,1);render();};row.append(label,size,remove);list.append(row);});input.files=dt.files;};
  const add=files=>{error.textContent='';for(const file of files){if(!/\.docx?$/i.test(file.name)){error.textContent='Добавляйте документы Word .doc или .docx.';continue;}if(file.size>50*1024*1024){error.textContent='Файл '+file.name+' превышает 50 МБ.';continue;}if(selected.length>=20||selected.reduce((sum,item)=>sum+item.size,0)+file.size>100*1024*1024){error.textContent='Лимит пакета: 20 документов и 100 МБ.';break;}if(!selected.some(item=>item.name===file.name&&item.size===file.size))selected.push(file);}render();};
  input.addEventListener('change',()=>add(Array.from(input.files)));['dragenter','dragover'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.add('drag-over');}));['dragleave','drop'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove('drag-over');}));drop.addEventListener('drop',event=>add(Array.from(event.dataTransfer.files)));form.addEventListener('submit',event=>{if(!selected.length){event.preventDefault();error.textContent='Добавьте хотя бы один документ.';return;}const button=document.getElementById('save-batch');button.disabled=true;button.textContent='Загружаем документы…';});
}
