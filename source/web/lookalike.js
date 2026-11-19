// Photo -> the same product on Amazon: four visible steps, each one started by the user.
const LOOK_VERDICTS={same:'同款',similar:'相似',different:'不同'};
let look=null,lookQueue=[],lookIndex=0,lookBusy=false,lookEpoch=0,lookHistory=null;
const lookCanUse=()=>currentRole!=='viewer';
function lookReset(keepImage){
  for(const entry of lookQueue)if(entry.preview)URL.revokeObjectURL(entry.preview);
  lookQueue=[];lookIndex=0;
  lookHistory=null;
  $('look-queue').replaceChildren();
  lookEpoch++;look=null;lookBusy=false;
  $('look-keywords').value='';$('look-description').textContent='';$('look-rows').replaceChildren();
  $('look-status').textContent='';$('look-results').hidden=true;
  if(!keepImage){$('look-file').value='';$('look-preview').hidden=true;$('look-preview').removeAttribute('src');}
  lookControls();
}
function lookControls(){
  const has=!!look?.file,items=look?.items||[];
  $('look-editor').hidden=!lookCanUse();$('look-viewer-note').hidden=lookCanUse();
  $('look-read').disabled=lookBusy||!has||!$('look-consent').checked;
  $('look-search').disabled=lookBusy||!look?.token||!$('look-keywords').value.trim();
  $('look-compare').disabled=lookBusy||!look?.token||!items.length||!$('look-consent').checked;
  $('look-prices').disabled=lookBusy||!look?.token||!items.some(i=>i.verdict==='same'||i.verdict==='similar'||i.picked);
  $('look-export').disabled=lookBusy||!lookQueue.some(entry=>entry.items.length);
  $('look-next').hidden=lookQueue.length<2;
  $('look-next').disabled=lookBusy||lookIndex>=lookQueue.length-1;
  $('look-file').disabled=lookBusy;
  $('look-history-save').disabled=lookBusy||!lookCanUse()||!lookQueue.some(entry=>entry.token&&entry.confirmed);
  $('look-history-delete').hidden=!lookHistory?.can_delete;
  $('look-history-delete').disabled=lookBusy;
  $('look-history-open').disabled=lookBusy||!$('look-history-list').value;
  for(const kind of ['csv','xlsx','json'])$('look-history-'+kind).disabled=lookBusy||!$('look-history-list').value;
  for(const button of document.querySelectorAll('#look-queue button'))button.disabled=lookBusy;
  $('look-market').disabled=lookBusy;
  $('look-step2').hidden=!look?.token&&!look?.keywords;$('look-step3').hidden=!items.length;
  $('look-keywords').readOnly=!look?.token;$('look-keywords').disabled=lookBusy;
}
function lookMoney(m){return m?m.amount.toFixed(2)+' '+m.currency:'';}
function lookShown(){
  const filter=$('look-filter').value;
  return (look?.items||[]).filter(i=>!filter||(filter==='match'?(i.verdict==='same'||i.verdict==='similar'):filter==='unchecked'?!i.verdict:true));
}
function lookSelect(index){
  if(lookBusy)return;
  if(look)look.keywords=$('look-keywords').value;
  lookIndex=index;look=lookQueue[index]||null;
  $('look-keywords').value=look?.keywords||'';
  $('look-description').textContent=look?.description||'';
  $('look-preview').hidden=!look?.preview;
  if(look?.preview)$('look-preview').src=look.preview;else $('look-preview').removeAttribute('src');
  $('look-status').textContent=look?.message||'';
  $('look-queue').replaceChildren(...lookQueue.map((entry,i)=>{
    const button=el('button',i===index?'button':'button secondary',(i+1)+'. '+entry.name+(entry.token?' · 已识别':''));
    button.type='button';button.setAttribute('aria-pressed',String(i===index));button.addEventListener('click',()=>lookSelect(i));return button;
  }));
  lookRender();
}
function lookRender(){
  const items=lookQueue.flatMap(entry=>entry.items);$('look-results').hidden=!items.length;
  const order={same:0,similar:1,'':2,different:3};
  const rows=[];
  for(const entry of lookQueue){
    if(!entry.items.length)continue;
    const header=el('tr','look-group'),heading=cell(header,entry.name+' · '+entry.items.length+' 个候选');heading.colSpan=6;rows.push(header);
    const filter=$('look-filter').value;
    const shown=entry.items.filter(i=>!filter||(filter==='match'?(i.verdict==='same'||i.verdict==='similar'):filter==='unchecked'?!i.verdict:true)).sort((a,b)=>(order[a.verdict||'']-order[b.verdict||''])||((b.similarity??-1)-(a.similarity??-1))||(b.found_by.length-a.found_by.length));
    rows.push(...shown.map(item=>{
    const tr=el('tr','look-'+(item.verdict||'pending'));
    const pick=el('input');pick.type='checkbox';pick.checked=!!item.picked;pick.setAttribute('aria-label','选择 '+item.asin);
    pick.addEventListener('change',()=>{item.picked=pick.checked;lookControls();});cell(tr,'').append(pick);
    const picture=cell(tr,'');if(item.image){const img=el('img','look-thumb');img.src=item.image;img.alt='';img.loading='lazy';img.referrerPolicy='no-referrer';picture.append(img);}else picture.textContent='无图';
    const product=cell(tr,''),link=el('a','',item.title||item.asin),host={AE:'www.amazon.ae',SA:'www.amazon.sa',US:'www.amazon.com'}[entry.marketplace];
    link.href='https://'+host+'/dp/'+item.asin;link.target='_blank';link.rel='noopener noreferrer';
    product.append(link,el('small','comp-asin',item.asin+(item.brand?' · '+item.brand:'')+(item.category?' · '+item.category:'')));
    const verdict=cell(tr,'');
    if(item.verdict)verdict.append(el('b','look-verdict look-v-'+item.verdict,LOOK_VERDICTS[item.verdict]+(item.similarity!=null?' '+item.similarity+'%':'')));
    else verdict.append(el('span','hint','未比对'));
    if(item.ai_reason)verdict.append(el('p','comp-evidence','AI：'+item.ai_reason));
    const evidence=cell(tr,'');for(const note of item.evidence||[])evidence.append(el('p','comp-evidence',note));
    const price=cell(tr,'');const offers=item.offers;
    if(!offers)price.append(el('span','hint','未查询'));
    else if(offers.error)price.append(el('span','ops-blocked',offers.error));
    else{
      price.append(el('b','',offers.lowest?'最低 '+offers.lowest.total.toFixed(2)+' '+offers.lowest.currency:'暂无在售报价'));
      if(offers.buy_box)price.append(el('p','comp-evidence','购物车价 '+lookMoney(offers.buy_box)));
      price.append(el('p','comp-evidence',offers.count+' 个卖家在卖'));
      if(offers.own_stores.length)price.append(el('p','look-own','我们的店铺在卖：'+offers.own_stores.join('、')));
      if(offers.list.length){const more=el('details','look-offers');more.append(el('summary','','每个卖家的价格'));for(const o of offers.list)more.append(el('p','',(o.own_store?'【'+o.own_store+'】':'卖家 '+o.seller)+' · '+o.total.toFixed(2)+' '+o.currency+(o.shipping?'（含运费 '+o.shipping.toFixed(2)+'）':'')+(o.buy_box?' · 购物车':'')+(o.fba?' · FBA':'')));price.append(more);}
    }
    return tr;
    }));
  }
  $('look-rows').replaceChildren(...rows);
  $('look-count').textContent='共 '+items.length+' 个候选，已比对 '+items.filter(i=>i.verdict).length+' 个，同款 '+items.filter(i=>i.verdict==='same').length+' 个，相似 '+items.filter(i=>i.verdict==='similar').length+' 个。';
  lookControls();
}
async function lookRun(button,label,work){
  if(lookBusy)return;const epoch=lookEpoch,text=button.textContent;lookBusy=true;button.textContent=label;lookControls();
  try{await work(epoch);}catch(e){if(epoch===lookEpoch){$('look-status').textContent=e.message;notify(e.message,true);}}
  finally{if(epoch===lookEpoch){lookBusy=false;button.textContent=text;lookControls();}}
}
function lookMerge(rows){const index=new Map((look?.items||[]).map(i=>[i.asin,i]));for(const row of rows){const old=index.get(row.asin);if(old)Object.assign(old,row,{picked:old.picked});}}
$('look-file').addEventListener('change',()=>{
  const files=[...$('look-file').files];
  if(files.length>10||files.some(file=>file.size>4*1024*1024||!['image/jpeg','image/png','image/webp'].includes(file.type))){notify('最多 10 张 JPG、PNG、WebP 图片，每张须小于 4 MB',true);$('look-file').value='';return;}
  if(lookQueue.some(entry=>entry.items.length)&&!confirm('当前结果将被新的图片队列替换。请先保存或导出。确定继续？'))return;
  lookReset(true);
  lookQueue=files.map((file,i)=>({file,name:file.name||'图片 '+(i+1),preview:URL.createObjectURL(file),keywords:'',description:'',items:[],store:currentStore,marketplace:$('look-market').value,message:'请先读取这张图片的核心词。'}));
  lookSelect(0);
});
$('look-consent').addEventListener('change',lookControls);
$('look-keywords').addEventListener('input',lookControls);
$('look-filter').addEventListener('change',lookRender);
$('look-market').addEventListener('change',()=>{lookReset(false);lookSelect(0);});
$('look-next').addEventListener('click',()=>lookSelect(lookIndex+1));
$('look-read').addEventListener('click',()=>lookRun($('look-read'),'AI 正在看图…',async epoch=>{
  const file=look.file;
  const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]||'');reader.onerror=()=>reject(new Error('读取图片失败'));reader.readAsDataURL(file);});
  const result=await postJSON('/api/lookalike/keywords',{marketplace:$('look-market').value,image:data,ai_consent:true});
  if(epoch!==lookEpoch)return;
  Object.assign(look,{token:result.token,marketplace:result.marketplace,items:[],keywords:result.keywords.join('\n'),description:[result.description,result.brand?'图上品牌：'+result.brand:''].filter(Boolean).join(' · ')});
  $('look-keywords').value=result.keywords.join('\n');
  $('look-description').textContent=[result.description,result.brand?'图上品牌：'+result.brand:''].filter(Boolean).join(' · ');
  $('look-status').textContent='第 1 步完成：可以先改核心词（每行一个，最多 5 个），再点“用核心词搜索”。';
  look.message=$('look-status').textContent;
}));
$('look-search').addEventListener('click',()=>lookRun($('look-search'),'正在搜索亚马逊目录…',async epoch=>{
  const keywords=$('look-keywords').value.split(/[\r\n]+/).map(s=>s.trim()).filter(Boolean);
  if(keywords.length>5)throw new Error('核心词最多 5 个');
  const result=await postJSON('/api/lookalike/search',{token:look.token,keywords,store:look.store});
  if(epoch!==lookEpoch)return;
  look.items=result.items;lookRender();
  look.keywords=keywords.join('\n');look.confirmed=true;
  $('look-status').textContent='第 2 步完成：'+result.keywords.length+' 个核心词共搜到 '+result.count+' 个不重复的商品。'+(result.errors.length?' '+result.errors.join('；'):'')+' 下一步让 AI 逐个比图。';
}));
$('look-compare').addEventListener('click',()=>lookRun($('look-compare'),'AI 正在比图…',async epoch=>{
  const limit=Number($('look-compare-limit').value)||40;
  const todo=look.items.filter(i=>!i.verdict&&i.image).sort((a,b)=>b.found_by.length-a.found_by.length).slice(0,limit);
  if(!todo.length)throw new Error('没有需要比对的候选了');
  for(let i=0;i<todo.length;i+=8){
    const batch=todo.slice(i,i+8).map(x=>x.asin);
    $('look-status').textContent='正在比对第 '+(i+1)+'–'+Math.min(i+8,todo.length)+' 个，共 '+todo.length+' 个…';
    const result=await postJSON('/api/lookalike/compare',{token:look.token,asins:batch,ai_consent:$('look-consent').checked});
    if(epoch!==lookEpoch)return;lookMerge(result.items);lookRender();
  }
  $('look-status').textContent='第 3 步完成：同款 '+look.items.filter(i=>i.verdict==='same').length+' 个，相似 '+look.items.filter(i=>i.verdict==='similar').length+' 个。AI 的判断要人工确认；下一步查价格。';
}));
$('look-prices').addEventListener('click',()=>lookRun($('look-prices'),'正在查价格…',async epoch=>{
  const todo=look.items.filter(i=>i.picked||i.verdict==='same'||i.verdict==='similar').slice(0,30);
  for(let i=0;i<todo.length;i+=10){
    const batch=todo.slice(i,i+10).map(x=>x.asin);
    $('look-status').textContent='正在查第 '+(i+1)+'–'+Math.min(i+10,todo.length)+' 个商品的卖家报价（每个约 2 秒）…';
    const result=await postJSON('/api/lookalike/prices',{token:look.token,asins:batch,store:look.store});
    if(epoch!==lookEpoch)return;lookMerge(result.items);lookRender();
  }
  $('look-status').textContent='第 4 步完成：已查 '+todo.length+' 个商品的价格。可以导出表格。';
  look.message=$('look-status').textContent;
}));
// CSV cell: quote when needed and neutralise spreadsheet formulas.
const lookCsvCell=value=>{let text=String(value??'');if(/^[=+\-@\t\r]/.test(text))text="'"+text;return /[",\r\n]/.test(text)?'"'+text.replace(/"/g,'""')+'"':text;};
$('look-export').addEventListener('click',()=>{try{
  const rows=[['哪张图','ASIN','标题','品牌','类目','AI判断','相似度','AI依据','依据','最低价','币种','购物车价','卖家数','我们的店铺','各卖家报价','链接']];
  for(const entry of lookQueue){
    const host={AE:'www.amazon.ae',SA:'www.amazon.sa',US:'www.amazon.com'}[entry.marketplace];
    for(const i of entry.items){const o=i.offers&&!i.offers.error?i.offers:null;rows.push([entry.name,i.asin,i.title,i.brand,i.category,LOOK_VERDICTS[i.verdict]||'未比对',i.similarity??'',i.ai_reason,(i.evidence||[]).join('；'),o?.lowest?o.lowest.total.toFixed(2):'',o?.lowest?.currency||'',o?.buy_box?o.buy_box.amount.toFixed(2):'',o?o.count:'',o?o.own_stores.join('、'):'',o?o.list.map(x=>(x.own_store||x.seller)+' '+x.total.toFixed(2)).join('；'):'','https://'+host+'/dp/'+i.asin]);}
  }
  saveDownloadBlob(new Blob(['﻿'+rows.map(r=>r.map(lookCsvCell).join(',')).join('\r\n')+'\r\n'],{type:'text/csv;charset=utf-8'}),'以图找同款_'+(look.marketplace||'')+'.csv');
}catch(error){notify('导出失败：'+error.message,true);}});
async function lookHistoryList(){
  const epoch=lookEpoch,result=await request('/api/lookalike/history');
  if(epoch!==lookEpoch)return;
  const select=$('look-history-list'),previous=select.value;select.replaceChildren(new Option('选择已保存的查找',''));
  for(const item of result.history||[])select.append(new Option(item.title+' · '+item.owner+' · '+localTime(item.created_at),item.id));
  if([...select.options].some(option=>option.value===previous))select.value=previous;
  lookControls();
}
$('look-history-list').addEventListener('change',lookControls);
$('look-history-refresh').addEventListener('click',()=>lookHistoryList().catch(e=>notify(e.message,true)));
$('look-history-save').addEventListener('click',()=>lookRun($('look-history-save'),'正在保存…',async epoch=>{
  const entries=lookQueue.filter(entry=>entry.token&&entry.confirmed).map(entry=>({token:entry.token,name:entry.name}));
  const result=await postJSON('/api/lookalike/history/save',{title:$('look-history-title').value.trim()||'以图找同款',entries,save_images:$('look-save-images').checked});
  if(epoch!==lookEpoch)return;
  lookHistory=result;await lookHistoryList();$('look-history-list').value=result.id;
  notify('已保存 '+result.groups.length+' 张图的查询结果'+(result.images_saved?'及原图':'，原图未保存')+'。');
}));
$('look-history-open').addEventListener('click',()=>{
  if(lookQueue.some(entry=>entry.token&&entry.items.length)&&!confirm('打开历史会替换当前图片队列。请先保存或导出当前结果。确定继续？'))return;
  lookRun($('look-history-open'),'正在打开…',async epoch=>{
    const result=await request('/api/lookalike/history/open?id='+encodeURIComponent($('look-history-list').value));
    if(epoch!==lookEpoch)return;
    $('look-file').value='';
    for(const entry of lookQueue)if(entry.preview?.startsWith('blob:'))URL.revokeObjectURL(entry.preview);
    lookHistory=result;lookQueue=result.groups.map(group=>({...group,keywords:group.keywords.join('\n'),
      preview:group.original_image?'data:'+group.original_image.mime+';base64,'+group.original_image.data:'',
      message:'已保存的历史结果 · 查询时间 '+localTime(group.queried_at)+'。重新比图或搜索需要重新上传图片。'}));
    lookBusy=false;lookSelect(0);lookBusy=true;
    $('look-history-title').value=result.title;
  });
});
$('look-history-delete').addEventListener('click',()=>{
  if(!lookHistory||!confirm('确定永久删除这次保存的查询结果及保存的原图？'))return;
  lookRun($('look-history-delete'),'正在删除…',async epoch=>{
    await postJSON('/api/lookalike/history/delete',{id:lookHistory.id,confirmed:true});
    if(epoch!==lookEpoch)return;lookHistory=null;await lookHistoryList();notify('历史记录已删除。');
  });
});
for(const kind of ['csv','xlsx','json'])$('look-history-'+kind).addEventListener('click',()=>lookRun($('look-history-'+kind),'正在导出…',async epoch=>{
  const response=await fetch('/api/lookalike/history/export?id='+encodeURIComponent($('look-history-list').value)+'&format='+kind);
  if(!response.ok){const error=await response.json();throw new Error(error.error||'导出失败');}
  const blob=await response.blob();if(epoch===lookEpoch)saveDownloadBlob(blob,'以图找同款历史.'+kind);
}));
function enterLookalike(){lookControls();lookHistoryList().catch(e=>notify(e.message,true));}
$('logout').addEventListener('click',()=>lookReset(false));
lookControls();
