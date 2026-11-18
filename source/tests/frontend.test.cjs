const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

class Element{
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.value='';this.hidden=false;this.disabled=false;this.checked=false;this.dataset={};this.childNodes=[];this.handlers={};this.files=[];this.textContent='';}
  append(...nodes){this.childNodes.push(...nodes);}
  replaceChildren(...nodes){this.childNodes=[...nodes];}
  get options(){return this.childNodes;}
  addEventListener(type,fn){this.handlers[type]=fn;}
  setAttribute(key,value){this[key]=value;}
  removeAttribute(key){delete this[key];}
  emit(type){return this.handlers[type]?.({preventDefault(){}});}
}
function browser(script){
  const nodes=new Map(),calls=[],notices=[],downloads=[];
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  $('workspace').hidden=true;$('comp-market').value='AE';$('look-market').value='AE';$('look-consent').checked=true;
  const el=(tag,cls,text)=>{const e=new Element(tag);e.className=cls;e.textContent=text||'';return e;};
  const sandbox={$,el,cell:(tr,text)=>{const td=el('td','',text);tr.append(td);return td;},currentRole:'editor',currentStore:'default',
    Option:function(text,value){this.textContent=text;this.value=value;},localTime:x=>x||'',
    document:{querySelectorAll:()=>[]},window:{addEventListener(){}},MutationObserver:class{observe(){}},
    setTimeout,clearTimeout,Blob,URL:{createObjectURL:file=>'blob:'+file.name,revokeObjectURL(){}},
    FileReader:class{readAsDataURL(){this.result='data:image/png;base64,AA==';queueMicrotask(()=>this.onload());}},
    notify:(message,error)=>notices.push({message,error}),confirm:()=>true,
    saveDownloadBlob:(blob,name)=>downloads.push({blob,name}),csvCell:value=>'"'+String(value??'').replaceAll('"','""')+'"',
    request:async()=>({history:[]}),postJSON:async(url,body)=>{calls.push({url,body});return sandbox.reply(url,body);},
    reply:()=>{throw new Error('Unexpected API call');}};
  const ctx=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web',script),'utf8'),ctx);
  return {ctx,$,calls,notices,downloads,sandbox,run:code=>vm.runInContext(code,ctx)};
}

test('all pending scoring sends 20 at a time with no overlap',async()=>{
  const b=browser('competitors.js');let active=0,maxActive=0;const sizes=[],progress=[];
  const completed=await b.ctx.compScoreBatches(Array.from({length:45},(_,i)=>String(i)),async batch=>{
    active++;maxActive=Math.max(active,maxActive);sizes.push(batch.length);await Promise.resolve();active--;
    return {ai_summary:{completed:batch.length,failed:0}};
  },done=>progress.push(done),()=>false);
  assert.equal(completed,45);assert.deepEqual(sizes,[20,20,5]);assert.equal(maxActive,1);assert.equal(progress.at(-1),45);
});

test('stopping scoring finishes only the current batch; partial failure stops the queue',async()=>{
  const b=browser('competitors.js');let stop=false,calls=0;
  const completed=await b.ctx.compScoreBatches(Array(45).fill('A'),async batch=>{
    calls++;stop=true;return {ai_summary:{completed:batch.length,failed:0}};
  },()=>{},()=>stop);
  assert.equal(completed,20);assert.equal(calls,1);
  calls=0;await assert.rejects(b.ctx.compScoreBatches(Array(45).fill('A'),async()=>{
    calls++;return {ai_summary:{completed:19,failed:1}};
  },()=>{},()=>false));assert.equal(calls,1);
});

test('photo queue waits for keyword confirmation, keeps results by image, and exports both',async()=>{
  const b=browser('lookalike.js');
  let keywordCalls=0;
  b.sandbox.reply=(url,body)=>{
    if(url.endsWith('/keywords'))return {token:'token-'+(++keywordCalls),marketplace:'AE',description:'中性笔',brand:'',keywords:['black pen']};
    if(url.endsWith('/search'))return {keywords:body.keywords,count:1,errors:[],items:[{asin:'B000TEST01',title:'Pen',image:'',found_by:['black pen'],evidence:[],verdict:'',offers:null}]};
    if(url.endsWith('/compare'))return {items:[{asin:'B000TEST01',verdict:'same',similarity:95}]};
    if(url.endsWith('/prices'))return {items:[{asin:'B000TEST01',offers:{count:0,list:[],own_stores:[],lowest:null}}]};
    throw new Error(url);
  };
  b.$('look-file').files=['front.png','side.png'].map(name=>({name,type:'image/png',size:100}));
  b.$('look-file').emit('change');assert.equal(b.run('lookQueue.length'),2);
  await b.$('look-read').emit('click');
  assert.equal(b.calls.length,1);assert.ok(b.calls[0].url.endsWith('/keywords'));
  assert.equal(b.$('look-keywords').value,'black pen');
  await b.$('look-search').emit('click');
  assert.deepEqual(Array.from(b.calls[1].body.keywords),['black pen']);
  b.run("look.items[0].image='https://m.media-amazon.com/image.png'");
  await b.$('look-compare').emit('click');await b.$('look-prices').emit('click');
  b.$('look-next').emit('click');assert.equal(b.run('look.token'),undefined);
  await b.$('look-read').emit('click');await b.$('look-search').emit('click');
  assert.equal(b.run('lookQueue[0].items[0].verdict'),'same');
  assert.equal(b.run('lookQueue[1].items[0].verdict'),'');
  assert.equal(b.$('look-rows').childNodes.length,4);
  b.$('look-export').emit('click');const csv=await b.downloads[0].blob.text();
  assert.ok(csv.includes('哪张图'));assert.ok(csv.includes('front.png'));assert.ok(csv.includes('side.png'));
  assert.equal(b.notices.filter(n=>n.error).length,0);
});

test('more than ten photos is rejected before creating jobs',()=>{
  const b=browser('lookalike.js');b.$('look-file').files=Array.from({length:11},(_,i)=>({name:i+'.png',type:'image/png',size:100}));
  b.$('look-file').emit('change');assert.equal(b.run('lookQueue.length'),0);assert.equal(b.calls.length,0);
  assert.ok(b.notices[0].error);
});

test('opening saved history clears the old file input and displays readonly keywords',async()=>{
  const b=browser('lookalike.js');b.$('look-file').value='old-image.png';
  b.$('look-history-list').value='saved';
  b.sandbox.request=async()=>({id:'saved',title:'历史',can_delete:false,groups:[{name:'pen.png',marketplace:'AE',
    keywords:['black pen'],description:'黑色笔',queried_at:'2026-11-17T10:00:00Z',items:[]}]});
  await b.$('look-history-open').emit('click');
  await new Promise(setImmediate);
  assert.equal(b.$('look-file').value,'');assert.equal(b.$('look-keywords').value,'black pen');
  assert.equal(b.$('look-keywords').readOnly,true);assert.equal(b.$('look-step2').hidden,false);
  assert.equal(b.$('look-search').disabled,true);assert.equal(b.$('look-history-delete').hidden,true);
});
