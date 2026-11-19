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
function browser(script,extra){
  const nodes=new Map(),calls=[],notices=[],downloads=[];
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  $('workspace').hidden=true;$('look-market').value='AE';$('look-consent').checked=true;
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
  if(extra)Object.assign(sandbox,extra({$,sandbox,calls,notices}));
  const ctx=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web',script),'utf8'),ctx);
  return {ctx,$,calls,notices,downloads,sandbox,run:code=>vm.runInContext(code,ctx)};
}

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

// --- store analysis, offers watch, inventory generation ---
// A tiny selector for the checkbox lists these pages read back.
function checkboxQuery($){
  return sel=>{
    const m=sel.match(/^#([\w-]+) input\[type=checkbox\](:checked)?$/);if(!m)return [];
    const out=[],walk=node=>{for(const c of node.childNodes||[])if(c instanceof Element){if(c.tagName==='INPUT'&&c.type==='checkbox'&&(!m[2]||c.checked))out.push(c);walk(c);}};
    walk($(m[1]));return out;
  };
}
function storePage(script,more){
  return browser(script,({$})=>({
    document:{getElementById:$,querySelectorAll:checkboxQuery($)},window:{},
    withButton:async(button,label,work)=>{if(button.disabled)return;button.disabled=true;try{await work();}catch(e){/* shown by the page */}finally{button.disabled=false;}},
    saveCsv(){},reportChip:()=>'',...(more?more($):{})}));
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const tick=()=>new Promise(setImmediate);
const reviewOrders=n=>({marketplace:'AE',truncated:false,orders:Array.from({length:n},(_,i)=>({id:'111-0000000-'+String(i).padStart(7,'0'),purchased:'2026-11-01',channel:'AFN',total:{amount:'',currency:''},done:'',last_not_eligible:''}))});
const reviewBoxes=b=>b.$('reviews-rows').childNodes.map(tr=>tr.childNodes[0].childNodes[0]);

test('review requests cannot be sent twice while a send is running',async()=>{
  const b=storePage('insights.js');const pending=[];
  b.sandbox.reply=(url,body)=>{if(url.endsWith('/reviews/orders'))return reviewOrders(25);const d=deferred();pending.push({body,d});return d.promise;};
  await b.$('reviews-load').emit('click');
  for(const box of reviewBoxes(b))box.checked=true;
  const first=b.$('reviews-send').emit('click');await tick();
  assert.equal(pending.length,1);
  assert.equal(b.$('reviews-send').disabled,true);assert.equal(b.$('reviews-all').disabled,true);assert.equal(b.$('reviews-load').disabled,true);
  assert.ok(reviewBoxes(b).every(box=>box.disabled&&box.checked));
  await b.$('reviews-send').emit('click');await tick();
  assert.equal(pending.length,1);
  pending[0].d.resolve({sent:20,items:pending[0].body.orders.map(id=>({id,result:'sent'}))});await tick();
  assert.equal(pending.length,2);assert.equal(pending[1].body.orders.length,5);
  pending[1].d.resolve({sent:4,items:pending[1].body.orders.map((id,i)=>({id,result:i?'sent':'not_eligible'}))});await first;
  const boxes=reviewBoxes(b);
  assert.equal(boxes.filter(box=>!box.disabled).length,1);   // only the not-eligible one can be picked again
  assert.equal(b.$('reviews-send').disabled,true);assert.equal(b.$('reviews-load').disabled,false);
  assert.ok(b.notices.at(-1).message.includes('24'));
});

test('switching store in the middle of a review send stops cleanly and reports what was sent',async()=>{
  const b=storePage('insights.js');const pending=[];
  b.sandbox.reply=(url,body)=>{if(url.endsWith('/reviews/orders'))return reviewOrders(45);const d=deferred();pending.push({body,d});return d.promise;};
  await b.$('reviews-load').emit('click');
  for(const box of reviewBoxes(b))box.checked=true;
  const send=b.$('reviews-send').emit('click');await tick();
  assert.equal(pending[0].body.store,'default');assert.equal(pending[0].body.marketplace,b.$('insights-market').value);
  pending[0].d.resolve({sent:20,items:pending[0].body.orders.map(id=>({id,result:'sent'}))});await tick();
  b.sandbox.window.resetInsights();     // store switch while the second batch is on its way
  pending[1].d.resolve({sent:20,items:pending[1].body.orders.map(id=>({id,result:'sent'}))});
  await send;
  assert.equal(pending.length,2);       // the third batch is never sent
  assert.equal(b.$('reviews-result').hidden,true);
  const last=b.notices.at(-1);assert.ok(last.message.includes('40'),last.message);assert.ok(last.message.includes('5 个没有发送'),last.message);
  assert.equal(b.$('reviews-send').disabled,true);assert.equal(b.$('reviews-load').disabled,false);
});

test('orders Amazon once called not eligible stay selectable; sent orders do not',async()=>{
  const b=storePage('insights.js');const data=reviewOrders(3);
  data.orders[0].done='sent';data.orders[1].last_not_eligible='2026-11-10T00:00:00+00:00';
  b.sandbox.reply=()=>data;await b.$('reviews-load').emit('click');
  assert.deepEqual(reviewBoxes(b).map(box=>box.disabled),[true,false,false]);
  assert.ok(b.$('reviews-status').textContent.includes('2 个还没发送过'));
});

test('a profit answer that arrives after a store switch is dropped',async()=>{
  const b=storePage('insights.js');const d=deferred();b.sandbox.reply=()=>d.promise;
  const run=b.$('profit-run').emit('click');await tick();
  b.sandbox.window.resetInsights();
  d.resolve({marketplace:'AE',days:30,currency:'AED',items:[],cost_missing:[],totals:{},vat_rate:5,vat_included:true});await run;
  assert.equal(b.$('profit-result').hidden,true);assert.equal(b.run('typeof state'),'undefined');
});

test('offers watch and restock drop replies for the previous store',async()=>{
  const b=storePage('watch.js');const pending=[];
  b.sandbox.reply=(url,body)=>{const d=deferred();pending.push({url,body,d});return d.promise;};
  b.$('watch-asins').value='B000TEST01';b.$('watch-market').value='AE';
  const watch=b.$('watch-run').emit('click');await tick();
  b.sandbox.window.resetWatch();
  pending[0].d.resolve({marketplace:'AE',items:[],counts:{}});await watch;
  assert.equal(b.$('watch-result').hidden,true);assert.equal(b.$('watch-status').textContent,'');
  b.$('restock-market').value='AE';b.$('restock-days').value='30';b.$('restock-lead').value='30';b.$('restock-cover').value='30';
  const restock=b.$('restock-run').emit('click');await tick();
  assert.equal(pending[1].body.store,'default');
  b.sandbox.window.resetWatch();
  pending[1].d.resolve({id:'r1'});await restock;
  assert.equal(pending.length,2);       // no polling for the old store's report
});

function inventoryPage(status){
  let loads=0;
  const b=browser('inventory-generate.js',()=>({amazonStatus:null,storeEpoch:1,busy:false,inventoryReport:null,
    loadAmazonStatus:async()=>{loads++;b.sandbox.amazonStatus=status;return true;},
    storeList:()=>{throw new Error('the status, not the store list, names the store');},
    renderInventory(){},categoryResults:()=>[],load:async()=>{},updateInventoryShopOptions(){},inspectedFile:null}));
  b.loads=()=>loads;return b;
}

test('generated tables for 通用模板 go to the connected main store, read on demand after a reload',async()=>{
  const b=inventoryPage({store:'default',name:'阿联酋主店',configured:true});
  assert.equal(await b.run("invGenTargetShop({shop:'通用模板'})"),'阿联酋主店');
  assert.equal(b.loads(),1);
  assert.equal(await b.run("invGenTargetShop({shop:'二店'})"),'二店');
  assert.equal(b.run('invGenStoreName()'),'阿联酋主店');
});

test('no rules are downloaded when the table has nowhere to go or the store changed',async()=>{
  const b=inventoryPage({store:'default',name:'主店',configured:false,missing:['Refresh Token']});
  b.sandbox.reply=(url)=>{throw new Error('unexpected '+url);};
  b.run("invGenEntry('笔').chosen='PEN'");
  await assert.rejects(b.run("invGenSave({shop:'通用模板',marketplace:'AE'},{category:'笔'},storeEpoch)"),/还没有连接亚马逊店铺/);
  b.sandbox.amazonStatus={store:'s1111111',name:'二店',configured:true};
  await assert.rejects(b.run("invGenSave({shop:'通用模板',marketplace:'AE'},{category:'笔'},storeEpoch-1)"),/店铺已切换/);
  assert.equal(b.calls.length,0);
});
