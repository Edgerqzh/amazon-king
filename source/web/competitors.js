// Public BSR research is independent of the seller connection and never submits listings.
const COMP_CLASSES={pending:'待核对',direct:'直接竞品',same_type:'同类不同款',substitute:'替代方案',accessory:'配件耗材',same_brand:'同品牌家族',irrelevant:'无关'};
const COMP_STATES={discovering:'正在查找类目',awaiting_category:'请选择一个类目',running:'正在采集榜单',completed:'采集完成',partial:'仅取得部分结果',blocked:'访问受限，已停止',cancelled:'已停止采集',interrupted:'上次任务已中断',failed:'未完成采集'};
const COMP_ACTIVE=new Set(['discovering','awaiting_category','running']);
let compBatch=null,compEpoch=0,compTimer=null,compBusy=false,compListSequence=0;
const compSelected=new Set();
const compCanEdit=()=>currentRole!=='viewer';
const compActive=()=>compBatch&&COMP_ACTIVE.has(compBatch.status);
const compWords=id=>$(id).value.split(/[\r\n,，;；]+/).map(s=>s.trim()).filter(Boolean);
const compQuery=(path,id)=>'/api/competitors/'+path+'?id='+encodeURIComponent(id);
function compOptions(select,all=false){
  select.replaceChildren();if(all)select.append(new Option('全部分类',''));
  for(const [key,label] of Object.entries(COMP_CLASSES))select.append(new Option(label,key));
}
compOptions($('comp-filter-class'),true);
function compStopPoll(){clearTimeout(compTimer);compTimer=null;}
function compClear(){
  ++compEpoch;++compListSequence;compStopPoll();compBusy=false;compBatch=null;compSelected.clear();
  $('comp-library').replaceChildren(new Option('请选择一个已保存批次',''));
  $('comp-list-note').textContent='';$('comp-state').hidden=true;
  $('comp-source').value='';$('comp-keywords').value='';$('comp-exclude').value='';
  $('comp-target-title').value='';$('comp-target-brand').value='';
  $('comp-consent').checked=false;$('comp-ai-consent').checked=false;
  $('comp-html').value='';$('comp-import-url').value='';
  compRender();
}
function compControls(){
  const edit=compCanEdit(),active=!!compActive(),off=compBusy||active;
  $('comp-editor').hidden=!edit;$('comp-viewer-note').hidden=edit;
  $('comp-market').disabled=off;
  for(const id of ['comp-source','comp-keywords','comp-exclude','comp-target-title','comp-target-brand','comp-consent','comp-html','comp-import-url'])$(id).disabled=off;
  $('comp-start').disabled=!edit||off;$('comp-import').disabled=!edit||off;
  $('comp-open').disabled=compBusy||active||!$('comp-library').value;
  $('comp-library').disabled=compBusy||active;
  $('comp-choose').disabled=!edit||compBusy||!$('comp-category').value;
  $('comp-cancel').hidden=!edit||!active;$('comp-cancel').disabled=compBusy;
  $('comp-save').hidden=!edit;$('comp-save').disabled=off||!compSelected.size;
  $('comp-delete').hidden=!edit;$('comp-delete').disabled=off||!compBatch;
  $('comp-ai-box').hidden=!edit;$('comp-score').disabled=off||!compSelected.size||!$('comp-ai-consent').checked;
  for(const button of document.querySelectorAll('#comp-rows button'))button.disabled=off;
  for(const input of document.querySelectorAll('#comp-rows select, #comp-rows textarea'))input.disabled=!edit||off;
  for(const kind of ['csv','xlsx','json'])$('comp-export-'+kind).disabled=compBusy||!compBatch;
}
function compFiltered(){
  const query=$('comp-search').value.trim().toLocaleLowerCase(),kind=$('comp-filter-class').value,hide=$('comp-filter-excluded').checked;
  const excludes=compBatch?.exclude_words||compWords('comp-exclude');
  return (compBatch?.items||[]).filter(item=>{
    const title=String(item.title||'').toLocaleLowerCase(),haystack=[title,...(item.observations||[]).map(o=>o.category||'')].join(' ').toLocaleLowerCase();
    return (!query||(item.asin+' '+title).toLocaleLowerCase().includes(query))&&(!kind||(item.classification||'pending')===kind)&&(!hide||!excludes.some(word=>haystack.includes(String(word).toLocaleLowerCase())));
  });
}
function compLink(item){
  const a=el('a','comp-product-link',item.title||item.asin),host={AE:'www.amazon.ae',SA:'www.amazon.sa'}[compBatch?.marketplace];
  if(host&&/^[A-Z0-9]{10}$/.test(item.asin)){a.href='https://'+host+'/dp/'+item.asin;a.target='_blank';a.rel='noopener noreferrer';}
  return a;
}
function compRender(){
  const batch=compBatch,items=batch?.items||[],shown=compFiltered();
  $('comp-results').hidden=!batch||!items.length;
  $('comp-state').hidden=!batch;
  if(batch){
    $('comp-state-title').textContent=COMP_STATES[batch.status]||batch.status;
    $('comp-state-message').textContent=(batch.message||'')+(batch.pages_done?' · 已读取 '+batch.pages_done+' 页':'');
    $('comp-state').dataset.state=batch.status;
    $('comp-result-title').textContent=batch.title||'本次候选';
    $('comp-result-meta').textContent=[batch.marketplace,batch.target_asin?'目标 '+batch.target_asin:'',localTime(batch.created_at)].filter(Boolean).join(' · ');
    $('comp-saved').textContent=batch.saved?'已保存 · 团队可见':'未入库 · 仅当前成员可见';
    $('comp-category-box').hidden=batch.status!=='awaiting_category';
    const summary=batch.ai_summary;
    $('comp-ai-status').textContent=summary?'已取得 '+summary.completed+' / '+summary.requested+' 个有效建议。'+(summary.errors||[]).join('；'):'';
    const selected=$('comp-category').value;
    $('comp-category').replaceChildren(...(batch.categories||[]).map(category=>new Option(category.label||category.url,category.url)));
    if((batch.categories||[]).some(c=>c.url===selected))$('comp-category').value=selected;
  }else $('comp-category-box').hidden=true;
  const visible=new Set(items.map(i=>i.asin));for(const asin of compSelected)if(!visible.has(asin))compSelected.delete(asin);
  $('comp-rows').replaceChildren();
  for(const item of shown){
    const row=el('tr'),pickCell=cell(row,''),pick=el('input');pick.type='checkbox';pick.checked=compSelected.has(item.asin);pick.setAttribute('aria-label','选择 '+item.asin);
    pick.addEventListener('change',()=>{if(pick.checked)compSelected.add(item.asin);else compSelected.delete(item.asin);compSelection();});pickCell.append(pick);
    const product=cell(row,'');product.append(compLink(item),el('small','comp-asin',item.asin));
    const ranks=cell(row,'');
    for(const observation of item.observations||[]){
      const line=el('div','comp-observation');line.append(el('b','',Number.isInteger(observation.rank)?'#'+observation.rank:'排名未识别'),el('span','',observation.category||'来源类目'),el('small','',localTime(observation.fetched_at)));
      if(observation.category_url)line.title=observation.category_url;
      ranks.append(line);
    }
    if(!ranks.childNodes.length)ranks.textContent='缺少榜单证据';
    const price=cell(row,item.price?[item.price,item.currency].filter(Boolean).join(' '):'未取得');
    if(item.raw_price)price.title='页面原文：'+item.raw_price;
    const evidence=cell(row,'');
    for(const note of item.evidence||[])evidence.append(el('p','comp-evidence',note));
    if(item.keyword_hits?.length)evidence.append(el('small','', '关键词命中（标题/类目）：'+item.keyword_hits.join('、')));
    if(!evidence.childNodes.length)evidence.append(el('span','hint','资料不足，请人工核对'));
    if(item.score!=null)evidence.append(el('p','comp-ai-suggestion','AI 建议：'+item.score+'/10 · '+(COMP_CLASSES[item.ai_classification]||'待核对')));
    if(item.ai_note)evidence.append(el('p','comp-evidence',item.ai_note));
    if(item.ai_model)evidence.append(el('small','',item.ai_model));
    const review=cell(row,'');
    if(compCanEdit()){
      const select=el('select');compOptions(select);select.value=item.classification||'pending';select.setAttribute('aria-label',item.asin+' 的人工分类');
      const note=el('textarea');note.rows=2;note.maxLength=1000;note.value=item.note||'';note.placeholder='备注（可选）';note.setAttribute('aria-label',item.asin+' 的备注');
      const save=el('button','text-button','保存这条复核');save.type='button';
      save.addEventListener('click',()=>compRun(save,'保存中…',async epoch=>{
        const result=await postJSON('/api/competitors/review',{id:batch.id,asin:item.asin,classification:select.value,note:note.value,revision:compBatch.revision});
        if(epoch===compEpoch)compApply(result,false);
      }));
      review.append(select,note,save);
    }else review.append(el('b','',COMP_CLASSES[item.classification]||'待核对'),el('p','',item.note||''));
    $('comp-rows').append(row);
  }
  $('comp-result-note').textContent='显示 '+shown.length+' / '+items.length+' 个候选。排名按来源类目分别记录；标题匹配不等于真实竞争关系。'+(batch?.status==='completed'?'本次采集完成不代表已覆盖整个类目。':'');
  compSelection();compControls();
}
function compSelection(){
  const shown=compFiltered(),all=shown.length>0&&shown.every(i=>compSelected.has(i.asin));
  $('comp-select-all').checked=all;$('comp-select-all').indeterminate=!all&&shown.some(i=>compSelected.has(i.asin));
  $('comp-selection-note').textContent='已选择 '+compSelected.size+' 个';compControls();
}
function compApply(batch,selectNew=true){
  if(batch.marketplace!==$('comp-market').value)return;
  const different=compBatch?.id!==batch.id;compBatch=batch;
  if(different){compSelected.clear();$('comp-search').value='';$('comp-filter-class').value='';}
  if(selectNew&&different)for(const item of batch.items||[])compSelected.add(item.asin);
  compRender();compSchedule();
}
function compSchedule(){
  compStopPoll();
  if(!compBatch||!['discovering','running'].includes(compBatch.status)||$('workspace').hidden||$('competitors-panel').hidden)return;
  const epoch=compEpoch,id=compBatch.id;
  compTimer=setTimeout(async()=>{
    if(epoch!==compEpoch)return;
    if(compBusy){compSchedule();return;}
    try{const batch=await request(compQuery('batch',id));if(epoch===compEpoch&&compBatch?.id===id){
      const existing=new Set((compBatch.items||[]).map(i=>i.asin));
      for(const item of batch.items||[])if(!existing.has(item.asin))compSelected.add(item.asin);
      compApply(batch,false);
    }}catch(e){if(epoch===compEpoch){$('comp-state-message').textContent=e.message+'；可重新进入本页刷新任务状态。';compStopPoll();}}
  },2000);
}
async function compRun(button,label,work){
  if(compBusy||button.disabled)return;
  const epoch=compEpoch,text=button.textContent;compBusy=true;button.textContent=label;compControls();
  try{await work(epoch);}catch(e){if(epoch===compEpoch){notify(e.message,true);$('comp-list-note').textContent=e.message;}}
  finally{if(epoch===compEpoch){compBusy=false;button.textContent=text;compControls();compSchedule();}}
}
async function compLoadList(){
  const epoch=compEpoch,sequence=++compListSequence;
  const result=await request('/api/competitors?marketplace='+encodeURIComponent($('comp-market').value));
  if(epoch!==compEpoch||sequence!==compListSequence)return;
  const select=$('comp-library'),previous=select.value;select.replaceChildren(new Option('请选择一个已保存批次',''));
  for(const batch of result.batches||[])select.append(new Option((batch.title||batch.target_asin||'BSR 批次')+' · '+batch.count+' 个 · '+localTime(batch.created_at),batch.id));
  if([...select.options].some(o=>o.value===previous))select.value=previous;
  $('comp-list-note').textContent=result.batches?.length?'共 '+result.batches.length+' 个已保存批次':'还没有保存的竞品库。采集后核对候选，再保存入库。';compControls();
}
function enterCompetitors(){
  compControls();compLoadList().catch(e=>notify(e.message,true));
  if(compBatch){const epoch=compEpoch,id=compBatch.id;request(compQuery('batch',id)).then(batch=>{if(epoch===compEpoch&&compBatch?.id===id)compApply(batch,false);}).catch(e=>{if(epoch===compEpoch)notify(e.message,true);});}
}
function compInput(){
  const keywords=compWords('comp-keywords');if(keywords.length>5)throw new Error('核心关键词最多 5 个');
  return {marketplace:$('comp-market').value,keywords,exclude_words:compWords('comp-exclude'),target_title:$('comp-target-title').value.trim(),target_brand:$('comp-target-brand').value.trim()};
}
$('comp-form').addEventListener('submit',event=>{
  event.preventDefault();
  if(compBatch&&!compBatch.saved&&compBatch.items?.length&&!confirm('当前候选还未入库。确定开始新的采集吗？'))return;
  compRun($('comp-start'),'正在启动…',async epoch=>{
    const result=await postJSON('/api/competitors/start',{...compInput(),source:$('comp-source').value.trim(),consent:$('comp-consent').checked});
    if(epoch===compEpoch){compSelected.clear();compApply(result);}
  });
});
$('comp-choose').addEventListener('click',()=>compRun($('comp-choose'),'正在开始…',async epoch=>{
  const result=await postJSON('/api/competitors/choose',{id:compBatch.id,category_url:$('comp-category').value,consent:$('comp-consent').checked});
  if(epoch===compEpoch)compApply(result,false);
}));
$('comp-cancel').addEventListener('click',()=>compRun($('comp-cancel'),'正在停止…',async epoch=>{
  const result=await postJSON('/api/competitors/cancel',{id:compBatch.id});if(epoch===compEpoch)compApply(result,false);
}));
$('comp-import-form').addEventListener('submit',event=>{
  event.preventDefault();
  if(compBatch&&!compBatch.saved&&compBatch.items?.length&&!confirm('当前候选还未入库。确定导入另一个批次吗？'))return;
  compRun($('comp-import'),'正在解析…',async epoch=>{
    const file=$('comp-html').files[0];if(!file||file.size>4*1024*1024)throw new Error('请选择不超过 4 MB 的 HTML 文件');
    const html=await file.text();if(epoch!==compEpoch)return;
    const source=$('comp-source').value.trim().toUpperCase();
    const result=await postJSON('/api/competitors/import',{...compInput(),source_url:$('comp-import-url').value.trim(),html,target_asin:/^[A-Z0-9]{10}$/.test(source)?source:''});
    if(epoch===compEpoch)compApply(result);
  });
});
$('comp-market').addEventListener('change',()=>{++compEpoch;compStopPoll();compBatch=null;compSelected.clear();$('comp-state').hidden=true;$('comp-source').value='';$('comp-import-url').value='';$('comp-consent').checked=false;compRender();compLoadList().catch(e=>notify(e.message,true));});
$('comp-library').addEventListener('change',compControls);
$('comp-list-refresh').addEventListener('click',()=>compLoadList().catch(e=>notify(e.message,true)));
$('comp-open').addEventListener('click',()=>compRun($('comp-open'),'正在读取…',async epoch=>{
  const result=await request(compQuery('batch',$('comp-library').value));if(epoch===compEpoch)compApply(result);
}));
for(const id of ['comp-search','comp-filter-class','comp-filter-excluded'])$(id).addEventListener(id==='comp-search'?'input':'change',compRender);
$('comp-select-all').addEventListener('change',()=>{for(const item of compFiltered())if($('comp-select-all').checked)compSelected.add(item.asin);else compSelected.delete(item.asin);compRender();});
$('comp-save').addEventListener('click',()=>compRun($('comp-save'),'正在保存…',async epoch=>{
  if(compBatch.saved&&compSelected.size<compBatch.items.length&&!confirm('这会将已保存批次替换为所选候选，未选中的商品及其复核记录将被移出这个批次。确定继续？'))return;
  const result=await postJSON('/api/competitors/save',{id:compBatch.id,asins:[...compSelected]});
  if(epoch===compEpoch){compApply(result,false);await compLoadList();notify('竞品库已保存，团队成员可以查看。');}
}));
$('comp-delete').addEventListener('click',()=>{
  if(!compBatch||!confirm('确定永久删除这个竞品批次及其复核记录？此操作不会删除亚马逊商品。'))return;
  compRun($('comp-delete'),'正在删除…',async epoch=>{
    await postJSON('/api/competitors/delete',{id:compBatch.id,confirmed:true});
    if(epoch===compEpoch){compBatch=null;compSelected.clear();compRender();await compLoadList();}
  });
});
$('comp-ai-consent').addEventListener('change',compControls);
$('comp-score').addEventListener('click',()=>compRun($('comp-score'),'AI 正在分析…',async epoch=>{
  const asins=[...compSelected];if(!asins.length||asins.length>20)throw new Error('每次请选择 1–20 个候选；不要一次发送整库');
  const result=await postJSON('/api/competitors/score',{id:compBatch.id,asins,ai_consent:$('comp-ai-consent').checked});
  if(epoch===compEpoch){compApply(result,false);const summary=result.ai_summary;notify(summary?'AI 已完成 '+summary.completed+' / '+summary.requested+' 个；请核对建议和未完成项。':'未取得有效 AI 分析结果',!summary||summary.failed>0);}
}));
for(const kind of ['csv','xlsx','json'])$('comp-export-'+kind).addEventListener('click',()=>compRun($('comp-export-'+kind),'正在导出…',async epoch=>{
  const response=await fetch(compQuery('export',compBatch.id)+'&format='+kind);
  if(response.status===401){$('workspace').hidden=true;$('login-screen').hidden=false;throw new Error('请先登录');}
  if(!response.ok){const error=await response.json();throw new Error(error.error||'导出失败');}
  const blob=await response.blob();if(epoch===compEpoch)saveDownloadBlob(blob,'BSR-竞品库-'+compBatch.marketplace+'.'+kind);
}));
$('logout').addEventListener('click',compClear);
new MutationObserver(()=>{if($('workspace').hidden)compClear();}).observe($('workspace'),{attributes:true,attributeFilter:['hidden']});
window.addEventListener('hashchange',()=>{if($('competitors-panel').hidden)compStopPoll();});
$('backup-panel').append(el('p','hint','模板备份不包含竞品库。请在“竞品库”页面另行导出完整 JSON，或停服后备份 competitors.sqlite3。'));
if(!$('workspace').hidden&&$('workspace').dataset.page==='competitors')enterCompetitors();
