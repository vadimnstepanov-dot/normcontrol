'use strict';
(()=>{
if(document.body.dataset.embedded||window.self!==window.top)return;
const csrf=()=>document.querySelector('[name=csrfmiddlewaretoken]')?.value||'';
const llmPanel=document.getElementById('llm-monitor');
if(llmPanel){
  const labels={online:'Работа модели',vram_used_mb:'Память GPU, МиБ',gpu_percent:'Загрузка GPU, %',cpu_percent:'Загрузка CPU, %',ram_used_mb:'Оперативная память, МиБ',generation_tps:'Генерация, ток/с',prefill_tps:'Prefill, ток/с',uptime_seconds:'Время работы модели, с'};
  const fields={online:'llm-online',vram_used_mb:'llm-vram',gpu_percent:'llm-gpu',cpu_percent:'llm-cpu',ram_used_mb:'llm-ram',generation_tps:'llm-generation',prefill_tps:'llm-prefill'};
  let history=[],opened='',timer=null,busy=false,uptimeBase=null,uptimeAt=0,commandSending=false;
  const setText=(id,value)=>{const element=document.getElementById(id);if(element)element.textContent=value;};
  const metric=value=>typeof value==='number'?new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(value):'—';
  const speed=value=>llmPanel.closest('.chat-topbar')&&typeof value==='number'&&value>=1000?metric(value/1000)+'к':metric(value);
  const duration=value=>{if(typeof value!=='number')return '—';const seconds=Math.max(0,Math.floor(value)),days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60);return days?`${days} д ${hours} ч`:`${String(hours).padStart(2,'0')}:${String(minutes).padStart(2,'0')}`;};
  function tickUptime(){const elapsed=uptimeBase===null?null:uptimeBase+(Date.now()-uptimeAt)/1000;setText('llm-online-uptime',elapsed===null?'—':duration(elapsed));setText('llm-uptime-detail',elapsed===null?'—':`${duration(elapsed)} (${Math.floor(elapsed/60)} мин)`);const model=llmPanel.querySelector('[data-llm-chart="online"]'),name=document.getElementById('llm-model-label')?.textContent||'Модель';if(model)model.title=elapsed===null?`${name} · выключена или статус недоступен`:`${name} · включена · работает ${duration(elapsed)}`;}
  const gauge=(key,value,color)=>{const button=llmPanel.querySelector(`[data-llm-chart="${key}"]`);if(!button)return;button.style.setProperty('--gauge',String(Math.max(0,Math.min(100,value||0))));if(color)button.style.setProperty('--gauge-color',color);};
  function closeChart(){opened='';draw();llmPanel.querySelectorAll('[data-llm-chart]').forEach(item=>item.setAttribute('aria-expanded','false'));}
  const svgNode=(svg,tag,attributes,value)=>{const element=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,item] of Object.entries(attributes))element.setAttribute(key,String(item));if(value!==undefined)element.textContent=value;svg.append(element);return element;};
  const clock=value=>new Date(value).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  function timeAxis(svg,left){
    const end=Math.max(Date.now(),Date.parse(history.at(-1)?.at)||0),start=end-7200000;
    for(let index=0;index<=4;index++){
      const x=left+(700-left)*index/4,at=start+(end-start)*index/4;
      svgNode(svg,'line',{x1:x,x2:x,y1:14,y2:145,class:'grid-line time-grid'});
      svgNode(svg,'text',{x,y:171,'text-anchor':index===0?'start':index===4?'end':'middle',class:'axis-label'},clock(at));
    }
    return at=>left+(700-left)*Math.max(0,Math.min(1,(at-start)/(end-start)));
  }
  function drawStateChart(svg){
    const xAt=timeAxis(svg,153),states=[['Вкл с нагрузкой',27,'var(--accent)'],['Вкл без нагрузки',79,'var(--green)'],['Выкл',131,'var(--red)']];
    for(const [label,y,color] of states){svgNode(svg,'line',{x1:153,x2:700,y1:y,y2:y,class:'grid-line'});svgNode(svg,'text',{x:4,y:y+4,fill:color,class:'state-label'},label);}
    const state=item=>item.online===false?0:item.online===true&&item.processing===true?2:item.online===true&&item.processing===false?1:null;
    const levels={0:131,1:79,2:27},colors={0:'var(--red)',1:'var(--green)',2:'var(--accent)'};
    let previous=null,shown=0,unclassified=0;
    for(const point of history){
      const at=Date.parse(point.at),level=state(point);if(!Number.isFinite(at))continue;
      if(level===null){if(point.online===true)unclassified++;previous=null;continue;}
      if(previous&&at-previous.at<=150000){
        const from=xAt(previous.at),to=xAt(at);
        svgNode(svg,'line',{x1:from,x2:to,y1:levels[previous.level],y2:levels[previous.level],stroke:colors[previous.level],class:'state-line'});
        if(previous.level!==level)svgNode(svg,'line',{x1:to,x2:to,y1:levels[previous.level],y2:levels[level],class:'state-transition'});
      }
      const dot=svgNode(svg,'circle',{cx:xAt(at),cy:levels[level],r:3.5,fill:colors[level]});svgNode(dot,'title',{},`${clock(at)} — ${states[2-level][0]}`);
      previous={at,level};shown++;
    }
    if(!shown)svgNode(svg,'text',{x:420,y:83,'text-anchor':'middle',class:'axis-label'},'Нет классифицированных замеров');
    setText('llm-chart-context',unclassified?'Старые замеры без признака нагрузки пропущены; состояния уточняются по новым замерам.':'Состояние определяется по занятости слота модели. Интервал замера — 1 минута.');
    setText('llm-chart-min','');setText('llm-chart-max','');
  }
  function drawNumericChart(svg){
    const xAt=timeAxis(svg,52),values=history.map(item=>item[opened]);
    const finite=values.filter(value=>typeof value==='number'&&Number.isFinite(value));
    if(!finite.length){setText('llm-chart-min','Нет замеров');setText('llm-chart-max','');return;}
    const minimum=Math.min(...finite),maximum=Math.max(...finite),range=maximum-minimum||1;
    for(let y=25;y<=125;y+=50)svgNode(svg,'line',{x1:52,x2:700,y1:y,y2:y,class:'grid-line'});
    let path='',connected=false,previousAt=null;
    history.forEach((item,index)=>{const value=values[index],at=Date.parse(item.at);if(typeof value!=='number'||!Number.isFinite(value)||!Number.isFinite(at)){connected=false;return;}if(previousAt!==null&&at-previousAt>150000)connected=false;const x=xAt(at),y=130-(value-minimum)/range*110;path+=(connected?' L':' M')+x.toFixed(1)+','+y.toFixed(1);connected=true;previousAt=at;});
    svgNode(svg,'path',{d:path.trim(),class:'data-line'});
    setText('llm-chart-min',opened==='uptime_seconds'?duration(minimum):metric(minimum));setText('llm-chart-max',opened==='uptime_seconds'?duration(maximum):metric(maximum));
    setText('llm-chart-context','Интервал замера — 1 минута. Пропуски связи не соединяются линией.');
  }
  function draw(){
    const box=document.getElementById('llm-chart'),svg=document.getElementById('llm-chart-svg');box.hidden=!opened;
    if(!opened)return;
    setText('llm-chart-title',labels[opened]);svg.replaceChildren();box.classList.toggle('is-state-chart',opened==='online');
    if(opened==='online')drawStateChart(svg);else drawNumericChart(svg);
  }
  async function refresh(){
    if(busy)return;busy=true;clearTimeout(timer);
    let pending=false;
    try{
      const response=await fetch(llmPanel.dataset.statusUrl,{cache:'no-store'});if(!response.ok)throw Error('status');
      const data=await response.json(),sample=data.sample||{};history=data.history||[];
      const on=!!(data.telemetry_fresh&&sample.online);
      setText('llm-model-label',sample.model_label||'Модель');
      setText(fields.online,data.telemetry_fresh?(on?'ON':'OFF'):'—');
      const statusCard=llmPanel.querySelector('[data-llm-chart=online]');statusCard.classList.toggle('is-online',!!on);statusCard.classList.toggle('is-offline',!on);
      const vramPercent=sample.vram_total_mb?sample.vram_used_mb/sample.vram_total_mb*100:null;
      setText(fields.vram_used_mb,vramPercent==null?'—':Math.round(vramPercent)+'%');
      setText(fields.gpu_percent,sample.gpu_percent==null?'—':Math.round(sample.gpu_percent)+'%');
      setText(fields.generation_tps,speed(sample.generation_tps));setText(fields.prefill_tps,speed(sample.prefill_tps));
      setText('llm-profile',!on?'Выключена':sample.processing===true?'Включена · с нагрузкой':sample.processing===false?'Включена · без нагрузки':'Включена · нагрузка неизвестна');
      setText('llm-vram-detail',sample.vram_used_mb==null?'—':`${metric(sample.vram_used_mb)} / ${metric(sample.vram_total_mb)} МиБ`);
      setText('llm-gpu-detail',sample.gpu_percent==null?'—':`${metric(sample.gpu_percent)}%`);
      setText('llm-cpu-detail',sample.cpu_percent==null?'—':`${metric(sample.cpu_percent)}%`);
      setText('llm-ram-detail',sample.ram_used_mb==null?'—':`${metric(sample.ram_used_mb)} / ${metric(sample.ram_total_mb)} МиБ`);
      setText(fields.cpu_percent,sample.cpu_percent==null?'—':Math.round(sample.cpu_percent)+'%');
      const ramPercent=sample.ram_total_mb?sample.ram_used_mb/sample.ram_total_mb*100:null;
      setText(fields.ram_used_mb,ramPercent==null?'—':Math.round(ramPercent)+'%');
      setText('llm-generation-detail',sample.generation_tps==null?'—':`${metric(sample.generation_tps)} ток/с`);
      setText('llm-prefill-detail',sample.prefill_tps==null?'—':`${metric(sample.prefill_tps)} ток/с`);
      uptimeBase=on&&typeof sample.uptime_seconds==='number'?sample.uptime_seconds:null;uptimeAt=Date.now();tickUptime();
      gauge('online',on?100:0,on?'var(--green)':'var(--amber)');gauge('vram_used_mb',vramPercent,vramPercent>92?'var(--red)':vramPercent>80?'var(--amber)':'var(--violet)');
      gauge('gpu_percent',sample.gpu_percent,sample.gpu_percent>90?'var(--amber)':'var(--accent)');
      gauge('cpu_percent',sample.cpu_percent,sample.cpu_percent>90?'var(--amber)':'var(--violet)');
      gauge('ram_used_mb',ramPercent,ramPercent>90?'var(--red)':ramPercent>80?'var(--amber)':'var(--green)');
      for(const [key,color] of [['generation_tps','var(--green)'],['prefill_tps','var(--accent)']]){
        const peak=Math.max(0,...history.map(point=>Number(point[key])||0));gauge(key,peak?Number(sample[key]||0)/peak*100:0,color);
        const measured=typeof sample.timing_at==='number'?` · запрос завершён ${clock(sample.timing_at*1000)}`:'';
        llmPanel.querySelector(`[data-llm-chart="${key}"]`).title=`${labels[key]}${measured}; дуга показывает долю от максимума за последние 120 минут`;
      }
      const latest=history.at(-1);setText('llm-sampled-at',latest&&data.telemetry_fresh?'Замер '+new Date(latest.at).toLocaleTimeString('ru'):'Нет свежего замера');
      const command=data.command||{};pending=command.state==='pending';
      const note=!data.telemetry_fresh?'Локальный монитор недоступен. Управление моделью появится после восстановления связи.':
        pending?'Команда передана локальному компьютеру. Активная проверка будет сохранена в контрольной точке.':
        command.state==='failed'?'Не удалось выполнить команду: '+(command.message||'причина неизвестна'):
        sample.note||'Замеры раз в минуту. Скорости — по последнему рабочему запросу.';
      setText('llm-monitor-note',note);
      const controls=document.getElementById('llm-controls');if(controls)for(const button of controls.querySelectorAll('[data-llm-action]')){
        const action=button.dataset.llmAction;button.disabled=!data.telemetry_fresh||pending||commandSending||(action==='start'?on:action==='stop'?!on:false);
      }
      draw();
    }catch(error){setText('llm-monitor-note','Не удалось получить показатели LLM.');llmPanel.querySelectorAll('[data-llm-action]').forEach(button=>button.disabled=true);uptimeBase=null;tickUptime();}
    finally{busy=false;timer=setTimeout(refresh,pending?5000:60000);}
  }
  llmPanel.querySelectorAll('[data-llm-chart]').forEach(button=>button.addEventListener('click',()=>{
    const selected=button.dataset.llmChart;opened=opened===selected?'':selected;
    llmPanel.querySelectorAll('[data-llm-chart]').forEach(item=>item.setAttribute('aria-expanded',String(item.dataset.llmChart===opened)));draw();
  }));
  document.getElementById('llm-chart-close')?.addEventListener('click',closeChart);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&opened)closeChart();});
  document.addEventListener('pointerdown',event=>{if(opened&&!llmPanel.contains(event.target))closeChart();});
  llmPanel.querySelectorAll('[data-llm-action]').forEach(button=>button.addEventListener('click',async()=>{
    if(commandSending)return;commandSending=true;
    const action=button.dataset.llmAction;llmPanel.querySelectorAll('[data-llm-action]').forEach(item=>item.disabled=true);
    try{const response=await fetch(llmPanel.dataset.actionUrl,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify({action})});
      const value=await response.json();if(!response.ok)throw Error(value.error||'Команда не принята');
      setText('llm-monitor-note','Команда передана. Текущая задача будет завершена и сохранена перед переключением модели.');
    }catch(error){setText('llm-monitor-note',error.message);}finally{commandSending=false;refresh();}
  }));
  refresh();setInterval(tickUptime,1000);
}

})();
