'use strict';

(function(){
  const user=document.documentElement.dataset.uiUser||'anonymous';
  const key='normcontrol-theme:'+user;
  let theme='dark';
  try{
    const saved=window.localStorage.getItem(key)||document.documentElement.dataset.savedTheme||window.localStorage.getItem('normcontrol-theme');
    if(saved==='light'||saved==='dark')theme=saved;
  }catch(error){}
  document.documentElement.dataset.theme=theme;
  window.addEventListener('storage',e=>{if(e.key===key&&(e.newValue==='light'||e.newValue==='dark')){document.documentElement.dataset.theme=e.newValue;document.querySelectorAll('[data-theme-toggle]').forEach(update);}});

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
      window.dispatchEvent(new CustomEvent('normcontrol-theme',{detail:theme}));
      update(button);
    });
  });
})();
