'use strict';

(function(){
  const key='normcontrol-theme';
  let theme='dark';
  try{
    const saved=window.localStorage.getItem(key);
    if(saved==='light'||saved==='dark')theme=saved;
  }catch(error){}
  document.documentElement.dataset.theme=theme;

  function update(button){
    const light=document.documentElement.dataset.theme==='light';
    button.querySelector('.theme-toggle-icon').textContent=light?'☾':'☀';
    button.querySelector('.theme-toggle-label').textContent=light?'Тёмная':'Светлая';
    button.setAttribute('aria-label',light?'Включить тёмную тему':'Включить светлую тему');
  }

  document.addEventListener('DOMContentLoaded',()=>{
    const button=document.querySelector('[data-theme-toggle]');
    if(!button)return;
    update(button);
    button.addEventListener('click',()=>{
      const theme=document.documentElement.dataset.theme==='light'?'dark':'light';
      document.documentElement.dataset.theme=theme;
      try{window.localStorage.setItem(key,theme);}catch(error){}
      update(button);
    });
  });
})();
