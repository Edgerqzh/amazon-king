// Photo -> the same product on Amazon: four visible steps, each one started by the user.
const LOOK_VERDICTS={same:'同款',similar:'相似',different:'不同'};
let look=null,lookBusy=false,lookEpoch=0;
const lookCanUse=()=>currentRole!=='viewer';
function lookReset(keepImage){
  lookEpoch++;look=null;lookBusy=false;
  $('look-keywords').value='';$('look-description').textContent='';$('look-rows').replaceChildren();
  $('look-status').textContent='';$('look-results').hidden=true;
  if(!keepImage){$('look-file').value='';$('look-preview').hidden=true;$('look-preview').removeAttribute('src');}
  lookControls();
}
function lookControls(){
  const has=!!$('look-file').files[0],items=look?.items||[];
  $('look-editor').hidden=!lookCanUse();$('look-viewer-note').hidden=lookCanUse();
  $('look-read').disabled=lookBusy||!has||!$('look-consent').checked;
  $('look-search').disabled=lookBusy||!look?.token||!$('look-keywords').value.trim();
  $('look-compare').disabled=lookBusy||!items.length||!$('look-consent').checked;
  $('look-prices').disabled=lookBusy||!items.some(i=>i.verdict==='same'||i.verdict==='similar'||i.picked);
  $('look-export').disabled=lookBusy||!items.length;
  $('look-market').disabled=lookBusy;
  $('look-step2').hidden=!look?.token;$('look-step3').hidden=!items.length;
}
function lookMoney(m){return m?m.amount.toFixed(2)+' '+m.currency:'';}
function lookShown(){
  const filter=$('look-filter').value;
  return (look?.items||[]).filter(i=>!filter||(filter==='match'?(i.verdict==='same'||i.verdict==='similar'):filter==='unchecked'?!i.verdict:true));
}
function lookRender(){
  const items=look?.items||[];$('look-results').hidden=!items.length;
  const order={same:0,similar:1,'':2,different:3};
  const shown=lookShown().sort((a,b)=>(order[a.verdict||'']-order[b.verdict||''])||((b.similarity??-1)-(a.similarity??-1))||(b.found_by.length-a.found_by.length));
  $('look-rows').replaceChildren(...shown.map(item=>{
    const tr=el('tr','look-'+(item.verdict||'pending'));
    const pick=el('input');pick.type='checkbox';pick.checked=!!item.picked;pick.setAttribute('aria-label','选择 '+item.asin);
    pick.addEventListener('change',()=>{item.picked=pick.checked;lookControls();});cell(tr,'').append(pick);
    const picture=cell(tr,'');if(item.image){const img=el('img','look-thumb');img.src=item.image;img.alt='';img.loading='lazy';img.referrerPolicy='no-referrer';picture.append(img);}else picture.textContent='无图';
    const product=cell(tr,''),link=el('a','',item.title||item.asin),host={AE:'www.amazon.ae',SA:'www.amazon.sa'}[look.marketplace];
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
  const file=$('look-file').files[0];lookReset(true);
  if(!file)return;
  if(file.size>4*1024*1024){notify('图片须小于 4 MB',true);$('look-file').value='';return;}
  $('look-preview').src=URL.createObjectURL(file);$('look-preview').hidden=false;lookControls();
});
$('look-consent').addEventListener('change',lookControls);
$('look-keywords').addEventListener('input',lookControls);
$('look-filter').addEventListener('change',lookRender);
$('look-market').addEventListener('change',()=>lookReset(true));
$('look-read').addEventListener('click',()=>lookRun($('look-read'),'AI 正在看图…',async epoch=>{
  const file=$('look-file').files[0];
  const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]||'');reader.onerror=()=>reject(new Error('读取图片失败'));reader.readAsDataURL(file);});
  const result=await postJSON('/api/lookalike/keywords',{marketplace:$('look-market').value,image:data,ai_consent:true});
  if(epoch!==lookEpoch)return;
  look={token:result.token,marketplace:result.marketplace,items:[]};
  $('look-keywords').value=result.keywords.join('\n');
  $('look-description').textContent=[result.description,result.brand?'图上品牌：'+result.brand:''].filter(Boolean).join(' · ');
  $('look-status').textContent='第 1 步完成：可以先改核心词（每行一个，最多 5 个），再点“用核心词搜索”。';
}));
$('look-search').addEventListener('click',()=>lookRun($('look-search'),'正在搜索亚马逊目录…',async epoch=>{
  const keywords=$('look-keywords').value.split(/[\r\n]+/).map(s=>s.trim()).filter(Boolean);
  if(keywords.length>5)throw new Error('核心词最多 5 个');
  const result=await postJSON('/api/lookalike/search',{token:look.token,keywords});
  if(epoch!==lookEpoch)return;
  look.items=result.items;lookRender();
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
    const result=await postJSON('/api/lookalike/prices',{token:look.token,asins:batch});
    if(epoch!==lookEpoch)return;lookMerge(result.items);lookRender();
  }
  $('look-status').textContent='第 4 步完成：已查 '+todo.length+' 个商品的价格。可以导出表格。';
}));
$('look-export').addEventListener('click',()=>{
  const rows=[['ASIN','标题','品牌','类目','AI判断','相似度','AI依据','依据','最低价','币种','购物车价','卖家数','我们的店铺','各卖家报价','链接']];
  const host={AE:'www.amazon.ae',SA:'www.amazon.sa'}[look.marketplace];
  for(const i of look.items){const o=i.offers&&!i.offers.error?i.offers:null;rows.push([i.asin,i.title,i.brand,i.category,LOOK_VERDICTS[i.verdict]||'未比对',i.similarity??'',i.ai_reason,(i.evidence||[]).join('；'),o?.lowest?o.lowest.total.toFixed(2):'',o?.lowest?.currency||'',o?.buy_box?o.buy_box.amount.toFixed(2):'',o?o.count:'',o?o.own_stores.join('、'):'',o?o.list.map(x=>(x.own_store||x.seller)+' '+x.total.toFixed(2)).join('；'):'','https://'+host+'/dp/'+i.asin]);}
  saveDownloadBlob(new Blob(['﻿'+rows.map(r=>r.map(csvCell).join(',')).join('\r\n')+'\r\n'],{type:'text/csv;charset=utf-8'}),'以图找同款_'+look.marketplace+'.csv');
});
function enterLookalike(){lookControls();}
$('logout').addEventListener('click',()=>lookReset(false));
lookControls();
