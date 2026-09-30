(() => {
 'use strict';
 const root=document.documentElement,key='normcontrol-sidebar-'+(root.dataset.uiUser||'anonymous');
 if(new URL(location.href).searchParams.get('embedded')==='1')root.dataset.sidebar='expanded';
 else try{root.dataset.sidebar=localStorage.getItem(key)==='collapsed'?'collapsed':'expanded';}catch(_){root.dataset.sidebar='expanded';}
 document.addEventListener('DOMContentLoaded',()=>{
  const button=document.getElementById('system-sidebar-toggle'),panel=document.getElementById('system-sidebar');
  if(!button||!panel)return;
  panel.querySelectorAll('.nav-link').forEach(link=>{const name=link.textContent.trim();link.title=name;link.setAttribute('aria-label',name);});
  function update(){const collapsed=root.dataset.sidebar==='collapsed';button.textContent=collapsed?'☰':'⇤';button.title=collapsed?'Развернуть системную панель':'Свернуть системную панель';button.setAttribute('aria-label',button.title);button.setAttribute('aria-expanded',String(!collapsed));const brand=document.getElementById('brand-sidebar-toggle');if(brand){brand.title=button.title;brand.setAttribute('aria-label',button.title);brand.setAttribute('aria-expanded',String(!collapsed));}}
  const toggle=()=>{root.dataset.sidebar=root.dataset.sidebar==='collapsed'?'expanded':'collapsed';try{localStorage.setItem(key,root.dataset.sidebar);}catch(_){}update();};button.addEventListener('click',toggle);document.getElementById('brand-sidebar-toggle')?.addEventListener('click',toggle);
  update();
 });
})();
