const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
test('saved Office POST links become downloads; duplicate appendix is hidden, permissions paths remain internal',()=>{
 const url='/normcontol/knowledge/normative-sets/area/sources/source/open/';
 const c={id:'chat',messages:[{role:'assistant',text:'Вывод [S1] и [S2].\n\nИсточники из RAG:\n[S1] Документ — [открыть источник](https://portal.example'+url+')\n[S2] Документ — ссылка\n\nОграничения поиска: Пример.',metadata:{kind:'model',state:'done',rag_sources:[{label:'S1',name:'Документ',locator:'p62',url},{label:'S2',name:'Документ',locator:'t1/r1/c3',url}]}}]};
 const row=app(c).element('messages').children[0],sources=row.children.find(n=>n.className==='chat-rag-sources');
 assert.equal(row.children[1].textContent,'Вывод [S1] и [S2].\n\nОграничения поиска: Пример.');
 assert.equal(sources.children.length,2);
 assert.equal(sources.children[1].href,url.replace('/knowledge/','/api/v2/').replace('/open/','/download/'));
 assert.match(sources.children[1].textContent,/абзац 62/);assert.match(sources.children[1].textContent,/таблица 1, строка 1, ячейка 3/);
 assert.equal(app({id:'chat',messages:[{role:'user',text:c.messages[0].text,metadata:{}}]}).element('messages').children[0].children[1].textContent,c.messages[0].text);
});
test('RAG links are shown only for cited internal sources and titles remain plain text',()=>{
 const c={id:'chat',messages:[{role:'assistant',text:'Вывод [S1].',metadata:{kind:'model',state:'done',rag_sources:[{label:'S1',name:'<script>title</script>',locator:'4.2',url:'/normcontol/knowledge/areas/fixture/'},{label:'S2',name:'Unused',url:'/normcontol/knowledge/areas/unused/'},{label:'S1',name:'External',url:'https://outside.test/'}]}}]};
 const row=app(c).element('messages').children[0],sources=row.children.find(n=>n.className==='chat-rag-sources');
 assert.equal(sources.children.length,2);assert.equal(sources.children[1].textContent,'[S1] <script>title</script> · 4.2');
 assert.equal(sources.children[1].href,'/normcontol/knowledge/areas/fixture/');assert.equal(sources.children[1].rel,'noopener');
});
function app(conversation=null){const nodes=new Map(),timers=new Map(),intervals=new Map(),requests=[],events={};let serial=0,clock=0;
const element=id=>{if(nodes.has(id))return nodes.get(id);const e={hidden:false,value:'',placeholder:'Проверь документы',textContent:'',dataset:{},children:[],get firstChild(){return this.children[0];},attrs:{},files:[],scrollHeight:0,scrollTop:0,clientHeight:0,classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k]||null;},removeAttribute(k){delete this.attrs[k];},focus(){},append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;},querySelectorAll(){return [];},addEventListener(k,f){this[k]=f;},getBoundingClientRect(){return {height:72};}};nodes.set(id,e);return e;};
element('chat-initial').textContent=JSON.stringify({conversation,choices:{checks:[],norms:[]},presentation:'chat'});element('history').hidden=true;
const document={documentElement:{dataset:{uiUser:'1'}},body:{classList:{toggle(){}}},getElementById:element,querySelector:s=>s==='.chat-topbar'?element('topbar'):{value:'csrf'},createElement:tag=>element(tag+ ++serial),addEventListener(){}};
const context={document,window:{addEventListener(k,f){events[k]=f;}},performance:{now:()=>clock},localStorage:{getItem(){},setItem(){}},crypto:{randomUUID:()=> 'key'},URL,location:{href:'http://localhost/normcontol/chat/'},history:{pushState(){},replaceState(){}},ResizeObserver:class{observe(){}},FormData:class{},setTimeout:f=>{timers.set(++serial,f);return serial;},clearTimeout:id=>timers.delete(id),setInterval(f,ms){intervals.set(ms,f);},AbortController,fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),console};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/chat.js'),'utf8'),context);return {element,requests,events,clockNode:()=>[...nodes.values()].find(x=>x.className==='chat-elapsed'),tick(ms){clock+=ms;intervals.get(1000)();},poll(){intervals.get(7000)();},runTimers(){const pending=[...timers.values()];timers.clear();pending.forEach(f=>f());},async respond(i,data){requests[i].resolve({ok:true,json:async()=>data});for(let n=0;n<10;n++)await Promise.resolve();}};}
test('closed history stays closed after delayed response',async()=>{const a=app();for(let n=0;n<20;n++)a.element('history-toggle').onclick();assert.equal(a.element('history').hidden,true);assert.equal(a.requests.length,1);await a.respond(0,{items:[]});assert.equal(a.element('history').hidden,true);a.element('history-toggle').onclick();a.element('history-close').onclick();await a.respond(1,{items:[]});assert.equal(a.element('history').hidden,true);});
test('rapid switches coalesce and serialize writes, final click wins',async()=>{const a=app();for(let n=0;n<20;n++){a.element('expert-toggle').onclick();a.events['normcontrol-theme']({detail:n%2?'light':'dark'});}assert.equal(a.element('expert-toggle').attrs['aria-checked'],'false');assert.equal(a.element('bottom').hidden,false);a.runTimers();assert.equal(a.requests.length,1);assert.deepEqual(JSON.parse(a.requests[0].options.body),{presentation:'chat',theme:'light'});a.element('expert-toggle').onclick();a.events['normcontrol-theme']({detail:'dark'});a.runTimers();assert.equal(a.requests.length,1);await a.respond(0,{});assert.equal(a.requests.length,2);assert.deepEqual(JSON.parse(a.requests[1].options.body),{presentation:'expert',theme:'dark'});await a.respond(1,{});assert.equal(a.element('expert-toggle').attrs['aria-checked'],'true');});
test('elapsed clock ticks locally, persists after reload, preserves controls on polling and stops',async()=>{
 const c={id:'review',batch:'batch',messages:[{role:'assistant',text:'Проверяется',metadata:{kind:'progress'}}],state:{actions:[],terminal:false,clock:{known:true,seconds:214,live:true,label:'Работает уже'}}};
 const a=app(c),row=a.element('messages').children[0],clock=a.clockNode();assert.equal(clock.textContent,'Работает уже 3 мин 34 с');
 a.tick(1000);assert.equal(clock.textContent,'Работает уже 3 мин 35 с');assert.equal(a.requests.length,0);
 a.poll();await a.respond(0,{...c,state:{...c.state,clock:{...c.state.clock,seconds:221}}});assert.equal(a.element('messages').children[0],row);assert.equal(clock.textContent,'Работает уже 3 мин 41 с');
 assert.equal(app({...c,state:{...c.state,clock:{...c.state.clock,seconds:3601}}}).clockNode().textContent,'Работает уже 1 ч 0 мин 1 с');
 const paused=app({...c,state:{...c.state,clock:{...c.state.clock,seconds:221,live:false,label:'Приостановлено; прошло'}}});paused.tick(30000);assert.equal(paused.clockNode().textContent,'Приостановлено; прошло 3 мин 41 с');
});
test('DOC warning follows accepted attachments, removal and expert transfer',()=>{
 const a=app();assert.equal(a.element('doc-review-warning').hidden,true);
 a.element('documents').onchange({target:{files:[{name:'report.docx',size:10}]}});assert.equal(a.element('doc-review-warning').hidden,true);
 a.element('documents').onchange({target:{files:[{name:'old.DOC',size:10}]}});assert.equal(a.element('doc-review-warning').hidden,false);assert.equal(a.requests.length,0);
 a.element('attachments').children[1].children[2].onclick();assert.equal(a.element('doc-review-warning').hidden,true);
 a.element('chat-main').ondrop({preventDefault(){},dataTransfer:{files:[{name:'legacy.doc',size:10}]}});assert.equal(a.element('doc-review-warning').hidden,false);
 a.element('expert-toggle').onclick();a.element('expert-frame').contentDocument={getElementById(id){return id==='launch-form'?{elements:{namedItem(){return null;}},querySelectorAll(){return [];}}:id==='documents'?{files:[{name:'new.docx',size:10}]}:null;}};
 a.element('expert-toggle').onclick();assert.equal(a.element('doc-review-warning').hidden,true);
});
