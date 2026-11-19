// Inventory result -> official product type -> generated fill table saved for that shop, then re-match.
// Every category's type is chosen by a person from Amazon's own search results; nothing is guessed.
const invGen={key:'',state:new Map(),running:false,switchTo:'',statusLoading:false};
function invGenKey(report){return [report.filename,report.shop,report.marketplace,report.table_type||'upload'].join('|');}
// The connection status names the chosen store, the main store included (its id is "default" there
// while currentStore is ''). After a page reload it may not be read yet, so it is read when needed.
function invGenStoreName(){return amazonStatus?.name||(invGen.statusLoading?'（正在读取店铺名…）':'当前连接的店铺');}
async function invGenStatus(){
  if(!amazonStatus)await loadAmazonStatus();
  if(!amazonStatus)throw new Error('读取亚马逊连接状态失败，请稍后再试，或在“设置 › 亚马逊连接”里点“重新读取配置”');
  return amazonStatus;
}
// Reads the status once for the batch bar, then redraws only the bar (a full redraw would reset the ticked templates).
function invGenNeedStatus(){
  if(amazonStatus||invGen.statusLoading)return;
  invGen.statusLoading=true;
  loadAmazonStatus().catch(()=>{}).finally(()=>{invGen.statusLoading=false;if(amazonStatus&&inventoryReport)invGenBar(categoryResults(inventoryReport),inventoryReport);});
}
// Generated tables carry the seller's identity, so they can never be public (通用模板) templates:
// with 通用模板 selected they go under the connected Amazon store's name instead.
async function invGenTargetShop(report){
  if(report.shop!=='通用模板')return report.shop;
  const status=await invGenStatus();
  if(!status.configured||!status.name)throw new Error('还没有连接亚马逊店铺。请先在“设置 › 亚马逊连接”里连接店铺，再生成表格');
  return status.name;
}
const INV_GEN_SWITCHED='店铺已切换，没有生成。请确认右上角选的是要生成表格的店铺后再点一次';
function invGenEntry(category){if(!invGen.state.has(category))invGen.state.set(category,{items:null,chosen:'',status:'',error:false,saved:null,busy:false});return invGen.state.get(category);}
function invGenUsable(report){return currentRole!=='viewer'&&(report.table_type||'upload')==='upload';}
function invGenMissing(groups){return groups.filter(g=>!g.template&&g.category!=='无法识别类目'&&!g.category.includes(' / '));}
async function invGenSearch(report,group){
  const entry=invGenEntry(group.category),title=(group.titles||[]).find(Boolean)||'';
  const locale=text=>/[㐀-鿿]/.test(text)?'zh_CN':'en_US';
  const tries=title?[{mode:'item_name',query:title.slice(0,200)},{mode:'keywords',query:group.category}]:[{mode:'keywords',query:group.category}];
  let items=[];
  for(const t of tries){
    const result=await postJSON('/api/generated-templates/search',{marketplace:report.marketplace,query:t.query,mode:t.mode,search_locale:locale(t.query)});
    items=result.items||[];if(items.length)break;
  }
  entry.items=items;entry.chosen=items[0]?.name||'';entry.error=!items.length;
  entry.status=items.length?'找到 '+items.length+' 个官方类型，请核对第一个是否符合实际商品，不符就换一个。':'亚马逊没有返回匹配的类型。可在“模板库 › 从规则生成”里换个说法再找。';
}
// epoch is the store choice when the click happened; a switch midway stops before anything is saved.
async function invGenSave(report,group,epoch){
  const entry=invGenEntry(group.category);if(!entry.chosen)throw new Error('请先选择商品类型');
  // Where it will be saved is settled first, so Amazon's rules are not downloaded for a table that cannot be kept.
  const shop=await invGenTargetShop(report);
  if(epoch!==storeEpoch)throw new Error(INV_GEN_SWITCHED);
  const generated=await postJSON('/api/generated-templates/generate',{marketplace:report.marketplace,product_type:entry.chosen});
  if(epoch!==storeEpoch)throw new Error(INV_GEN_SWITCHED);
  const saved=await postJSON('/api/generated-templates/save',{token:generated.token,shop,category:group.category});
  if(shop!==report.shop)invGen.switchTo=shop;
  entry.saved={filename:saved.original_name||saved.filename||generated.product_type,fields:generated.field_count,required:generated.required_count};
  entry.status='已存入模板库（店铺“'+shop+'”）：'+(generated.display_name||generated.product_type)+' · '+generated.field_count+' 列（'+generated.required_count+' 列必填）';entry.error=false;
}
async function invGenRematch(){
  try{await load();}catch(e){notify('已存入模板库，但刷新模板列表失败，请稍后点“识别类别并匹配表格”',true);return;}
  const target=invGen.switchTo;invGen.switchTo='';
  if(target){
    updateInventoryShopOptions();const select=$('inventory-shop');
    if(![...select.options].some(o=>o.value===target))select.add(new Option(target,target));
    select.value=target;updateInventoryShopOptions();
    notify('表格已存到店铺“'+target+'”，库存识别已切换到这个店铺');
  }
  if(!$('inventory-file').files.length||$('inventory-file').files[0]!==inspectedFile){notify('表格已存入模板库。重新选择这份库存后识别，就能直接下载。');return;}
  notify('表格已存入模板库，正在重新匹配…');
  inventoryLocalRecheck=true;$('inventory-form').requestSubmit($('inventory-form').querySelector('button[type="submit"]'));
}
// Every click ends in visible feedback: a status under the category, a notice, or a re-match.
async function invGenRun(work,rematch){
  if(invGen.running){notify('正在处理上一步，请稍等',true);return;}
  if(busy){notify('库存识别还在进行中，请等它完成后再点',true);return;}
  invGen.running=true;
  const savedBefore=[...invGen.state.values()].filter(e=>e.saved).length,epoch=storeEpoch;
  try{invGenRefresh();await work(epoch);}
  catch(error){notify('出错了：'+(error?.message||error),true);}
  finally{invGen.running=false;}
  const savedNow=[...invGen.state.values()].filter(e=>e.saved).length;
  try{if(rematch&&savedNow>savedBefore)await invGenRematch();else invGenRefresh();}
  catch(error){notify('表格已存入模板库，但重新匹配出错：'+(error?.message||error)+'。请点“识别类别并匹配表格”重新识别',true);}
}
function invGenError(message){
  if(/卖家记号不一致|另一个店铺/.test(message))return message+'。请在右上角切换到这个店铺的亚马逊连接后再生成。';
  const bound=message.match(/已绑定店铺“([^”]+)”/);
  if(bound)return message+'。请把库存识别的目标店铺改成“'+bound[1]+'”后重新识别，再生成。';
  return message;
}
function invGenRefresh(){if(inventoryReport)renderInventory(inventoryReport);}
// Called by renderInventory for each category without a template.
function invGenAttach(body,group,report){
  if(!invGenUsable(report)||group.category==='无法识别类目'||group.category.includes(' / '))return;
  const entry=invGenEntry(group.category),box=el('div','inv-gen');
  if(entry.saved){box.append(el('p','inv-gen-status ok',entry.status));body.append(box);return;}
  if(!entry.items){
    const find=el('button','text-button',entry.busy?'正在查找…':'按亚马逊规则生成表格');find.type='button';find.disabled=invGen.running;
    find.addEventListener('click',()=>invGenRun(async()=>{entry.busy=true;invGenRefresh();try{await invGenSearch(report,group);}catch(e){entry.status=invGenError(e.message);entry.error=true;}finally{entry.busy=false;}},false));
    box.append(find);
  }else if(entry.items.length){
    const row=el('div','inv-gen-row'),select=el('select');select.setAttribute('aria-label','选择 '+group.category+' 的亚马逊商品类型');select.disabled=invGen.running;
    for(const item of entry.items.slice(0,30)){const o=el('option','',item.display_name+'（'+item.name+'）');o.value=item.name;o.selected=item.name===entry.chosen;select.append(o);}
    select.addEventListener('change',()=>{entry.chosen=select.value;});
    const go=el('button','button secondary',entry.busy?'正在生成…':'生成并存入模板库');go.type='button';go.disabled=invGen.running;
    go.addEventListener('click',()=>invGenRun(async epoch=>{entry.busy=true;invGenRefresh();try{await invGenSave(report,group,epoch);}catch(e){entry.status=invGenError(e.message);entry.error=true;notify('没有保存。'+entry.status,true);}finally{entry.busy=false;}},true));
    row.append(select,go);box.append(row);
  }
  if(entry.status)box.append(el('p','inv-gen-status'+(entry.error?' bad':''),entry.status));
  body.append(box);
}
// Called by renderInventory once per render: the batch bar above the category list.
function invGenBar(groups,report){
  const bar=$('inventory-gen-bar');if(!bar)return;
  const key=invGenKey(report);if(key!==invGen.key){invGen.key=key;invGen.state.clear();}
  const missing=invGenMissing(groups);
  if(!invGenUsable(report)||!missing.length){bar.hidden=true;return;}
  bar.hidden=false;invGenNeedStatus();
  const unsearched=missing.filter(g=>!invGenEntry(g.category).items),ready=missing.filter(g=>{const e=invGenEntry(g.category);return e.items?.length&&e.chosen&&!e.saved;});
  const text=el('p','hint',report.shop==='通用模板'
    ?'有 '+missing.length+' 类商品还没有表格。可以用亚马逊最新的商品类型规则直接生成。生成的表格带店铺的卖家信息，不能存成通用模板，会存到当前连接的亚马逊店铺“'+invGenStoreName()+'”名下，存好后库存识别自动切换到这个店铺。'
    :'有 '+missing.length+' 类商品还没有表格。可以用亚马逊最新的商品类型规则直接生成，存到店铺“'+report.shop+'”名下。规则读取自亚马逊连接里的“'+invGenStoreName()+'”，请确认它就是这个店铺。');
  const actions=el('div','inv-gen-actions');
  if(unsearched.length){
    const find=el('button','button secondary',invGen.running?'正在查找…':'为这 '+unsearched.length+' 类查找官方类型');find.type='button';find.disabled=invGen.running;
    find.addEventListener('click',()=>invGenRun(async()=>{for(const g of unsearched){const e=invGenEntry(g.category);e.busy=true;invGenRefresh();try{await invGenSearch(report,g);}catch(err){e.status=invGenError(err.message);e.error=true;if(/401|403|未配置|连接/.test(err.message))break;}finally{e.busy=false;}}},false));
    actions.append(find);
  }
  if(ready.length){
    const all=el('button','button',invGen.running?'正在生成…':'核对无误，生成全部 '+ready.length+' 份');all.type='button';all.disabled=invGen.running;
    all.addEventListener('click',async()=>{
      if(invGen.running||all.disabled)return;
      const epoch=storeEpoch;let target;all.disabled=true;
      try{target=await invGenTargetShop(report);}catch(err){notify(err.message,true);all.disabled=false;return;}
      all.disabled=false;
      if(epoch!==storeEpoch){notify(INV_GEN_SWITCHED,true);return;}
      if(!confirm('将按各类别当前选中的官方类型生成 '+ready.length+' 份表格，存到店铺“'+target+'”名下。确定类型都核对过了吗？'))return;
      if(epoch!==storeEpoch){notify(INV_GEN_SWITCHED,true);return;}
      invGenRun(async()=>{const failed=[];for(const g of ready){
          if(epoch!==storeEpoch)break;
          const e=invGenEntry(g.category);e.busy=true;invGenRefresh();try{await invGenSave(report,g,epoch);}catch(err){e.status=invGenError(err.message);e.error=true;failed.push(g.category+'：'+e.status);}finally{e.busy=false;}}
        if(epoch!==storeEpoch)notify('店铺已切换，还有 '+ready.filter(g=>!invGenEntry(g.category).saved).length+' 份没有生成。请确认右上角选的店铺后再点一次',true);
        else if(failed.length)notify(failed.length+' 份没有保存。'+failed[0],true);},true);});
    actions.append(all);
  }
  bar.replaceChildren(text,actions);
}
// A store switch changes whose name the bar shows (and where 通用模板 tables would go).
function invGenStoreChanged(){if(inventoryReport&&$('inventory-gen-bar'))invGenBar(categoryResults(inventoryReport),inventoryReport);}
$('logout').addEventListener('click',()=>{invGen.key='';invGen.state.clear();});
