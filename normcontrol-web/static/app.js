'use strict';

const stateLabels={waiting:'Ожидает локального обработчика',preparing:'Чтение и планирование',running:'Проверяется',paused:'Приостановлена',partial:'Завершена с непроверенными областями',completed:'Проверка завершена',failed:'Ошибка выполнения',cancelled:'Отменена'};
const stageLabels={language:'Грамотность',logic:'Техническая логика',sto:'Требования СТО',cross:'Связи разделов',inter:'Связи документов',verify:'Перепроверка'};
let findingFilter='confirmed';
let liveFindings=[];
let findingDispositions={};
let selectedFinding='';

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
  const disposition=findingDispositions[finding.id]||{state:'new',comment:''};const workflow=node('div','finding-workflow');workflow.append(node('strong','','Решение специалиста'));
  const states=[['new','Не рассмотрено'],['in_work','В работу'],['fixed','Исправлено'],['disputed','Не согласен']];
  for(const [value,label] of states){const button=node('button','button '+(disposition.state===value?'primary':'subtle'),label);button.type='button';button.onclick=async()=>{let comment='';if(value==='disputed'){comment=window.prompt('Укажите обоснование несогласия',disposition.comment||'')||'';if(comment.length<8)return;}const url=reviewProgress.dataset.dispositionUrl.replace('__FINDING__',encodeURIComponent(finding.id));const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify({state:value,comment})});if(!response.ok)return;findingDispositions[finding.id]={...(await response.json()),comment};drawFindingRegister();};workflow.append(button);}
  if(disposition.author)workflow.append(node('small','muted',`${disposition.author} · ${disposition.updated?new Date(disposition.updated).toLocaleString('ru'):''}`));article.append(workflow);
  const details=node('details','feedback-box'),summary=node('summary','','Отправить замечание модели на перепроверку'),form=node('form','review-feedback');form.dataset.url=reviewProgress.dataset.feedbackUrl;form.dataset.finding=finding.id||'';const token=document.createElement('input');token.type='hidden';token.name='csrfmiddlewaretoken';token.value=csrf();form.append(token);const textarea=node('textarea','feedback-text');textarea.name='comment';textarea.minLength=8;textarea.maxLength=6000;textarea.required=true;textarea.placeholder='Укажите, что следует перепроверить, и приведите доказательство.';const button=node('button','button secondary','Отправить');button.type='submit';const status=node('span','muted');status.setAttribute('role','status');form.append(textarea,button,status);details.append(summary,form);article.append(details);box.append(article);
}

function drawFindingRegister(){
  const container=document.getElementById('live-findings');if(!container)return;const query=(document.getElementById('finding-search')?.value||'').trim().toLocaleLowerCase('ru');
  const visible=liveFindings.filter(f=>(f.status===findingFilter||(findingFilter==='candidate'&&f.status==='verifying'))&&(!query||findingText(f).includes(query)));container.replaceChildren();
  if(!visible.some(f=>f.id===selectedFinding))selectedFinding=visible[0]?.id||'';
  for(const finding of visible){const row=node('button','finding-row'+(finding.id===selectedFinding?' selected':' '));row.type='button';row.dataset.findingStatus=finding.status||'candidate';const top=node('span','finding-row-top');top.append(node('span','badge '+(finding.severity||''),finding.category||'Замечание'),node('span','disposition-state',({'new':'Не рассмотрено','in_work':'В работе','fixed':'Исправлено','disputed':'Не согласен'})[findingDispositions[finding.id]?.state||'new']));row.append(top,node('strong','',finding.issue||'Замечание'),node('small','',shortAddress(finding)));row.onclick=()=>{selectedFinding=finding.id;drawFindingRegister();};container.append(row);}
  const chosen=visible.find(f=>f.id===selectedFinding);if(chosen)renderFindingDetail(chosen);else document.getElementById('finding-detail')?.replaceChildren(node('div','empty-inline','Выберите замечание слева.'));
  const empty=document.getElementById('no-live-findings');if(empty)empty.hidden=visible.length!==0;
}

function applyFindingFilter(){
  drawFindingRegister();
}
document.querySelectorAll('[data-finding-filter]').forEach(button=>button.addEventListener('click',()=>{findingFilter=button.dataset.findingFilter;document.querySelectorAll('[data-finding-filter]').forEach(item=>item.classList.toggle('selected',item===button));applyFindingFilter();}));
applyFindingFilter();
document.getElementById('finding-search')?.addEventListener('input',drawFindingRegister);

const reviewProgress=document.getElementById('review-progress');
if(reviewProgress){
  let reviewActive=false,lastWake=0;
  const wake=async force=>{if(!reviewActive||!reviewProgress.dataset.wakeUrl)return;if(!force&&Date.now()-lastWake<600000)return;const response=await fetch(reviewProgress.dataset.wakeUrl,{method:'POST',headers:{'X-CSRFToken':csrf()}});if(response.ok)lastWake=Date.now();};
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
      findingDispositions=data.dispositions||{};if(Array.isArray(data.findings_preview)){liveFindings=data.findings_preview;drawFindingRegister();}
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
