(() => {
  const form=document.getElementById('launch-form');if(!form)return;
  const input=document.getElementById('documents'),list=document.getElementById('file-list'),error=document.getElementById('upload-error');
  const button=document.getElementById('launch-submit'),readiness=document.getElementById('launch-readiness'),summary=document.getElementById('launch-error-summary');
  const scopes=JSON.parse(document.getElementById('launch-normative-scopes').textContent),experiences=JSON.parse(document.getElementById('launch-experience-scopes').textContent);
  let selected=[],submitting=false;const existing=Number(form.dataset.existingCount||0);
  const checked=name=>Array.from(form.querySelectorAll(`input[name="${name}"]:checked`));
  const messageNode=(tag,content)=>{const item=document.createElement(tag);item.textContent=content;return item;};
  const update=()=>{
    const directions=checked('checks'),sto=directions.some(x=>x.value==='sto'),norms=checked('normative_sets');
    document.getElementById('launch-document-count').textContent=existing+selected.length;
    document.getElementById('launch-direction-count').textContent=directions.length;
    const selectedDirections=document.getElementById('launch-selected-directions');selectedDirections.replaceChildren(...directions.map(x=>messageNode('li',x.closest('label').textContent.trim())));
    const normSection=document.getElementById('launch-normatives'),normCount=document.getElementById('launch-norm-count');
    if(normSection)normSection.hidden=!sto;if(normCount){normCount.textContent=norms.length;document.getElementById('launch-norm-count-row').hidden=!sto;}
    document.getElementById('launch-without-sto').hidden=sto;
    const chosenScopes=new Set(norms.map(x=>scopes[x.value]));const experience=form.elements.namedItem('experience')?.value;
    let problem='';
    if(!existing&&!selected.length)problem='Добавьте хотя бы один документ.';
    else if(!directions.length)problem='Выберите хотя бы одно направление.';
    else if(sto&&normSection&&!norms.length)problem='Для СТО выберите нормативный набор.';
    else if(sto&&experience&&!chosenScopes.has(experiences[experience]))problem='Выберите опыт рецензий из той же проектной области.';
    readiness.textContent=problem||'Готово к запуску. Проверьте выбранные направления.';readiness.classList.toggle('launch-readiness-error',!!problem);
    if(button){button.disabled=submitting||!!problem;button.title=problem;}
    return !problem;
  };
  if(input){
    const render=()=>{const dt=new DataTransfer();list.replaceChildren();selected.forEach((file,index)=>{
      dt.items.add(file);const row=messageNode('div','');row.className='file-item';const sizeText=file.size<1024*1024?`${Math.max(1,Math.round(file.size/1024))} КБ`:`${(file.size/1024/1024).toFixed(1)} МБ`;const name=messageNode('span',file.name),size=messageNode('small',sizeText),remove=messageNode('button','Удалить');
      remove.type='button';remove.setAttribute('aria-label','Удалить '+file.name);remove.addEventListener('click',()=>{selected.splice(index,1);render();});row.append(name,size,remove);list.append(row);
    });input.files=dt.files;update();};
    const add=files=>{const problems=[];for(const file of files){
      if(!/\.docx?$/i.test(file.name)){problems.push(file.name+': нужен DOC или DOCX.');continue;}
      if(file.size>50*1024*1024){problems.push(file.name+': больше 50 МБ.');continue;}
      if(selected.some(x=>x.name===file.name&&x.size===file.size&&x.lastModified===file.lastModified))continue;
      if(selected.length>=20||selected.reduce((total,x)=>total+x.size,0)+file.size>100*1024*1024){problems.push('Лимит пакета: 20 документов и 100 МБ.');continue;}
      selected.push(file);
    }error.textContent=problems.join(' ');render();};
    input.addEventListener('change',()=>add(Array.from(input.files)));const drop=document.getElementById('dropzone');
    ['dragenter','dragover'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.add('drag-over');}));
    ['dragleave','drop'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove('drag-over');}));
    drop.addEventListener('drop',event=>add(Array.from(event.dataTransfer.files)));add(Array.from(input.files));
  }
  form.addEventListener('change',update);update();
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(submitting||!update())return;
    form.querySelectorAll('[data-launch-error-field]').forEach(x=>x.replaceChildren());summary.hidden=true;summary.replaceChildren();
    submitting=true;form.setAttribute('aria-busy','true');const original=button.innerHTML;button.disabled=true;button.textContent='Сохраняем и запускаем…';
    try{
      const response=await fetch(form.action||location.href,{method:'POST',body:new FormData(form),credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest','Accept':'application/json'}});
      if(response.redirected)throw new Error('Сессия или состояние пакета изменились. Обновите страницу; выбранные файлы пока остаются в форме.');
      const payload=await response.json().catch(()=>null);
      if(response.ok&&payload?.next){location.assign(payload.next);return;}
      if(!payload?.errors)throw new Error(response.status===413?'Пакет слишком велик для загрузки. Уменьшите размер файлов.':'Не удалось отправить пакет. Проверьте связь и повторите нажатие — второй пакет создан не будет.');
      let firstField=null;for(const [field,errors] of Object.entries(payload.errors)){
        const target=Array.from(form.querySelectorAll('[data-launch-error-field]')).find(x=>x.dataset.launchErrorField===field);
        const container=target||summary;for(const item of errors){const text=messageNode('p',item.message);text.className='launch-server-error';container.append(text);}
        const details=target?.closest('details');if(details)details.open=true;
        if(target&&!firstField)firstField=form.elements.namedItem(field);
      }
      summary.prepend(messageNode('strong','Проверка не запущена. Исправьте отмеченные поля.'));summary.hidden=false;
      if(firstField instanceof Element)firstField.setAttribute('aria-invalid','true');summary.focus();summary.scrollIntoView({behavior:'auto',block:'center'});
    }catch(failure){summary.textContent=failure.name==='TypeError'?'Не удалось связаться с сервером. Повторите нажатие: выбранные файлы сохранены, второй пакет создан не будет.':failure.message;summary.hidden=false;summary.focus();}
    finally{submitting=false;form.removeAttribute('aria-busy');button.innerHTML=original;update();}
  });
})();
