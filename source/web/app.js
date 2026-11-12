const $ = id => document.getElementById(id);
const UI_REVISION='英文文案版 1112.1';
const TABLE_TYPES={upload:'上传表格',price_update:'改价表格'};
ensureInventoryShopPicker();
let templates = [];
let inventoryShopNames=[];
const selectedTemplates=new Set();
let libraryDownloading=false;
let visibleTemplates=[];
let currentRole='viewer';
let inspectingTemplate=false;
let busy = false;
let timer;
const PAGES={
  library:{id:'library',title:'模板库',description:'查找和下载团队保存的模板，按店铺、地区与类目筛选。',roles:['admin','editor','viewer']},
  fill:{id:'fill-panel',title:'填写表格',description:'把产品资料填写到原始模板，支持新商品与价格／库存更新。',roles:['admin','editor']},
  report:{id:'report-panel',title:'处理报告',description:'导入亚马逊返回的处理报告，按 SKU 和字段看懂每条问题。',roles:['admin','editor','viewer']},
  import:{id:'import',title:'上传模板',description:'保存原始模板，自动读取来源，只需确认店铺名称。',roles:['admin','editor']},
  batch:{id:'batch-panel',title:'同地区批量生成',description:'为同地区店铺生成模板，可选择保存入库或下载 ZIP。',roles:['admin','editor']},
  inventory:{id:'inventory',title:'库存识别',description:'识别商品类型，直接获取对应类别表格。',roles:['admin','editor','viewer']},
  aliases:{id:'aliases-panel',title:'商品叫法归类',description:'批量整理不同叫法，自动匹配对应商品类别。',roles:['admin','editor','viewer']},
  shops:{id:'shop-roster-panel',title:'店铺管理',description:'管理店铺及各地区的卖家记号。',roles:['admin']},
  ai:{id:'ai-settings-panel',title:'AI 设置',description:'独立配置 AI 服务商、模型与 API 密钥。',roles:['admin']},
  members:{id:'admin-panel',title:'成员管理',description:'添加团队成员，分配管理员、编辑者和只读权限。',roles:['admin']},
  backup:{id:'backup-panel',title:'备份恢复',description:'备份模板和店铺目录，或将备份恢复到空模板库。',roles:['admin','editor','viewer']}
};
function showPage(raw=location.hash.slice(1)){
  const alias=Object.keys(PAGES).find(key=>PAGES[key].id===raw);
  const requested=Object.prototype.hasOwnProperty.call(PAGES,raw)?raw:alias||'library';
  const key=PAGES[requested].roles.includes(currentRole)?requested:'library';
  for(const [name,page] of Object.entries(PAGES))$(page.id).hidden=name!==key;
  for(const link of document.querySelectorAll('nav [data-route]')){
    const allowed=PAGES[link.dataset.route].roles.includes(currentRole);link.hidden=!allowed;
    link.classList.toggle('active',link.dataset.route===key);
    if(link.dataset.route===key)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  }
  $('library-stats').hidden=key!=='library';
  document.querySelector('.heading h1').textContent=PAGES[key].title;
  document.querySelector('.heading p').textContent=PAGES[key].description;
  $('breadcrumb').textContent='工作空间 / '+PAGES[key].title;
  $('workspace').dataset.page=key;document.title=PAGES[key].title+' · 模板仓';
  if(key!==shownPage){replayClass(document.querySelector('.heading'),'enter');if(key==='library')replayRows();shownPage=key;}
  moveNavIndicator();
  if(key==='fill')updateFillActions();
  if(key==='report'){updateReportTemplates();if(reportData)renderReport();}
  if(raw!==key)history.replaceState(null,'','#'+key);
}
window.addEventListener('hashchange',()=>{if(!$('workspace').hidden){showPage();window.scrollTo({top:0,behavior:'instant'});}});
// Motion helpers: CSS does the animating, these only restart it or place the sliding nav marker.
let shownPage='',rowsTimer,navFrame;
const reducedMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
function replayClass(element,name){if(!element)return;element.classList.remove(name);void element.offsetWidth;element.classList.add(name);}
function replayRows(){clearTimeout(rowsTimer);replayClass($('rows'),'rows-enter');rowsTimer=setTimeout(()=>$('rows').classList.remove('rows-enter'),1400);}
function moveNavIndicator(){
  const nav=document.querySelector('aside nav');if(!nav)return;
  let marker=nav.querySelector('.nav-indicator');
  if(!marker){marker=document.createElement('span');marker.className='nav-indicator';marker.setAttribute('aria-hidden','true');nav.append(marker);}
  const active=nav.querySelector('a.active');
  if(!active||!active.offsetParent){marker.hidden=true;return;}
  marker.hidden=false;marker.style.width=active.offsetWidth+'px';marker.style.height=active.offsetHeight+'px';
  marker.style.transform='translate('+active.offsetLeft+'px,'+active.offsetTop+'px)';
  if(!nav.classList.contains('has-indicator'))requestAnimationFrame(()=>nav.classList.add('has-indicator'));
  if(nav.scrollWidth>nav.clientWidth+1)nav.scrollTo({left:Math.max(0,active.offsetLeft-(nav.clientWidth-active.offsetWidth)/2),behavior:reducedMotion()?'auto':'smooth'});
}
window.addEventListener('resize',()=>{cancelAnimationFrame(navFrame);navFrame=requestAnimationFrame(moveNavIndicator);});
function countTo(element,value){
  const from=Number(element.textContent)||0;cancelAnimationFrame(element.countFrame);
  if(from===value||reducedMotion()){element.textContent=value;return;}
  const start=performance.now(),step=now=>{const t=Math.min(1,(now-start)/800);element.textContent=Math.round(from+(value-from)*(1-Math.pow(1-t,3)));if(t<1)element.countFrame=requestAnimationFrame(step);};
  element.countFrame=requestAnimationFrame(step);
}
function notify(text, error=false) {
  clearTimeout(timer);const box=$('status');box.textContent=text;box.className=error?'error':'';
  box.style.display='none';void box.offsetWidth;box.style.display='block';
  timer=setTimeout(()=>{box.classList.add('leaving');timer=setTimeout(()=>{box.style.display='none';box.classList.remove('leaving');},240);},8000);
}
async function request(url, options={}) {
  const res=await fetch(url,options);
  const data=await res.json();
  if(res.status===401) { $('workspace').hidden=true; $('login-screen').hidden=false; }
  if(!res.ok) throw new Error(data.error || '操作失败');
  return data;
}
function cell(row,text,cls) {
  const td=document.createElement('td');td.textContent=text;if(cls)td.className=cls;row.append(td);return td;
}
function productTypeInfo(template){if(template.metadata)return template.metadata;if(Array.isArray(template.product_types))return template;return inventoryReport?.template_supports?.[template.id]||templates.find(t=>t.id===template.id)?.metadata||{};}
function declaredProductTypes(template){return productTypeInfo(template).product_types||[];}
function productTypeLabels(template){return productTypeInfo(template).product_type_labels||{};}
function templateCategories(template){const codes=declaredProductTypes(template),labels=productTypeLabels(template);return [...new Set([template.category,...codes,...codes.map(code=>labels[code]||code)])].filter(Boolean);}
function formatProductTypes(template){const labels=productTypeLabels(template);return declaredProductTypes(template).map(code=>labels[code]&&labels[code]!==code?labels[code]+'（'+code+'）':code).join('、');}
function renderQuickMarkets(){for(const button of document.querySelectorAll('[data-quick-market]')){const active=button.dataset.quickMarket===$('filter-market').value;button.classList.toggle('active-market',active);button.setAttribute('aria-pressed',String(active));}}
function render() {
  const query=$('search').value.trim().toLowerCase(), market=$('filter-market').value,shop=$('filter-shop').value,categoryFilter=$('filter-category').value,versionFilter=$('filter-version').value,typeFilter=$('filter-table-type').value;
  const rows=templates.filter(t=>(!typeFilter||(t.table_type||'upload')===typeFilter)&&(!market||t.marketplace===market)&&(!shop||(t.shop||'通用模板')===shop)&&(!categoryFilter||templateCategories(t).includes(categoryFilter))&&(!versionFilter||t.version===versionFilter)&&[t.original_name,t.download_name||t.original_name,t.category,t.version,t.id,t.shop||'通用模板',TABLE_TYPES[t.table_type||'upload'],...templateCategories(t)].some(v=>v.toLowerCase().includes(query)));
  renderQuickMarkets();$('filter-result-summary').textContent='找到 '+rows.length+' 条模板 / 全部 '+templates.length+' 条';visibleTemplates=rows;$('template-bulk-actions').hidden=false;$('delete-selected').hidden=currentRole==='viewer';document.querySelector('.template-select-column').hidden=false;
  $('rows').replaceChildren();$('count').textContent=rows.length;$('empty').hidden=rows.length>0;
  $('empty').querySelector('h3').textContent=templates.length?'没有找到匹配的模板':'模板库还空着';
  $('empty').querySelector('p').textContent=templates.length?'调整关键词、店铺、地区、类别或版本后重试。':'导入第一份原始模板，以后无需反复查找。';
  for(const t of rows) {
    const tr=document.createElement('tr');{const td=cell(tr,'');td.className='template-select-cell';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=selectedTemplates.has(t.id);checkbox.disabled=libraryDownloading;checkbox.setAttribute('aria-label','选择模板 '+t.original_name);checkbox.addEventListener('change',()=>{if(checkbox.checked)selectedTemplates.add(t.id);else selectedTemplates.delete(t.id);updateTemplateSelection();});td.append(checkbox);}const name=cell(tr,'');
    const title=document.createElement('div');title.className='filename';title.textContent=t.download_name||t.original_name;title.title=(t.download_name?'下载名：'+t.download_name+'\n原文件名：':'')+t.original_name;
    const hash=document.createElement('small');hash.className='hash';hash.textContent='SHA-256 '+t.sha256.slice(0,14)+'…';hash.title=t.sha256;
    name.append(title,hash);
    const meta=cell(tr,'');const store=document.createElement('small');store.className='hash';store.textContent=(t.shop||'通用模板')+(t.metadata?.account_hint?' · 账户 '+t.metadata.account_hint:'')+(t.account_scope_unknown?' · 需绑定店铺':'');meta.append(store);const badge=document.createElement('span');badge.className='badge';badge.textContent=t.marketplace;meta.append(badge,document.createTextNode(t.category));
    if(declaredProductTypes(t).length){const details=document.createElement('details');details.className='supported-product-types';const summary=document.createElement('summary');summary.textContent='支持 '+declaredProductTypes(t).length+' 种商品类型';const list=document.createElement('p');list.textContent=formatProductTypes(t);details.append(summary,list);meta.append(details);}
    for(const warning of t.metadata?.product_type_warnings||[]){const line=document.createElement('small');line.className='product-type-warning';line.textContent=warning;meta.append(line);}
    const typeCell=cell(tr,'');
    if(currentRole==='viewer'){typeCell.textContent=TABLE_TYPES[t.table_type||'upload'];}
    else{
      const select=document.createElement('select');select.className='table-type-select';select.title='调整登记类型，Excel 内容保持原样';select.setAttribute('aria-label','表格类型 '+t.original_name);for(const [value,label] of Object.entries(TABLE_TYPES))select.add(new Option(label,value));select.value=t.table_type||'upload';
      select.addEventListener('change',async()=>{if(busy){select.value=t.table_type||'upload';notify('请等待当前操作完成',true);return;}const value=select.value;busy=true;select.disabled=true;try{await postJSON('/api/template/type',{id:t.id,table_type:value});inventoryAICache={};resetInventory(false);await load();notify('已改为'+TABLE_TYPES[value]);}catch(e){select.value=t.table_type||'upload';notify(e.message,true);}finally{busy=false;select.disabled=false;updateInventoryActions();}});typeCell.append(select);
    }
    cell(tr,t.version);cell(tr,new Date(t.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}));
    const action=cell(tr,'');const link=document.createElement('a');link.href='/api/download/'+encodeURIComponent(t.id);link.textContent='↓ 下载原件';action.append(link);if(currentRole!=='viewer'){const fill=document.createElement('button');fill.className='text-button';fill.type='button';fill.textContent='填写';fill.addEventListener('click',()=>{if(busy)return;openFillTemplate(t);});action.append(fill);}if(currentRole!=='viewer'){const move=document.createElement('button');move.className='text-button';move.textContent='设置店铺';move.addEventListener('click',async()=>{const shop=prompt('输入归属店铺；含账户标识的同来源模板会一起调整。无账户来源的模板才可设为“通用模板”',t.shop||'通用模板');if(shop===null)return;try{await postJSON('/api/template-shop',{id:t.id,shop});inventoryAICache={};resetInventory(false);await load();notify('已更新店铺归属');}catch(e){notify(e.message,true);}});action.append(move);const remove=document.createElement('button');remove.className='text-button delete-template';remove.textContent='删除';remove.setAttribute('aria-label','删除模板 '+t.original_name);remove.addEventListener('click',async()=>{
      if(busy){notify('请等待当前操作完成',true);return;}
      if(!confirm('确认删除以下模板？\n\n文件：'+t.original_name+'\n店铺：'+(t.shop||'通用模板')+'\n站点 / 类目：'+t.marketplace+' / '+t.category+'\n表格类型：'+TABLE_TYPES[t.table_type||'upload']+'\n版本：'+t.version+'\n\n确认后移除该文件和库内记录。已有备份可用于恢复。'))return;
      busy=true;remove.disabled=true;
      try{const result=await postJSON('/api/template/delete',{id:t.id});inventoryAICache={};resetInventory(false);await load();notify(result.cleanup_pending?'已从模板库移除；磁盘文件清理尚未完成':'模板已删除');}
      catch(e){notify(e.message,true);}finally{busy=false;remove.disabled=false;}
    });action.append(remove);}$('rows').append(tr);
  }
  updateTemplateSelection();
}
async function load() {
  selectedTemplates.clear();
  const [stored,registeredShops]=await Promise.all([request('/api/templates'),request('/api/shops')]);templates=stored;
  countTo($('total'),templates.length);countTo($('markets'),new Set(templates.map(t=>t.marketplace)).size);countTo($('categories'),new Set(templates.map(t=>t.category)).size);replayRows();
  const selected=$('filter-market').value;$('filter-market').replaceChildren(new Option('全部站点',''));
  [...new Set(['AE','SA',...templates.map(t=>t.marketplace)])].sort().forEach(m=>$('filter-market').add(new Option(m,m)));
  $('filter-market').value=selected;const selectedShop=$('filter-shop').value;const shops=[...new Set(['通用模板',...registeredShops,...templates.map(t=>t.shop||'通用模板')])].sort();$('filter-shop').replaceChildren(new Option('全部店铺',''),...shops.map(shop=>new Option(shop,shop)));$('filter-shop').value=selectedShop;$('shop-options').replaceChildren(...shops.map(shop=>new Option(shop,shop)));inventoryShopNames=shops;updateInventoryShopOptions();for(const [id,field,label] of [['filter-category','category','全部类别'],['filter-version','version','全部版本']]){const value=$(id).value;$(id).replaceChildren(new Option(label,''),...[...new Set(field==='category'?templates.flatMap(templateCategories):templates.map(t=>t[field]))].sort().map(item=>new Option(item,item)));$(id).value=value;}render();updateBatchVersion();updateFillOptions();updateReportTemplates();
}
// Raw binary upload: JSON metadata followed by the file itself; the browser streams the file without base64.
function fileSize(bytes){return bytes<1024*1024?Math.max(1,Math.round(bytes/1024))+' KB':(bytes/1024/1024).toFixed(1)+' MB';}
function fileRequest(file,meta={},maxMB=32) {
  if(file.size>maxMB*1024*1024)throw new Error('当前文件 '+(file.size/1024/1024).toFixed(1)+' MB，超过此操作 '+maxMB+' MB 上限');
  const head=new TextEncoder().encode(JSON.stringify({filename:file.name,...meta}));
  return {method:'POST',headers:{'Content-Type':'application/x-template-upload','X-Template-Request':'1','X-Template-Meta-Length':String(head.length)},body:new Blob([head,file])};
}
async function sendFile(url,file,meta={}) {
  const inventory=url.startsWith('/api/inventory/');
  if(inventory&&/\.xls$/i.test(file.name)){const header=new Uint8Array(await file.slice(0,4).arrayBuffer());if(!(header[0]===0x50&&header[1]===0x4b&&header[2]===3&&header[3]===4))throw new Error('旧版 .xls 请先用 Excel/WPS 另存为 .xlsx 并保留图片，不能只改扩展名');}
  return request(url,fileRequest(file,meta,inventory?100:32));
}
const FILE_PROMPT='点击选择或拖入亚马逊原始模板（可一次多选）';
$('file').addEventListener('change',()=>$('file-name').textContent=$('file').files[0]?.name||FILE_PROMPT);
$('version').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
$('upload').addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;if(inspectingTemplate){notify('正在读取模板来源，请稍后保存',true);return;}busy=true;const button=event.submitter||event.target.querySelector('[type=submit]');button.disabled=true;button.textContent='正在保存…';
  try {
    const result=await sendFile('/api/upload',$('file').files[0],{table_type:$('template-table-type').value,shop:$('template-shop').value,marketplace:$('marketplace').value,category:$('category').value,version:$('version').value});
    await load();notify(result.reused?'此模板已保存，已复用现有版本。':'保存成功，原件已校验。建议下载完整备份。');
    $('file').value='';$('file-name').textContent=FILE_PROMPT;
  } catch(e){notify(e.message,true);}finally{busy=false;button.disabled=false;button.textContent='＋ 保存模板';}
});
$('restore').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  if(busy){notify('请等待当前操作完成',true);event.target.value='';return;}
  if(templates.length){notify('恢复仅支持空模板库，现有模板不会被覆盖。',true);event.target.value='';return;}
  busy=true;
  try{const result=await sendFile('/api/restore',file);await load();notify('恢复成功，共 '+result.count+' 份模板。');}catch(e){notify(e.message,true);}finally{busy=false;event.target.value='';}
});
const filterTemplates=()=>{selectedTemplates.clear();render();};$('search').addEventListener('input',filterTemplates);$('filter-table-type').addEventListener('change',filterTemplates);$('filter-market').addEventListener('change',filterTemplates);$('filter-shop').addEventListener('change',filterTemplates);$('filter-category').addEventListener('change',filterTemplates);$('filter-version').addEventListener('change',filterTemplates);$('clear-template-filters').addEventListener('click',()=>{for(const id of ['search','filter-table-type','filter-market','filter-shop','filter-category','filter-version'])$(id).value='';filterTemplates();});
for(const button of document.querySelectorAll('[data-quick-market]'))button.addEventListener('click',()=>{$('filter-market').value=button.dataset.quickMarket;filterTemplates();});
$('refresh').addEventListener('click',()=>load().catch(e=>notify(e.message,true)));
async function enter() {
  const me=await request('/api/me');currentRole=me.role;
  resetInventory();$('inventory-file').value='';
  $('account').textContent=me.username+' · '+({admin:'管理员',editor:'编辑者',viewer:'只读'}[me.role]);
  if($('account-avatar'))$('account-avatar').textContent=(me.username||'?').trim().slice(0,1).toUpperCase();
  $('login-screen').hidden=true;$('workspace').hidden=false;
  showPage();await loadAISettings();if(me.role==='admin')await loadShopEditor();
  document.querySelector('.restore-label').hidden=me.role!=='admin';
  await load();await loadAliases();
}
async function postJSON(url,body) {return request(url,{method:'POST',headers:{'Content-Type':'application/json','X-Template-Request':'1'},body:JSON.stringify(body)});}
$('login-form').addEventListener('submit',async e=>{
  e.preventDefault();const b=e.submitter;b.disabled=true;$('login-error').textContent='';
  try{await postJSON('/api/login',{username:$('username').value,password:$('password').value});$('password').value='';await enter();}catch(err){$('login-error').textContent=err.message;}finally{b.disabled=false;}
});
$('logout').addEventListener('click',async()=>{try{await postJSON('/api/logout',{});templates=[];render();$('workspace').hidden=true;$('login-screen').hidden=false;}catch(e){notify(e.message,true);}});
$('user-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{await postJSON('/api/users',{username:$('new-user').value,password:$('new-password').value,role:$('new-role').value});$('user-form').reset();notify('团队成员已创建');}catch(err){notify(err.message,true);}finally{b.disabled=false;}});
enter().catch(e=>{if(e.message!=='请先登录')$('login-error').textContent=e.message;});

// Inventory analysis remains local to the logged-in browser session.
let inventoryReport=null;
const inventorySelected=new Set();
let inventoryDownloading=false;
let inventoryAICache={};
let inventoryLocalRecheck=false;
let inventoryOverrides={};
let inventoryTemplateOverrides={};
let inspectedFile=null;
let inspectedSheet=null;
let inventoryFileRevision=0,inventoryInspectRunning=false,inventoryInspectQueued=false;
const statusLabels={available:'已有登记模板',missing:'需要补充模板',review:'类目待确认'};
for(const option of $('marketplace').options) $('inventory-market').add(new Option(option.text,option.value));
function updateInventoryActions(){
  for(const id of ['inventory-new-file','inventory-next-file-button','inspect-inventory','reread-inventory'])if($(id))$(id).disabled=busy||inventoryDownloading;
  const submit=$('inventory-form').querySelector('button[type="submit"]');if(submit)submit.disabled=busy||inventoryDownloading||!inspectedFile;
}
function updateInventoryFileInfo(){
  const file=$('inventory-file').files[0];$('inventory-file-info').textContent=file?file.name+' · '+(file.size/1024/1024).toFixed(1)+' MB'+(/\.xls$/i.test(file.name)?' · 旧版格式请另存为 .xlsx':''):'选择文件后自动读取表头';
}
function resetInventory(clearMapping=true) {
  inventoryReport=null;inventorySelected.clear();inventoryLocalRecheck=false;if($('inventory-progress'))$('inventory-progress').hidden=true;$('inventory-download-bar').hidden=true;$('inventory-result').hidden=true;$('inventory-inputs').open=true;$('inventory-inputs').querySelector('summary').textContent='上传库存与识别设置';
  for(const id of ['inventory-groups','inventory-warnings','inventory-summary','inventory-context'])$(id).replaceChildren();
  if(clearMapping){
    inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};inspectedFile=null;inspectedSheet=null;$('inventory-mapping').hidden=true;
    $('inventory-sheet').replaceChildren();$('inventory-header').value='1';$('inventory-preview').textContent='';
    for(const field of ['sku','title','category'])$('inventory-'+field).replaceChildren(new Option('未提供 / 不使用',''));
    $('column-settings').open=false;
  }
  setInventoryStep(clearMapping?1:($('inventory-mapping').hidden?1:2));updateInventoryActions();
}
function chooseNextInventory(){
  if(busy||inventoryDownloading){notify('请等待当前操作完成',true);return;}
  // Use a separate picker so Cancel preserves the current file and result.
  $('inventory-next-file').value='';$('inventory-next-file').click();
}
for(const id of ['inventory-new-file','inventory-next-file-button'])$(id).addEventListener('click',chooseNextInventory);
$('inventory-next-file').addEventListener('change',()=>{
  const file=$('inventory-next-file').files[0];if(!file)return;
  if(busy||inventoryDownloading){notify('请等待当前操作完成',true);return;}
  const transfer=new DataTransfer();transfer.items.add(file);$('inventory-file').files=transfer.files;$('inventory-file').dispatchEvent(new Event('change',{bubbles:true}));
  $('inventory-inputs').scrollIntoView({block:'start',behavior:'smooth'});
});
$('inventory-file').addEventListener('change',()=>{
  inventoryFileRevision++;resetInventory();updateInventoryFileInfo();$('inventory-ai-consent').checked=false;
  if($('inventory-file').files.length)inspectInventory();
});
$('inventory-table-type').addEventListener('change',()=>{updateInventoryShopOptions();inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};resetInventory(false);$('inventory-ai-consent').checked=false;});
$('inventory-market').addEventListener('change',()=>{updateInventoryShopOptions();inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};resetInventory(false);});
for(const id of ['inventory-sku','inventory-title','inventory-category']) $(id).addEventListener('change',()=>{inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};resetInventory(false);});
$('inventory-header').addEventListener('input',()=>{inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};resetInventory(false);});
async function inspectInventory(reuseSelection=false) {
  const file=$('inventory-file').files[0];
  if(!file){notify('请先选择库存表',true);return;}
  if(busy){if(inventoryInspectRunning&&!reuseSelection){inventoryInspectQueued=true;return;}notify('请等待当前操作完成',true);return;}
  busy=true;inventoryInspectRunning=true;inventoryInspectQueued=false;updateInventoryActions();
  const revision=inventoryFileRevision;const button=$('inspect-inventory');button.textContent='正在读取表头…';
  try {
    const options=reuseSelection?{sheet:$('inventory-sheet').value,header_row:Number($('inventory-header').value)}:{};
    const info=await sendFile('/api/inventory/inspect',file,options);
    if($('inventory-file').files[0]!==file||revision!==inventoryFileRevision)return;
    resetInventory();inspectedFile=file;inspectedSheet=info.sheet;
    $('inventory-sheet').replaceChildren(...info.sheets.map(s=>new Option(s.name+' · '+s.rows+' 个非空行',s.name)));
    $('inventory-sheet').value=info.sheet;$('inventory-header').value=info.header_row;
    for(const field of ['sku','title','category']){
      const select=$('inventory-'+field);select.replaceChildren(new Option('未提供 / 不使用',''));
      info.headers.forEach((h,i)=>select.add(new Option('第 '+(i+1)+' 列 · '+(h||'空表头'),String(i))));
      select.value=info.mapping[field]===null?'':String(info.mapping[field]);
    }
    $('inventory-mapping').hidden=false;setInventoryStep(2);$('column-settings').open=(info.mapping.title===null&&info.mapping.category===null)||info.warnings.some(w=>w.includes('当前表头'));
    $('inventory-preview').textContent='表头已识别，请核对工作表、表头行号及列映射'+(info.warnings.length?'；'+info.warnings.join('；'):'');
    notify('表头已读取，请核对工作表、表头行和列映射');
  }catch(e){if(revision===inventoryFileRevision&&$('inventory-file').files[0]===file)notify(e.message,true);}
  finally{busy=false;inventoryInspectRunning=false;button.textContent=inspectedFile?'重新读取库存表':'读取库存表';updateInventoryActions();if(inventoryInspectQueued&&$('inventory-file').files.length)inspectInventory();else inventoryInspectQueued=false;}
}
$('inspect-inventory').addEventListener('click',()=>inspectInventory());
$('reread-inventory').addEventListener('click',()=>inspectInventory(true));
$('inventory-sheet').addEventListener('change',async()=>{
  if(busy){$('inventory-sheet').value=inspectedSheet||'';notify('请等待当前操作完成',true);return;}
  const file=$('inventory-file').files[0];if(!file)return;
  busy=true;inventoryInspectRunning=true;inventoryInspectQueued=false;resetInventory(false);updateInventoryActions();
  const revision=inventoryFileRevision;
  try {
    const info=await sendFile('/api/inventory/inspect',file,{sheet:$('inventory-sheet').value});
    if($('inventory-file').files[0]!==file||revision!==inventoryFileRevision)return;
    inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};inspectedSheet=info.sheet;$('inventory-header').value=info.header_row;
    for(const field of ['sku','title','category']){
      const select=$('inventory-'+field);select.replaceChildren(new Option('未提供 / 不使用',''));
      info.headers.forEach((h,i)=>select.add(new Option('第 '+(i+1)+' 列 · '+(h||'空表头'),String(i))));
      select.value=info.mapping[field]===null?'':String(info.mapping[field]);
    }
    $('inventory-preview').textContent='表头已识别，请核对列映射后开始匹配类别模板。';
  }catch(e){if(revision===inventoryFileRevision){resetInventory();notify(e.message,true);}}finally{busy=false;inventoryInspectRunning=false;updateInventoryActions();if(inventoryInspectQueued&&$('inventory-file').files.length)inspectInventory();else inventoryInspectQueued=false;}
});
function templateLink(target,template){
  const a=document.createElement('a');a.href='/api/download/'+encodeURIComponent(template.id);a.textContent='下载 '+template.filename+' · '+template.version+' · '+(template.shop||'通用模板');target.append(a);
}
function categoryResults(report){
  const groups=new Map();
  for(const item of report.items){
    const category=item.category_key||item.suggested_category||'无法识别类目';
    if(!groups.has(category))groups.set(category,{category,rows:[],statuses:new Set(),candidates:new Map()});
    const group=groups.get(category);group.rows.push(item.row);group.statuses.add(item.status);
    if(item.template)group.candidates.set(item.template.id,item.template);
  }
  return [...groups.values()].map(group=>{
    const candidates=[...group.candidates.values()].sort((a,b)=>{
      const own=Number(b.shop===report.shop)-Number(a.shop===report.shop);if(own)return own;
      const date=id=>templates.find(t=>t.id===id)?.created_at||'';return date(b.id).localeCompare(date(a.id));
    });
    return {...group,template:candidates[0]||null,status:group.statuses.has('review')?'review':group.statuses.has('available')?'available':'missing'};
  });
}
function renderInventory(report) {
  inventoryReport=report;const groups=categoryResults(report);
  inventorySelected.clear();for(const group of groups)if(group.template)inventorySelected.add(group.template.id);
  updateInventoryDownload(groups);
  $('inventory-summary').textContent='识别到 '+groups.filter(g=>g.category!=='无法识别类目').length+' 类商品 · '+inventoryDownloadIds().length+' 份不同表格';
  $('inventory-context').textContent='库存：'+report.filename+' · '+TABLE_TYPES[report.table_type||'upload']+' · 目标店铺：'+report.shop+' · 地区：'+report.marketplace+'。每类一行；相同文件只下载一次。';
  $('inventory-warnings').replaceChildren(...report.warnings.map(w=>{const p=document.createElement('p');p.textContent=w;return p;}));
  $('inventory-groups').replaceChildren(...groups.map(group=>{
    const box=document.createElement('article');box.className='analysis-group category-template '+group.status;
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.className='inventory-file-select';checkbox.disabled=!group.template;checkbox.checked=!!group.template;checkbox.setAttribute('aria-label','选择 '+group.category+' 对应表格');
    if(group.template){checkbox.dataset.templateId=group.template.id;checkbox.addEventListener('change',()=>{if(checkbox.checked)inventorySelected.add(group.template.id);else inventorySelected.delete(group.template.id);updateInventoryDownload(groups);});}
    const body=document.createElement('div');body.className='category-body';const heading=document.createElement('div');heading.className='category-heading';
    const title=document.createElement('h3');title.textContent=group.category==='无法识别类目'?'待识别的商品类别':group.category;
    const badge=document.createElement('span');badge.className='category-state '+group.status;badge.textContent=group.template?(group.status==='review'?'候选 · 待确认':'可下载'):(group.status==='review'?'待识别':'需补充');heading.append(title,badge);body.append(heading);
    const actions=document.createElement('div');actions.className='category-actions';
    if(group.template){
      const file=document.createElement('p');file.className='template-version';file.textContent=group.template.filename+' · '+group.template.version+' · '+(group.template.shop||'通用模板');if(declaredProductTypes(group.template).length>1)file.textContent+=' · 支持 '+declaredProductTypes(group.template).length+' 类';file.title=file.textContent+(declaredProductTypes(group.template).length?'\n支持：'+formatProductTypes(group.template):'');body.append(file);
      const link=document.createElement('a');link.href='/api/download/'+encodeURIComponent(group.template.id);link.className='button secondary';link.textContent=group.status==='review'?'下载候选':'下载表格';actions.append(link);
    }else{
      const detail=document.createElement('p');detail.className='hint';detail.textContent=group.status==='review'?'当前店铺未匹配到模板，请确认类别或启用 AI 图文识别。':'当前店铺和地区暂无对应模板。';body.append(detail);
      if(group.status==='missing'&&currentRole!=='viewer'){const link=document.createElement('a');link.href='#import';link.className='text-button';link.textContent='补充模板';actions.append(link);}
      else {const label=document.createElement('span');label.className='hint';label.textContent='暂无文件';actions.append(label);}
    }
    if(group.status==='review'&&group.category!=='无法识别类目'&&!group.category.includes(' / ')){
      const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='确认或修正类别';
      const input=document.createElement('input');input.className='confirm-category';input.value=group.category;input.maxLength=120;input.setAttribute('aria-label','确认类别 '+group.category);
      const button=document.createElement('button');button.className='text-button';button.type='button';button.textContent='确认类别并重新匹配';
      button.addEventListener('click',()=>{if(busy)return;const category=input.value.trim();if(!category){notify('请输入确认后的类别',true);return;}for(const row of group.rows){inventoryOverrides[String(row)]=category;delete inventoryTemplateOverrides[String(row)];if(group.template?.matched_product_type&&declaredProductTypes(group.template).includes(group.template.matched_product_type))inventoryTemplateOverrides[String(row)]={template_id:group.template.id,product_type:group.template.matched_product_type};}inventoryLocalRecheck=true;$('inventory-form').requestSubmit($('inventory-form').querySelector('button[type="submit"]'));});
      details.append(summary,input,button);body.append(details);
    }
    box.append(checkbox,body,actions);return box;
  }));
  $('inventory-result').hidden=false;setInventoryStep(3);$('inventory-inputs').open=false;$('inventory-inputs').querySelector('summary').textContent='更换库存表 / 调整识别设置';
}
$('inventory-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;
  const file=$('inventory-file').files[0];
  if(file!==inspectedFile){notify('文件已变化，请重新读取表头',true);return;}
  const useAI=$('inventory-use-ai').checked&&!inventoryLocalRecheck;const localRecheck=inventoryLocalRecheck;inventoryLocalRecheck=false;
  const button=event.submitter;const controls=[...$('inventory-form').querySelectorAll('input,select,button')];const disabledBefore=controls.map(control=>control.disabled);controls.forEach(control=>control.disabled=true);busy=true;const before=button.textContent;button.textContent=useAI?'AI 正在分析…':'正在分析…';
  const jobId='inv-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);const stopProgress=startInventoryProgress(jobId);const revision=inventoryFileRevision;updateInventoryActions();
  try{
    if(useAI&&!$('inventory-ai-consent').checked)throw new Error('请先同意将所选商品资料发送到 AI 服务商');
    const mapping={};for(const field of ['sku','title','category']){const value=$('inventory-'+field).value;mapping[field]=value===''?null:Number(value);}
    const report=await sendFile('/api/inventory/analyze',file,{table_type:$('inventory-table-type').value,job_id:jobId,use_ai_cache:$('inventory-ai-cache')?.checked!==false,shop:$('inventory-shop').value,marketplace:$('inventory-market').value,sheet:$('inventory-sheet').value,header_row:Number($('inventory-header').value),mapping,category_overrides:inventoryOverrides,template_overrides:inventoryTemplateOverrides,use_ai:useAI,include_images:$('inventory-images').checked,ai_consent:$('inventory-ai-consent').checked,ai_limit:Number($('inventory-ai-limit').value)});
    if($('inventory-file').files[0]!==file||revision!==inventoryFileRevision)return;
    if(inventoryReport?.alias_revision!==report.alias_revision)inventoryAICache={};
    if(localRecheck)reuseAISuggestions(report);
    for(const item of report.items)if(item.ai)inventoryAICache[item.row]=structuredClone(item);
    if($('inventory-file').files[0]===file){stopProgress(report);renderInventory(report);notify('识别完成，可一键下载全部或勾选表格下载');}
  }catch(e){stopProgress(null,e.message);notify(e.message,true);}finally{stopProgress();busy=false;controls.forEach((control,i)=>control.disabled=disabledBefore[i]);button.textContent=before;updateInventoryActions();}
});
$('export-inventory').addEventListener('click',()=>{
  if(!inventoryReport)return;
  const report=inventoryReport;
  const rows=[['店铺','站点','表格类型','商品类别','匹配状态','对应模板','模板版本']];
  for(const group of categoryResults(report))rows.push([report.shop,report.marketplace,TABLE_TYPES[report.table_type||'upload'],group.category,statusLabels[group.status],group.template?.filename||'',group.template?.version||'']);
  const csv=rows.map(row=>row.map(value=>{let s=String(value??'');if(/^[\s]*[=+\-@]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';}).join(',')).join('\r\n');
  const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='模板补充清单-'+report.marketplace+'.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('logout').addEventListener('click',()=>{inventoryFileRevision++;inventoryInspectQueued=false;$('inventory-next-file').value='';$('inventory-file').value='';updateInventoryFileInfo();$('ai-key').value='';$('inventory-ai-consent').checked=false;resetInventory();});

async function loadAISettings(){
  const info=await request('/api/ai/settings');window.currentAI=info;
  $('ai-provider').value=info.provider;$('ai-model').value=info.model;$('ai-region').value=info.region||'cn';$('ai-region-label').hidden=info.provider!=='qwen';
  $('ai-state').textContent=info.configured?'已配置 · '+info.providers[info.provider].name:'尚未配置 API 密钥';
  $('ai-ready-label').textContent='（'+info.providers[info.provider].name+' / '+info.model+' · '+(info.configured?'已配置':'待配置')+'）';
  if($('ai-model-help'))$('ai-model-help').textContent='推荐模型：'+info.providers[info.provider].model+'。已有保存配置不会自动替换，修改后请保存并测试。';
  $('inventory-images').disabled=!info.vision;if(!info.vision)$('inventory-images').checked=false;
}
$('ai-provider').addEventListener('change',()=>{const models={qwen:'qwen3-vl-plus',openai:'gpt-4.1-mini',deepseek:'deepseek-chat'};$('ai-model').value=models[$('ai-provider').value];$('ai-key').value='';$('ai-region-label').hidden=$('ai-provider').value!=='qwen';});
$('ai-settings-form').addEventListener('submit',async e=>{
  e.preventDefault();const b=e.submitter;b.disabled=true;
  try{await postJSON('/api/ai/settings',{provider:$('ai-provider').value,model:$('ai-model').value,api_key:$('ai-key').value.trim(),region:$('ai-region').value});$('ai-key').value='';await loadAISettings();notify('AI 配置已更新；请在库存分析中按需开启');}catch(err){notify(err.message,true);}finally{b.disabled=false;}
});

function reuseAISuggestions(report){
  let reused=0;
  for(const item of report.items){const prior=inventoryAICache[item.row];if(item.status!=='available'&&!item.confirmed&&prior&&prior.title===item.title&&prior.original_category===item.original_category){Object.assign(item,structuredClone(prior));reused++;}}
  if(!reused)return;
  const groups=new Map();
  for(const item of report.items){const key=JSON.stringify([item.status,item.suggested_category,item.template?.id]);if(!groups.has(key))groups.set(key,{status:item.status,category:item.suggested_category||'无法识别类目',count:0,template:item.template,rows:[]});const group=groups.get(key);group.count++;group.rows.push(item.row);}
  report.groups=[...groups.values()];for(const status of ['available','missing','review'])report.summary[status]=report.items.filter(item=>item.status===status).length;
  report.warnings.push('本次人工确认重查未再次调用 AI；沿用同一库存表尚未确认的 '+reused+' 行 AI 建议。');
}

$('inventory-shop').addEventListener('input',()=>{inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};resetInventory(false);$('inventory-ai-consent').checked=false;});

$('file').addEventListener('change',async()=>{
  // Several files, or a batch list already open: handled by the batch import below.
  const file=$('file').files[0];if(!file||$('file').files.length>1||uploadBatch.rows.length)return;inspectingTemplate=true;$('template-source-info').textContent='正在读取模板内部来源…';
  try{
    const result=await sendFile('/api/template/inspect',file);if($('file').files[0]!==file)return;
    const m=result.metadata;const parts=[];
    if(m.account_fingerprint){
      parts.push('账户来源 '+m.account_hint);$('template-shop').value=result.recognized_shop||'';
      parts.push(result.recognized_shop?'已识别归属：'+result.recognized_shop:'首次见到此账户，请填写真实店铺名称后保存；后续同来源模板将提示归属');
    }else parts.push('未找到可用账户来源标识，请手动确认店铺归属');
    if(m.marketplace){$('marketplace').value=m.marketplace;parts.push('主站点 '+m.marketplace);}
    if(m.product_types?.length===1)$('category').value=productTypeLabels(m)[m.product_types[0]]||m.product_types[0];
    if(m.product_types?.length>1)$('category').value='多商品类型模板';
    if(m.product_types?.length)parts.push('声明支持 '+m.product_types.length+' 类：'+formatProductTypes(m));
    parts.push(...(m.product_type_warnings||[]));
    if(m.field_count)parts.push(m.field_count+' 个字段，数据从第 '+m.data_row+' 行开始');
    if(m.has_prefilled_data)parts.push('注意：已有预填内容，原件未清除，使用前须核对');
    $('template-source-info').textContent=parts.join('；');
  }catch(e){if($('file').files[0]===file)$('template-source-info').textContent='来源读取失败：'+e.message+'；请勿猜测店铺归属';}
  finally{if($('file').files[0]===file)inspectingTemplate=false;}
});

// ---- Batch import: pick or drop several original templates, review the detected values, save all ----
const BATCH_MAX_FILES=200,BATCH_READERS=3;
const uploadBatch={rows:[],nextId:1,saving:false};
const BATCH_STATUS={reading:['正在读取…',''],invalid:['无法导入','status-missing'],incomplete:['待补充','status-review'],ready:['可保存','status-available'],saving:['正在保存…',''],saved:['✓ 已入库','status-available'],reused:['✓ 已存在，已复用','status-available'],error:['保存失败','status-missing']};
const batchLocked=row=>['reading','saving','saved','reused','invalid'].includes(row.status);
function shanghaiDate(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
$('upload-batch-version').value=shanghaiDate();
function batchCategory(m){
  const types=declaredProductTypes(m);if(!types.length)return '';
  const labels=productTypeLabels(m),names=types.map(code=>labels[code]||code);
  // Name multi-type templates after their types so several of them for one shop do not collide.
  return (names.length===1?names[0]:names.slice(0,3).join('、')+(names.length>3?' 等 '+names.length+' 类':'')).slice(0,120);
}
function batchKey(row){return [row.shop.trim(),row.marketplace,row.category.trim(),row.version.trim(),row.table_type].join('\u0000');}
function batchValidate(){
  const keys=new Map(),owners=new Map();
  for(const row of uploadBatch.rows){
    if(row.status==='reading'||row.status==='invalid')continue;
    const key=batchKey(row);if(!keys.has(key))keys.set(key,row);
    if(row.fingerprint&&row.shop.trim()&&!owners.has(row.fingerprint))owners.set(row.fingerprint,row);
  }
  for(const row of uploadBatch.rows){
    if(batchLocked(row))continue;
    const problems=[],missing=[['shop','店铺'],['marketplace','站点'],['category','类目'],['version','版本']].filter(([k])=>!row[k].trim()).map(([,label])=>label);
    if(missing.length)problems.push('请填写'+missing.join('、'));
    if(['shop','category','version'].some(k=>row[k].trim().length>120))problems.push('店铺、类目或版本不能超过 120 字');
    if(row.meta?.marketplace&&row.marketplace&&row.marketplace!==row.meta.marketplace)problems.push('模板内部站点为 '+row.meta.marketplace);
    if(row.fingerprint&&row.shop.trim()==='通用模板')problems.push('含账户来源的模板须指定具体店铺');
    const owner=row.fingerprint&&owners.get(row.fingerprint);
    if(owner&&owner!==row&&row.shop.trim()&&owner.shop.trim()!==row.shop.trim())problems.push('同一账户来源（'+row.hint+'）须归属同一店铺：'+owner.shop.trim());
    const first=keys.get(batchKey(row));
    if(!missing.length&&first&&first!==row)problems.push('与“'+first.file.name+'”的店铺、站点、类目、版本和类型完全相同，请改类目或版本');
    row.problems=problems;
    if(row.status!=='error'||problems.length)row.status=problems.length?'incomplete':'ready';
  }
}
function batchSummary(){
  const count=status=>uploadBatch.rows.filter(r=>r.status===status).length;
  const done=count('saved')+count('reused'),pending=count('ready')+count('error');
  const parts=['共 '+uploadBatch.rows.length+' 份'];
  for(const [status,label] of [['reading','读取中'],['ready','可保存'],['incomplete','待补充'],['invalid','无法导入'],['error','保存失败']])if(count(status))parts.push(label+' '+count(status));
  if(done)parts.push('已入库 '+done);
  $('upload-batch-summary').textContent=parts.join(' · ');
  $('upload-batch-save').disabled=uploadBatch.saving||!pending||count('reading')>0;
  $('upload-batch-save').textContent=uploadBatch.saving?'正在保存…':'＋ 全部保存'+(pending?'（'+pending+' 份）':'');
  $('upload-batch-clear').disabled=uploadBatch.saving;
}
function batchStatusCell(row){
  const td=row.cells.status;td.replaceChildren();
  const [label,cls]=BATCH_STATUS[row.status];const b=document.createElement('b');b.textContent=label;if(cls)b.className=cls;
  const line=document.createElement('div');line.className='upload-batch-status-line';line.append(b);if(row.remove)line.append(row.remove);td.append(line);
  const detail=row.status==='incomplete'?row.problems.join('；'):row.status==='error'?row.saveError+(row.problems?.length?'':'；可修改后重试'):row.message;
  if(detail){const small=document.createElement('small');small.className='hint';small.textContent=detail;td.append(small);}
}
function batchRefresh(){
  batchValidate();
  for(const row of uploadBatch.rows){
    batchStatusCell(row);
    const locked=batchLocked(row)||uploadBatch.saving;
    for(const input of Object.values(row.inputs))input.disabled=locked;
    row.remove.disabled=uploadBatch.saving||row.status==='saving';
  }
  batchSummary();
}
function batchSetMode(){
  const on=uploadBatch.rows.length>0;
  $('upload-batch').hidden=!on;$('upload').querySelector('.fields').hidden=on;$('template-source-info').hidden=on;
  $('file').required=!on;
  $('file-name').textContent=on?'继续点击或拖入更多模板（已列出 '+uploadBatch.rows.length+' 份）':FILE_PROMPT;
}
function batchRenderRow(row){
  const tr=document.createElement('tr');row.inputs={};row.cells={};
  const name=document.createElement('td');name.className='upload-batch-file';
  const title=document.createElement('b');title.textContent=row.file.name;title.title=row.file.name;
  const info=document.createElement('small');info.className='hint';info.textContent=fileSize(row.file.size)+(row.info?' · '+row.info:'');
  const status=document.createElement('div');status.className='upload-batch-status';status.setAttribute('aria-live','polite');row.cells.status=status;
  name.append(title,info,status);row.cells.info=info;tr.append(name);
  const field=(key,element)=>{
    const label={shop:'归属店铺',marketplace:'站点',category:'登记类目',table_type:'表格类型',version:'版本标签'}[key];
    element.value=row[key];element.setAttribute('aria-label',label+'：'+row.file.name);
    element.addEventListener(element.tagName==='SELECT'?'change':'input',()=>{
      row[key]=element.value;row.saveError='';if(row.status==='error')row.status='ready';
      if(key==='shop'){row.shopTouched=true;batchFollowShop(row);}
      if(key==='version')row.versionTouched=true;if(key==='table_type')row.typeTouched=true;
      batchRefresh();
    });
    const td=document.createElement('td');td.dataset.label=label;td.append(element);tr.append(td);row.inputs[key]=element;
  };
  const shop=document.createElement('input');shop.setAttribute('list','shop-options');shop.maxLength=120;shop.placeholder='填写真实店铺名称';field('shop',shop);
  const market=document.createElement('select');market.append(new Option('选择站点',''),...[...$('marketplace').options].filter(o=>o.value).map(o=>new Option(o.text,o.value)));field('marketplace',market);
  const category=document.createElement('input');category.maxLength=120;category.placeholder='例如：行李箱';field('category',category);
  const type=document.createElement('select');type.className='table-type-select';type.append(new Option('上传表格','upload'),new Option('改价表格','price_update'));field('table_type',type);
  const version=document.createElement('input');version.maxLength=120;field('version',version);
  const remove=document.createElement('button');remove.type='button';remove.className='text-button upload-batch-remove';remove.textContent='移除';remove.setAttribute('aria-label','移除 '+row.file.name);
  remove.addEventListener('click',()=>{uploadBatch.rows=uploadBatch.rows.filter(r=>r!==row);tr.remove();batchSetMode();batchRefresh();});
  row.remove=remove;row.tr=tr;
  return tr;
}
// Typing a shop name for one template fills every other template from the same seller account.
function batchFollowShop(source){
  if(!source.fingerprint)return;
  for(const row of uploadBatch.rows)if(row!==source&&row.fingerprint===source.fingerprint&&!row.shopTouched&&!batchLocked(row)){row.shop=source.shop;row.inputs.shop.value=source.shop;}
}
async function batchInspect(row){
  try{
    const result=await sendFile('/api/template/inspect',row.file);const m=result.metadata||{};
    row.meta=m;row.fingerprint=m.account_fingerprint||'';row.hint=m.account_hint||'';
    if(row.fingerprint){
      const sibling=uploadBatch.rows.find(r=>r!==row&&r.fingerprint===row.fingerprint&&r.shop.trim());
      row.shop=result.recognized_shop||sibling?.shop||'';
    }else row.shop='通用模板';
    row.marketplace=m.marketplace||'';row.category=batchCategory(m);
    const parts=[];
    if(row.fingerprint)parts.push('账户来源 '+row.hint+(result.recognized_shop?'（已识别：'+result.recognized_shop+'）':'（首次出现，请填写店铺）'));else parts.push('无账户来源标识');
    if(m.product_types?.length)parts.push(m.product_types.length+' 类商品：'+formatProductTypes(m));
    parts.push(...(m.product_type_warnings||[]));
    if(m.has_prefilled_data)parts.push('已有预填内容，使用前须核对');
    row.info=parts.join('；');row.status='ready';
  }catch(e){row.status='invalid';row.message=e.message;}
  if(!uploadBatch.rows.includes(row))return;
  for(const key of ['shop','marketplace','category'])row.inputs[key].value=row[key];
  row.cells.info.textContent=fileSize(row.file.size)+(row.info?' · '+row.info:'');
}
async function batchAdd(files){
  const seen=new Set(uploadBatch.rows.map(r=>r.file.name+'|'+r.file.size+'|'+r.file.lastModified));const added=[];let skipped=0;
  for(const file of files){
    const id=file.name+'|'+file.size+'|'+file.lastModified;
    if(seen.has(id)){skipped++;continue;}
    if(uploadBatch.rows.length>=BATCH_MAX_FILES){notify('一次最多列出 '+BATCH_MAX_FILES+' 份模板，请先保存后再继续添加',true);break;}
    seen.add(id);
    const row={id:uploadBatch.nextId++,file,status:'reading',message:'',saveError:'',problems:[],info:'',meta:null,fingerprint:'',hint:'',shop:'',marketplace:'',category:'',table_type:$('upload-batch-type').value,version:$('upload-batch-version').value.trim()||shanghaiDate(),shopTouched:false,versionTouched:false,typeTouched:false};
    if(!/\.(xlsx|xlsm)$/i.test(file.name)){row.status='invalid';row.message='仅支持 .xlsx / .xlsm 原始模板';}
    else if(file.size>32*1024*1024){row.status='invalid';row.message='超过 32 MB 上限';}
    uploadBatch.rows.push(row);$('upload-batch-rows').append(batchRenderRow(row));added.push(row);
  }
  if(skipped)notify('已跳过 '+skipped+' 份重复选择的文件');
  batchSetMode();batchRefresh();
  // Read a few files at a time; each read only inspects the workbook, nothing is stored yet.
  const queue=added.filter(r=>r.status==='reading');
  await Promise.all(Array.from({length:Math.min(BATCH_READERS,queue.length)},async()=>{
    while(queue.length){const row=queue.shift();await batchInspect(row);batchRefresh();}
  }));
}
$('file').addEventListener('change',()=>{
  const files=[...$('file').files];if(!files.length)return;
  if(files.length===1&&!uploadBatch.rows.length)return; // single template: the original form handles it
  if(uploadBatch.saving){notify('正在保存，请稍后再添加',true);$('file').value='';return;}
  $('file').value='';batchAdd(files).catch(e=>notify(e.message,true));
});
$('upload-batch-type').addEventListener('change',()=>{for(const row of uploadBatch.rows)if(!batchLocked(row)&&!row.typeTouched){row.table_type=$('upload-batch-type').value;row.inputs.table_type.value=row.table_type;}batchRefresh();});
$('upload-batch-version').addEventListener('input',()=>{const value=$('upload-batch-version').value;for(const row of uploadBatch.rows)if(!batchLocked(row)&&!row.versionTouched){row.version=value;row.inputs.version.value=value;}batchRefresh();});
$('upload-batch-clear').addEventListener('click',()=>{if(uploadBatch.saving)return;uploadBatch.rows=[];$('upload-batch-rows').replaceChildren();batchSetMode();batchSummary();});
$('upload-batch-save').addEventListener('click',async()=>{
  if(busy||uploadBatch.saving){notify('请等待当前操作完成',true);return;}
  batchValidate();const pending=uploadBatch.rows.filter(r=>r.status==='ready'||r.status==='error');if(!pending.length)return;
  busy=true;uploadBatch.saving=true;let saved=0,reused=0,failed=0;
  try{
    // One at a time: the first template from a new seller account binds it to the shop for the rest.
    for(const row of pending){
      if(!uploadBatch.rows.includes(row))continue;
      row.status='saving';row.saveError='';batchRefresh();
      try{
        const result=await sendFile('/api/upload',row.file,{table_type:row.table_type,shop:row.shop.trim(),marketplace:row.marketplace,category:row.category.trim(),version:row.version.trim()});
        row.status=result.reused?'reused':'saved';row.message=result.reused?'内容相同的模板已在库中':'原件已校验';result.reused?reused++:saved++;
      }catch(e){row.status='error';row.saveError=e.message;failed++;}
      batchRefresh();
    }
  }finally{
    busy=false;uploadBatch.saving=false;batchRefresh();
    await load().catch(e=>notify(e.message,true));
  }
  notify('批量导入完成：新入库 '+saved+' 份'+(reused?'，复用 '+reused+' 份':'')+(failed?'，失败 '+failed+' 份（可修改后重试）':'')+'。建议下载完整备份。',failed>0);
});

$('logout').addEventListener('click',()=>{$('shop-roster-text').value='';$('shop-roster-status').textContent='';});

let batchFile=null,batchSource=null,batchReading=false,batchReadSequence=0,batchLastResult=null,batchDownloading=false;
function batchDateBase(){
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
  const value=Object.fromEntries(parts.map(part=>[part.type,part.value]));return value.year+'-'+value.month+'-'+value.day+'-批量';
}
function batchRequestSignature(){return JSON.stringify([batchSource?.metadata.marketplace,$('batch-table-type').value,$('batch-category').value.trim(),[...$('batch-targets').querySelectorAll('input:checked')].map(c=>c.value).sort(),batchMode()!=='download']);}
function batchRetry(){return batchLastResult?.report.failed>0&&batchLastResult.sourceFile===$('batch-file').files[0]&&batchLastResult.requestSignature===batchRequestSignature();}
function updateBatchVersion(){
  const automatic=$('batch-auto-version').checked;$('batch-version').readOnly=automatic;
  if(!automatic){$('batch-version-help').textContent='自定义版本：同店铺、地区、类目和表格类型的同版本不同内容会提示冲突。';return;}
  if(batchRetry()){$('batch-version').value=batchLastResult.report.version;$('batch-version-help').textContent='本批有入库失败项，重试沿用此版本，已成功的店铺会复用。';return;}
  const base=batchDateBase(),shops=new Set([...$('batch-targets').querySelectorAll('input:checked')].map(c=>c.value)),market=batchSource?.metadata.marketplace,category=$('batch-category').value.trim(),kind=$('batch-table-type').value;
  let highest=0;
  for(const t of templates){if(!shops.has(t.shop)||t.marketplace!==market||t.category!==category||(t.table_type||'upload')!==kind)continue;if(t.version===base)highest=Math.max(highest,1);else if(t.version.startsWith(base+'-')&&/^[0-9]{1,9}$/.test(t.version.slice(base.length+1)))highest=Math.max(highest,Number(t.version.slice(base.length+1)));}
  $('batch-version').value=highest?base+'-'+String(highest+1).padStart(2,'0'):base;
  $('batch-version-help').textContent='同一天连续生成自动递增为 -02、-03；多人同时操作时以生成结果中的实际版本为准。取消勾选可自定义。';
}
$('batch-version').value=batchDateBase();
$('batch-auto-version').addEventListener('change',updateBatchVersion);
$('batch-category').addEventListener('input',updateBatchVersion);
$('batch-table-type').addEventListener('change',updateBatchControls);
const batchModeLabels={library:'生成并保存到模板库',both:'生成、保存并下载 ZIP',download:'生成并下载 ZIP'};
function batchMode(){return document.querySelector('input[name="batch-output"]:checked')?.value||'library';}
function updateBatchControls(){
  updateBatchVersion();
  const available=[...$('batch-targets').querySelectorAll('input:not(:disabled)')],selected=available.filter(c=>c.checked);
  $('batch-selection-count').textContent=batchSource?'已选 '+selected.length+' / '+available.length+' 家店铺':'尚未读取店铺';
  $('batch-select-all').disabled=busy||batchReading||!available.length;$('batch-clear-all').disabled=busy||batchReading||!selected.length;
  $('batch-submit').disabled=busy||batchReading||!batchSource||!selected.length;
  if(!busy)$('batch-submit').textContent=batchModeLabels[batchMode()];
  $('batch-mode-note').textContent={library:'完成后可在模板库查找，也可手动下载本批模板。',both:'生成后保存到模板库，并自动下载一份 ZIP。',download:'生成后只下载 ZIP，模板库中不会增加记录。'}[batchMode()];
}
function clearBatchResult(){batchLastResult=null;$('batch-result').hidden=true;$('batch-result-rows').replaceChildren();}
function selectBatchTargets(selected=true){
  if(!batchSource)return;for(const checkbox of $('batch-targets').querySelectorAll('input'))checkbox.checked=selected&&!checkbox.disabled;updateBatchControls();
}
async function readBatchSource(){
  const sequence=++batchReadSequence,file=$('batch-file').files[0];batchFile=null;batchSource=null;clearBatchResult();$('batch-targets').replaceChildren();
  if(!file){batchReading=false;$('batch-source').textContent='选择文件后自动读取地区与来源店铺。';updateBatchControls();return;}
  batchReading=true;$('batch-source').textContent='正在读取源模板和同地区店铺…';updateBatchControls();
  try{
    const source=await sendFile('/api/template/inspect',file);const m=source.metadata;
    if(sequence!==batchReadSequence)return;
    if(!m.marketplace||!m.account_fingerprint)throw new Error('源模板缺少可识别的主站点或卖家记号');
    const targets=await request('/api/shops/targets?marketplace='+encodeURIComponent(m.marketplace));
    if(sequence!==batchReadSequence)return;batchFile=file;batchSource=source;
    if(m.product_types?.length===1)$('batch-category').value=productTypeLabels(m)[m.product_types[0]]||m.product_types[0];
    if(m.product_types?.length>1)$('batch-category').value='多商品类型模板';
    $('batch-source').textContent='源店铺：'+(source.recognized_shop||'未登记来源')+' · 地区：'+m.marketplace+' · '+m.field_count+' 个字段'+(m.product_types?.length?' · 声明支持 '+m.product_types.length+' 类：'+formatProductTypes(m):'')+(m.has_prefilled_data?' · 已有商品内容会保留':'')+(m.product_type_warnings?.length?' · '+m.product_type_warnings.join('；'):'');
    for(const target of targets){const label=document.createElement('label');const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=target.shop;checkbox.disabled=!target.can_generate;const name=document.createElement('span');name.textContent=target.shop+' · '+target.account_hint+(target.can_generate?'':'（需补全卖家记号）');name.title=name.textContent;label.append(checkbox,name);$('batch-targets').append(label);}
    if(!targets.length)$('batch-targets').textContent='该地区暂无店铺记录，请先在店铺管理中登记。';
    selectBatchTargets();
  }catch(e){if(sequence===batchReadSequence){$('batch-source').textContent=e.message;notify(e.message,true);}}
  finally{if(sequence===batchReadSequence){batchReading=false;updateBatchControls();}}
}
$('batch-file').addEventListener('change',readBatchSource);$('batch-select-all').addEventListener('click',()=>selectBatchTargets());$('batch-clear-all').addEventListener('click',()=>selectBatchTargets(false));$('batch-targets').addEventListener('change',updateBatchControls);
for(const radio of document.querySelectorAll('input[name="batch-output"]'))radio.addEventListener('change',updateBatchControls);
function saveDownloadBlob(blob,filename){
  const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function renderBatchResult(result){
  batchLastResult=result;const report=result.report,ids=[...new Set((report.items||[]).filter(item=>item.template_id&&['saved','reused'].includes(item.library_status)).map(item=>item.template_id))];result.ids=ids;
  $('batch-result-context').textContent=report.marketplace+' · '+TABLE_TYPES[report.table_type||'upload']+' · '+report.category+' · 版本：'+report.version;
  $('batch-result').hidden=false;$('batch-result').classList.toggle('has-errors',report.failed>0);$('batch-result-title').textContent=report.failed?'部分入库失败，请核对':'生成完成';
  $('batch-result-counts').replaceChildren(...[['已生成',report.generated],['新入库',report.saved],['复用已有',report.reused],['入库失败',report.failed]].map(([label,value])=>{const box=document.createElement('div'),number=document.createElement('strong'),name=document.createElement('span');number.textContent=value;name.textContent=label;box.append(number,name);return box;}));
  $('batch-status').textContent=report.failed?'有 '+report.failed+' 份未成功入库。'+(result.blob?'副本已保留在 ZIP 中，请查看包内报告。':'请查看逐店结果；检查磁盘后可用相同源文件和版本重试，成功项会复用。'):result.blob?(report.save_requested?'已按店铺保存，并开始下载 ZIP。':'ZIP 已开始下载，本次未保存到模板库。'):'已保存到共享模板库，可直接查看或按需下载，不会自动弹出下载窗口。';
  $('batch-open-library').hidden=!report.saved&&!report.reused;
  $('batch-download-result').hidden=!result.blob&&!ids.length;$('batch-download-result').textContent=result.blob?'再次下载 ZIP':'下载本批已入库模板';
  $('batch-download-report').hidden=!report.items;
  $('batch-result-details').hidden=!report.items;$('batch-result-details').open=!!report.failed&&!!report.items;$('batch-result-rows').replaceChildren();
  const labels={saved:'已入库',reused:'复用已有',failed:'入库失败',not_requested:'仅下载'};
  for(const item of report.items||[]){const row=document.createElement('tr');cell(row,item.shop);cell(row,labels[item.library_status]||item.library_status);const file=cell(row,item.output_name);file.className='batch-result-file';file.title=item.output_name;cell(row,item.error||'');$('batch-result-rows').append(row);}
}
$('batch-form').addEventListener('submit',async e=>{
  e.preventDefault();if(busy||batchReading){notify('请等待当前操作完成',true);return;}
  const file=$('batch-file').files[0];if(file!==batchFile||!batchSource){notify('请先读取有效源模板',true);return;}
  const shops=[...$('batch-targets').querySelectorAll('input:checked')].map(c=>c.value);if(!shops.length){notify('请选择至少一家同地区目标店铺',true);return;}
  updateBatchVersion();const signature=batchRequestSignature(),automatic=$('batch-auto-version').checked&&!batchRetry();
  const mode=batchMode(),marketplace=batchSource.metadata.marketplace;
  busy=true;const controls=[...$('batch-form').querySelectorAll('input,select,button')];const states=controls.map(c=>c.disabled);controls.forEach(c=>c.disabled=true);$('batch-submit').textContent='正在生成和校验…';
  try{
    const payload={table_type:$('batch-table-type').value,shops,category:$('batch-category').value,version:$('batch-version').value,auto_version:automatic,save_to_library:mode!=='download',download_zip:mode!=='library'};
    const response=await fetch('/api/template/batch',fileRequest(file,payload));
    if(!response.ok){const error=await response.json();throw new Error(error.error||'生成失败');}
    let result;
    if(mode==='library'){
      if(!(response.headers.get('Content-Type')||'').includes('application/json'))throw new Error('服务器仍是旧版本，请完整覆盖新版程序并重启后重试');
      result={report:await response.json(),blob:null};
    }else{
      const blob=await response.blob(),report={marketplace,table_type:payload.table_type,category:payload.category.trim(),version:response.headers.get('X-Batch-Version')?decodeURIComponent(response.headers.get('X-Batch-Version')):payload.version.trim(),save_requested:payload.save_to_library};
      for(const [field,header] of [['generated','Generated'],['saved','Saved'],['reused','Reused'],['failed','Failed']])report[field]=Number(response.headers.get('X-'+header+'-Count'));
      const disposition=(response.headers.get('Content-Disposition')||'').match(/filename\*=UTF-8''([^;]+)/i);result={report,blob,filename:disposition?decodeURIComponent(disposition[1]):TABLE_TYPES[payload.table_type]+'_多店铺_'+marketplace+'.zip'};saveDownloadBlob(blob,result.filename);
    }
    result.sourceFile=file;result.requestSignature=signature;renderBatchResult(result);
    if(payload.save_to_library){try{await load();}catch(error){notify('生成已完成；模板库列表刷新失败，请稍后刷新查看',true);return;}}
    notify(result.report.failed?'部分入库失败，请查看本次结果':mode==='library'?'已保存到模板库，需要时再下载':'批量生成完成，ZIP 内附生成报告',result.report.failed>0);
  }catch(err){notify(err.message,true);}finally{busy=false;controls.forEach((c,i)=>c.disabled=states[i]);updateBatchControls();}
});
$('batch-download-result').addEventListener('click',async()=>{
  if(!batchLastResult||batchDownloading)return;if(busy){notify('请等待当前操作完成',true);return;}
  const result=batchLastResult;if(result.blob){saveDownloadBlob(result.blob,result.filename);return;}
  batchDownloading=true;$('batch-download-result').disabled=true;
  try{
    const response=await fetch('/api/templates/download',{method:'POST',headers:{'Content-Type':'application/json','X-Template-Request':'1'},body:JSON.stringify({ids:result.ids})});
    if(!response.ok){const error=await response.json();throw new Error(error.error||'下载失败');}
    const match=(response.headers.get('Content-Disposition')||'').match(/filename\*=UTF-8''([^;]+)/i);saveDownloadBlob(await response.blob(),match?decodeURIComponent(match[1]):'本批模板.zip');
  }catch(error){notify(error.message,true);}finally{batchDownloading=false;$('batch-download-result').disabled=false;}
});
$('batch-download-report').addEventListener('click',()=>{if(batchLastResult?.report.items)saveDownloadBlob(new Blob([JSON.stringify(batchLastResult.report,null,2)],{type:'application/json;charset=utf-8'}),'生成与入库报告.json');});
$('batch-open-library').addEventListener('click',()=>{if(!batchLastResult)return;const report=batchLastResult.report;$('search').value='';$('filter-shop').value='';for(const [id,value] of [['filter-table-type',report.table_type||'upload'],['filter-market',report.marketplace],['filter-category',report.category],['filter-version',report.version]])$(id).value=value;render();});
$('logout').addEventListener('click',()=>{$('batch-file').value='';batchReadSequence++;batchFile=null;batchSource=null;batchReading=false;$('batch-targets').replaceChildren();clearBatchResult();updateBatchControls();});
updateBatchControls();

function drawShopEditor(rows){
  $('shop-grid-body').replaceChildren();
  for(const row of rows.length?rows:[{shop:'',ae:'',sa:''}])addShopRow(row);
}
function addShopRow(value={shop:'',ae:'',sa:''}){
  const tr=document.createElement('tr');
  for(const key of ['shop','ae','sa']){const td=document.createElement('td');const input=document.createElement('input');input.value=value[key]||'';input.dataset.field=key;input.maxLength=key==='shop'?120:31;input.placeholder=key==='shop'?'店铺名称':'没有此地区可留空';input.setAttribute('aria-label',key==='shop'?'店铺名称':key==='ae'?'阿联酋卖家记号':'沙特卖家记号');td.append(input);tr.append(td);}
  const td=document.createElement('td');const button=document.createElement('button');button.type='button';button.className='text-button';button.textContent='移除此行';button.title='仅移出当前待保存表格，不删除已登记店铺';button.onclick=()=>tr.remove();td.append(button);tr.append(td);$('shop-grid-body').append(tr);
}
async function loadShopEditor(){drawShopEditor(await request('/api/shops/roster'));}
$('shop-add-row').addEventListener('click',()=>addShopRow());
$('shop-toggle-paste').addEventListener('click',()=>{$('shop-paste-area').hidden=!$('shop-paste-area').hidden;if(!$('shop-paste-area').hidden)$('shop-roster-text').focus();});
$('shop-preview').addEventListener('click',async()=>{
  const button=$('shop-preview');button.disabled=true;
  try{const rows=await postJSON('/api/shops/preview',{text:$('shop-roster-text').value});drawShopEditor(rows);$('shop-roster-text').value='';$('shop-paste-area').hidden=true;$('shop-roster-status').textContent='已填入 '+rows.length+' 家店铺，请核对“店铺／AE／SA”三列后点击保存。';}catch(e){$('shop-roster-status').textContent=e.message;notify(e.message,true);}finally{button.disabled=false;}
});
$('shop-roster-form').addEventListener('submit',async e=>{
  e.preventDefault();if(busy)return;const b=e.submitter;b.disabled=true;
  try{
    const rows=[...$('shop-grid-body').rows].map(tr=>Object.fromEntries([...tr.querySelectorAll('input')].map(input=>[input.dataset.field,input.value])));
    const result=await postJSON('/api/shops/import',{rows});$('shop-roster-status').textContent='已保存 '+result.shops+' 家店铺、'+result.site_records+' 条地区记录。以后无需重复填写。';
    inventoryAICache={};resetInventory(false);await load();await loadShopEditor();notify('店铺已保存');
  }catch(e){$('shop-roster-status').textContent=e.message;notify(e.message,true);}finally{b.disabled=false;}
});

function setInventoryStep(step){for(const item of document.querySelectorAll('.inventory-flow li')){const value=Number(item.dataset.step);item.classList.toggle('current',value===step);item.classList.toggle('complete',value<step);if(value===step)item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');}}
$('inventory-use-ai').addEventListener('change',()=>{$('ai-advanced').open=$('inventory-use-ai').checked;});

function selectedTemplateIds(){return [...new Set(visibleTemplates.filter(t=>selectedTemplates.has(t.id)).map(t=>t.id))];}
function updateTemplateSelection(){
  const count=selectedTemplateIds().length,all=visibleTemplates.length>0&&count===visibleTemplates.length;
  $('template-selection-count').textContent=count?'已选择 '+count+' 份表格':'未选择表格';
  $('download-selected-templates').disabled=!count||libraryDownloading;$('download-selected-templates').textContent=libraryDownloading?'正在准备下载…':'↓ 下载所选'+(count?'（'+count+' 份）':'');
  $('delete-selected').disabled=!count||busy||libraryDownloading;$('delete-selected').textContent='删除所选'+(count?'（'+count+'）':'');
  $('select-all-template-files').disabled=!visibleTemplates.length||libraryDownloading||all;$('clear-template-selection').disabled=!count||libraryDownloading;
  const box=$('select-all-templates');box.checked=all;box.indeterminate=!all&&count>0;box.disabled=!visibleTemplates.length||libraryDownloading;
  for(const checkbox of $('rows').querySelectorAll('.template-select-cell input'))checkbox.disabled=libraryDownloading;
}
function selectVisibleTemplates(selected){
  if(libraryDownloading)return;for(const template of visibleTemplates){if(selected)selectedTemplates.add(template.id);else selectedTemplates.delete(template.id);}render();
}
$('select-all-templates').addEventListener('change',()=>selectVisibleTemplates($('select-all-templates').checked));
$('select-all-template-files').addEventListener('click',()=>selectVisibleTemplates(true));
$('clear-template-selection').addEventListener('click',()=>{if(libraryDownloading)return;selectedTemplates.clear();render();});
async function downloadTemplateFiles(ids,fallback='对应类别表格.zip'){
  const response=await fetch('/api/templates/download',{method:'POST',headers:{'Content-Type':'application/json','X-Template-Request':'1'},body:JSON.stringify({ids})});
  if(!response.ok){const error=await response.json();throw new Error(error.error||'下载失败');}
  const blob=await response.blob(),match=(response.headers.get('Content-Disposition')||'').match(/filename\*=UTF-8''([^;]+)/i);
  saveDownloadBlob(blob,match?decodeURIComponent(match[1]):fallback);
}
$('download-selected-templates').addEventListener('click',async()=>{
  const ids=selectedTemplateIds();if(!ids.length||libraryDownloading)return;
  libraryDownloading=true;updateTemplateSelection();
  try{await downloadTemplateFiles(ids);notify(ids.length+' 份表格已开始下载');}
  catch(error){notify(error.message,true);}finally{libraryDownloading=false;updateTemplateSelection();}
});
$('delete-selected').addEventListener('click',async()=>{
  if(busy||libraryDownloading||!selectedTemplates.size)return;
  const selected=templates.filter(t=>selectedTemplates.has(t.id));
  if(!confirm('确认删除所选 '+selected.length+' 条模板？\n\n'+selected.slice(0,8).map(t=>t.original_name+' · '+t.shop+' · '+t.marketplace+' · '+TABLE_TYPES[t.table_type||'upload']+' · '+t.version).join('\n')+(selected.length>8?'\n……另有 '+(selected.length-8)+' 条':'')+'\n\n确认后移除对应文件和库内记录。已有备份可用于恢复。'))return;
  busy=true;updateTemplateSelection();
  try{const result=await postJSON('/api/templates/delete',{ids:selected.map(t=>t.id)});inventoryAICache={};resetInventory(false);await load();notify('已删除 '+result.count+' 条模板'+(result.cleanup_pending?'；部分磁盘文件清理待完成':''));}catch(e){notify(e.message,true);}finally{busy=false;updateTemplateSelection();}
});

function updateInventoryShopOptions(){
  ensureInventoryShopPicker();const select=$('inventory-shop'),selected=select.value,market=$('inventory-market').value,tableType=$('inventory-table-type').value;select.disabled=false;
  select.replaceChildren(new Option('请选择实际店铺',''));
  for(const shop of inventoryShopNames){const count=templates.filter(t=>(t.shop||'通用模板')===shop&&(!market||t.marketplace===market)&&(t.table_type||'upload')===tableType&&!t.account_scope_unknown).length;const label=shop==='通用模板'?'通用模板（只查公共库 · '+count+' 份）':shop+' · '+count+' 份'+(market?' '+market:'')+'模板';select.add(new Option(label,shop));}
  select.value=selected;
  const count=templates.filter(t=>(t.shop||'通用模板')===selected&&(!market||t.marketplace===market)&&(t.table_type||'upload')===tableType&&!t.account_scope_unknown).length;
  $('inventory-shop-hint').textContent=!inventoryShopNames.some(shop=>shop!=='通用模板')?'当前数据目录没有已登记店铺，请先上传店铺原模板，或在店铺管理中添加；公共模板仍可手动选择。':!selected?'请先选择实际店铺，再识别库存。':selected==='通用模板'?'当前仅检索公共模板库，不会查询其他店铺专用模板。':'当前店铺“'+selected+'”已有 '+count+' 份'+(market?' '+market:'')+TABLE_TYPES[tableType]+'；识别后按类别匹配，并允许使用适用的通用模板。';
}
$('inventory-shop').addEventListener('change',()=>{const selected=$('inventory-shop').value;updateInventoryShopOptions();$('inventory-shop').value=selected;});

function ensureInventoryShopPicker(){
  const field=$('inventory-shop');if(!field)return;
  if(field.tagName!=='SELECT'){
    const select=document.createElement('select');select.id='inventory-shop';select.required=true;select.setAttribute('aria-label','选择目标店铺');select.add(new Option('请选择实际店铺',''));field.replaceWith(select);
  }
  if(!$('inventory-shop-hint')){const hint=document.createElement('p');hint.id='inventory-shop-hint';hint.className='hint';hint.textContent='请选择实际店铺，公共模板不会查询店铺专用文件。';const container=$('inventory-shop').closest('.fields');if(container)container.insertAdjacentElement('afterend',hint);}
  if(!$('inventory-download-bar')){const bar=document.createElement('div');bar.id='inventory-download-bar';bar.className='inventory-download-bar';bar.hidden=true;const button=document.createElement('button');button.id='download-inventory-templates';button.type='button';button.className='button';button.disabled=true;button.textContent='下载对应表格';const note=document.createElement('span');note.id='inventory-download-note';bar.append(button,note);const flow=document.querySelector('.inventory-flow');if(flow)flow.insertAdjacentElement('afterend',bar);}
  const bar=$('inventory-download-bar');
  if(bar&&!$('download-inventory-selected')){const button=document.createElement('button');button.id='download-inventory-selected';button.type='button';button.className='button secondary';button.disabled=true;button.textContent='下载所选';const label=document.createElement('label');label.className='inventory-select-all';const checkbox=document.createElement('input');checkbox.id='inventory-select-all';checkbox.type='checkbox';label.append(checkbox,'全选');bar.insertBefore(button,$('inventory-download-note'));bar.insertBefore(label,$('inventory-download-note'));}
  if(!$('inventory-ai-cache')){const label=document.createElement('label');const checkbox=document.createElement('input');checkbox.id='inventory-ai-cache';checkbox.type='checkbox';checkbox.checked=true;label.append(checkbox,'复用最近 30 分钟相同资料的识别结果');$('ai-advanced').append(label);}
  if(!$('inventory-progress')){const box=document.createElement('div');box.id='inventory-progress';box.className='inventory-progress';box.hidden=true;box.setAttribute('role','status');for(const [tag,id] of [['strong','inventory-progress-label'],['span','inventory-progress-time'],['progress','inventory-progress-meter'],['p','inventory-progress-detail']]){const element=document.createElement(tag);element.id=id;box.append(element);}$('inventory-inputs').insertAdjacentElement('afterend',box);}
  if(!$('inventory-new-file')){const button=document.createElement('button');button.id='inventory-new-file';button.type='button';button.className='button secondary';button.textContent='＋ 上传新库存';$('inventory').querySelector('.section-title').append(button);}
  if(!$('inventory-next-file')){const input=document.createElement('input');input.id='inventory-next-file';input.type='file';input.accept='.xlsx,.xlsm,.csv';input.hidden=true;$('inventory').append(input);}
  if(!$('inventory-next-file-button')){const button=document.createElement('button');button.id='inventory-next-file-button';button.type='button';button.className='button secondary';button.textContent='识别下一份库存';$('inventory-download-bar').insertBefore(button,$('inventory-download-note'));}
  if(!$('clear-inventory-selection')){const button=document.createElement('button');button.id='clear-inventory-selection';button.className='text-button';button.type='button';button.textContent='取消全选';$('inventory-download-bar').insertBefore(button,$('inventory-download-note'));}
  if(!$('ui-version')&&$('account')){const badge=document.createElement('span');badge.id='ui-version';badge.className='tag ui-version';badge.textContent=UI_REVISION;if($('version-slot'))$('version-slot').append(badge);else $('account').insertAdjacentElement('beforebegin',badge);}
}

function inventoryDownloadIds(){return inventoryReport?[...new Set(categoryResults(inventoryReport).filter(group=>group.template).map(group=>group.template.id))]:[];}
function updateInventoryDownload(groups=categoryResults(inventoryReport)){
  const ids=inventoryDownloadIds(),selected=ids.filter(id=>inventorySelected.has(id));$('inventory-download-bar').hidden=false;
  const button=$('download-inventory-templates');button.disabled=!ids.length||inventoryDownloading;button.textContent=ids.length?'↓ 下载全部（'+ids.length+' 份）':'暂无可下载表格';
  $('download-inventory-selected').disabled=!selected.length||inventoryDownloading;$('download-inventory-selected').textContent='下载所选（'+selected.length+'）';
  $('clear-inventory-selection').disabled=!selected.length||inventoryDownloading;
  const all=$('inventory-select-all');all.disabled=!ids.length||inventoryDownloading;all.checked=!!ids.length&&selected.length===ids.length;all.indeterminate=selected.length>0&&selected.length<ids.length;
  for(const checkbox of document.querySelectorAll('.inventory-file-select[data-template-id]')){checkbox.checked=inventorySelected.has(checkbox.dataset.templateId);checkbox.disabled=inventoryDownloading;}
  const reviews=groups.filter(group=>group.status==='review'&&group.template).length;
  $('inventory-download-note').textContent=!ids.length?'尚未匹配到文件，请核对店铺、地区及模板库。':(reviews?'含 '+reviews+' 类待确认候选，请核对后使用。':'')+'相同文件仅下载一次；单份为 Excel，多份为 ZIP。';
}
$('clear-inventory-selection').addEventListener('click',()=>{inventorySelected.clear();updateInventoryDownload();});
$('inventory-select-all').addEventListener('change',()=>{inventorySelected.clear();if($('inventory-select-all').checked)for(const id of inventoryDownloadIds())inventorySelected.add(id);updateInventoryDownload();});
async function downloadInventoryTemplates(selectedOnly=false){
  const ids=inventoryDownloadIds().filter(id=>!selectedOnly||inventorySelected.has(id));if(!ids.length||busy||inventoryDownloading)return;
  inventoryDownloading=true;updateInventoryDownload();updateInventoryActions();
  try{
    await downloadTemplateFiles(ids);notify(ids.length+' 份不同表格已开始下载');
  }catch(e){notify(e.message,true);}finally{inventoryDownloading=false;if(inventoryReport)updateInventoryDownload();updateInventoryActions();}
}
$('download-inventory-templates').addEventListener('click',()=>downloadInventoryTemplates());
$('download-inventory-selected').addEventListener('click',()=>downloadInventoryTemplates(true));

function startInventoryProgress(jobId){
  const started=Date.now();let active=true,timeout;
  const stages={upload:'正在读取并上传库存…',parse:'正在解析库存并匹配本地模板…',images:'正在读取待识别商品的图片…',prepare:'正在合并重复商品…',ai:'AI 正在识别…',complete:'AI 已完成处理，正在整理结果…',done:'正在显示识别结果…',error:'识别遇到问题，正在返回说明…'};
  const box=$('inventory-progress'),meter=$('inventory-progress-meter');box.hidden=false;
  function display(state){
    $('inventory-progress-label').textContent=stages[state.stage]||stages.ai;
    $('inventory-progress-time').textContent='已用时 '+Math.floor((Date.now()-started)/1000)+' 秒';
    if(state.total>0){meter.max=state.total;meter.value=state.completed||0;$('inventory-progress-detail').textContent='已处理 '+(state.completed||0)+' / '+state.total+' 行 · 缓存复用 '+(state.cached||0)+' 行 · 已发起 '+(state.calls||0)+' 批请求';}
    else{meter.removeAttribute('value');$('inventory-progress-detail').textContent='识别期间可以查看模板库，请等待本次结果。';}
  }
  async function poll(){try{const state=await request('/api/inventory/progress?job_id='+encodeURIComponent(jobId));if(active)display(state);}catch(e){/* The analysis request reports authentication/network errors. */}if(active)timeout=setTimeout(poll,1000);}
  display({stage:'upload'});timeout=setTimeout(poll,1000);
  return (report,error)=>{
    active=false;clearTimeout(timeout);
    if(report){const ai=report.ai_summary;meter.max=1;meter.value=1;$('inventory-progress-label').textContent=ai?'处理完成 · AI '+ai.completed+'/'+ai.requested+' 行':'本地识别完成';$('inventory-progress-time').textContent='用时 '+Math.floor((Date.now()-started)/1000)+' 秒';$('inventory-progress-detail').textContent=ai?'合并重复 '+ai.deduplicated+' 行 · 缓存复用 '+ai.cached+' 行 · 实际调用 '+ai.calls+' 批'+(ai.completed<ai.requested?'；未完成商品保留本地结果，请查看识别说明。':''):'请选择需要的类别表格，或直接下载全部。';}
    else if(error){meter.max=1;meter.value=0;$('inventory-progress-label').textContent='识别未完成';$('inventory-progress-detail').textContent=error;}
  };
}

$('inventory-ai-limit').max='500';$('inventory-ai-limit').value='500';

$('ai-region').addEventListener('change',()=>{$('ai-key').value='';$('ai-test-result').textContent='区域切换后，请填写该区域的 API 密钥并保存，再测试连接。';});
$('ai-test').addEventListener('click',async()=>{const button=$('ai-test');button.disabled=true;$('ai-test-result').textContent='正在测试已保存的模型配置…';try{const result=await postJSON('/api/ai/test',{});$('ai-test-result').textContent=result.message+' 模型：'+result.model+'；接口：'+result.endpoint;}catch(e){$('ai-test-result').textContent=e.message;notify(e.message,true);}finally{button.disabled=false;}});

if($('ai-use-recommended'))$('ai-use-recommended').addEventListener('click',()=>{const provider=$('ai-provider').value;const model=window.currentAI?.providers?.[provider]?.model||({qwen:'qwen3-vl-plus',openai:'gpt-4.1-mini',deepseek:'deepseek-chat'}[provider]);$('ai-model').value=model;notify('已填入 '+model+'，请保存配置后测试模型连接');});

// Team aliases use a preview revision so concurrent edits cannot overwrite each other.
let aliasDirectory={items:[],builtin:[],revision:null};
let aliasPreview=null;
let aliasMode='single';
let aliasInputRevision=0;
const aliasSelected=new Set();
const ALIAS_STATUS={new:'新增',exists:'已存在',builtin:'内置已支持',duplicate:'本批重复',conflict:'冲突',invalid:'无效'};
function invalidateAliasPreview(){aliasInputRevision++;aliasPreview=null;$('alias-preview').hidden=true;$('alias-save').disabled=true;}
function aliasFiltered(){const query=$('alias-search').value.trim().normalize('NFKC').toLowerCase();return aliasDirectory.items.filter(row=>(row.alias+' '+row.category).normalize('NFKC').toLowerCase().includes(query));}
function updateAliasSelection(){
  const rows=aliasFiltered();const count=rows.filter(row=>aliasSelected.has(row.alias_key)).length;
  $('alias-select-all').checked=rows.length>0&&count===rows.length;$('alias-select-all').indeterminate=count>0&&count<rows.length;
  $('alias-delete-selected').disabled=busy||!aliasSelected.size;$('alias-delete-selected').textContent='删除所选'+(aliasSelected.size?'（'+aliasSelected.size+'）':'');
}
function renderAliases(){
  const editable=currentRole!=='viewer';$('alias-editor').hidden=!editable;$('alias-selection-tools').hidden=!editable;$('alias-viewer-note').hidden=editable;
  $('alias-count').textContent=aliasDirectory.items.length;
  const rows=aliasFiltered();$('alias-rows').replaceChildren(...rows.slice(0,200).map(row=>{
    const tr=document.createElement('tr');const select=cell(tr,'');
    if(editable){const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=aliasSelected.has(row.alias_key);checkbox.setAttribute('aria-label','选择叫法 '+row.alias);checkbox.addEventListener('change',()=>{if(checkbox.checked)aliasSelected.add(row.alias_key);else aliasSelected.delete(row.alias_key);updateAliasSelection();});select.append(checkbox);}
    const a=cell(tr,''),c=cell(tr,''),actions=cell(tr,'');
    if(editable){
      const name=document.createElement('input'),category=document.createElement('input');for(const input of [name,category]){input.type='text';input.maxLength=120;}
      name.value=row.alias;name.setAttribute('aria-label','叫法 '+row.alias);category.value=row.category;category.setAttribute('aria-label','对应类别 '+row.alias);category.setAttribute('list','alias-category-options');a.append(name);c.append(category);
      const save=document.createElement('button');save.type='button';save.className='text-button';save.textContent='保存修改';save.addEventListener('click',()=>aliasMutation('/api/aliases/update',{key:row.alias_key,alias:name.value,category:category.value},'归类已更新'));
      const remove=document.createElement('button');remove.type='button';remove.className='text-button';remove.textContent='删除';remove.addEventListener('click',()=>{if(confirm('删除“'+row.alias+' → '+row.category+'”的归类？'))aliasMutation('/api/aliases/delete',{keys:[row.alias_key]},'归类已删除');});actions.append(save,remove);
    }else{a.textContent=row.alias;c.textContent=row.category;actions.textContent='只读';}
    return tr;
  }));
  $('alias-list-note').textContent=rows.length?'匹配 '+rows.length+' 条'+(rows.length>200?'，显示前 200 条；可搜索缩小范围，全选会选中全部筛选结果。':'。'):'没有自定义归类，可在上方批量添加。';updateAliasSelection();
  $('alias-builtin-rows').replaceChildren(...aliasDirectory.builtin.map(row=>{const p=document.createElement('p');p.textContent=row.category+'：'+row.aliases.join('、');return p;}));
  const categories=new Set([...aliasDirectory.builtin.map(row=>row.category),...aliasDirectory.items.map(row=>row.category),...templates.flatMap(templateCategories)]);
  $('alias-category-options').replaceChildren(...[...categories].sort().map(value=>{const option=document.createElement('option');option.value=value;return option;}));
}
async function loadAliases(){aliasDirectory=await request('/api/aliases');aliasSelected.clear();renderAliases();}
function renderAliasPreview(){
  const preview=aliasPreview;if(!preview)return;
  $('alias-preview').hidden=false;$('alias-preview-counts').textContent=Object.entries(ALIAS_STATUS).map(([key,label])=>label+' '+preview.counts[key]).join(' · ');
  $('alias-save').disabled=busy||!preview.can_save;$('alias-save').textContent='保存 '+preview.counts.new+' 个新叫法';
  const rows=preview.items.filter(row=>!$('alias-errors-only').checked||['conflict','invalid'].includes(row.status));
  $('alias-preview-rows').replaceChildren(...rows.slice(0,200).map(row=>{const tr=document.createElement('tr');if(['conflict','invalid'].includes(row.status))tr.className='alias-error';for(const value of [row.row,row.alias,row.category,ALIAS_STATUS[row.status]+(row.message?'：'+row.message:'')])cell(tr,value);return tr;}));
  $('alias-preview-note').textContent=(preview.counts.conflict||preview.counts.invalid?'请修正冲突或无效项后重新检查；整批不会部分保存。':preview.can_save?'新增叫法会一次保存；已存在、内置和重复项会自动合并。':'没有需要新增的叫法。')+(rows.length>200?' 当前显示前 200 条，可勾选“仅看冲突与无效项”。':'')+(preview.warnings?.length?' '+preview.warnings.join('；'):'');
}
async function aliasMutation(url,payload,message){
  if(busy)return;busy=true;$('alias-save').disabled=true;updateAliasSelection();
  try{const result=await postJSON(url,{revision:aliasDirectory.revision,...payload});invalidateAliasPreview();inventoryAICache={};inventoryOverrides={};inventoryTemplateOverrides={};resetInventory(false);resetFill();await load();await loadAliases();notify(message+(result.saved!==undefined?'，新增 '+result.saved+' 个叫法':''));}
  catch(e){notify(e.message,true);}finally{busy=false;if(aliasPreview)renderAliasPreview();updateAliasSelection();updateInventoryActions();}
}
function setAliasMode(mode){if(busy)return;aliasMode=mode;invalidateAliasPreview();$('alias-target-label').hidden=mode!=='single';$('alias-file-area').hidden=mode!=='table';$('alias-mode-single').setAttribute('aria-pressed',String(mode==='single'));$('alias-mode-table').setAttribute('aria-pressed',String(mode==='table'));$('alias-text').value='';$('alias-file').value='';$('alias-sheet-label').hidden=true;$('alias-text-label').firstChild.textContent=mode==='single'?'商品的不同叫法':'粘贴“叫法／对应类别”两列';$('alias-text').placeholder=mode==='single'?'皮箱\n旅行硬箱\n万向轮旅行箱':'叫法\t对应类别\n皮箱\t行李箱\n美容镜\t化妆镜';$('alias-input-hint').textContent=mode==='single'?'每行一个叫法，也可用逗号、顿号或分号分隔。一次最多 5000 个。':'每行一组，可从 Excel 复制两列，或填写带表头的 CSV。一次最多 5000 个叫法。';}
$('alias-mode-single').addEventListener('click',()=>setAliasMode('single'));
$('alias-mode-table').addEventListener('click',()=>setAliasMode('table'));
for(const id of ['alias-target','alias-text'])$(id).addEventListener('input',invalidateAliasPreview);
$('alias-file').addEventListener('change',()=>{invalidateAliasPreview();$('alias-sheet').replaceChildren();$('alias-sheet-label').hidden=true;});
$('alias-sheet').addEventListener('change',()=>{invalidateAliasPreview();$('alias-form').requestSubmit();});
$('alias-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;invalidateAliasPreview();const revision=aliasInputRevision;busy=true;$('alias-check').disabled=true;$('alias-check').textContent='正在检查…';
  try{
    const file=aliasMode==='table'&&$('alias-file').files[0];
    if(aliasMode==='single'&&!$('alias-target').value.trim())throw new Error('请填写对应标准类别');
    const result=file?await sendFile('/api/aliases/inspect',file,{sheet:$('alias-sheet').value||null}):await postJSON('/api/aliases/preview',{text:$('alias-text').value,category:aliasMode==='single'?$('alias-target').value.trim():''});
    if(revision!==aliasInputRevision)return;
    aliasPreview=result;
    if(result.sheets){$('alias-sheet').replaceChildren(...result.sheets.map(value=>{const option=document.createElement('option');option.value=value;option.textContent=value;return option;}));$('alias-sheet').value=result.sheet;$('alias-sheet-label').hidden=result.sheets.length<2;}
    $('alias-errors-only').checked=false;
  }catch(e){notify(e.message,true);}finally{busy=false;$('alias-check').disabled=false;$('alias-check').textContent='检查归类';renderAliasPreview();}
});
$('alias-errors-only').addEventListener('change',renderAliasPreview);
$('alias-save').addEventListener('click',()=>{if(aliasPreview?.can_save)aliasMutation('/api/aliases/save',{rows:aliasPreview.items.map(({alias,category})=>({alias,category})),revision:aliasPreview.revision},'归类已保存');});
$('alias-search').addEventListener('input',renderAliases);
$('alias-select-all').addEventListener('change',()=>{for(const row of aliasFiltered()){if($('alias-select-all').checked)aliasSelected.add(row.alias_key);else aliasSelected.delete(row.alias_key);}renderAliases();});
$('alias-delete-selected').addEventListener('click',()=>{if(aliasSelected.size&&confirm('删除所选 '+aliasSelected.size+' 条归类？'))aliasMutation('/api/aliases/delete',{keys:[...aliasSelected]},'所选归类已删除');});
$('alias-refresh').addEventListener('click',async()=>{if(busy)return;try{invalidateAliasPreview();await loadAliases();notify('归类库已刷新');}catch(e){notify(e.message,true);}});

// Filled workbooks stay local downloads; shared originals are read-only inputs.
let fillReport=null,fillMapping=null,fillDefaults={},fillDirty=false,fillSourceFile=null,fillReading=false,fillRevision=0,fillInspectSequence=0;
function resetFill(){clearFillCopy(true);fillRevision++;fillReport=null;fillMapping=null;fillDefaults={};fillDirty=false;$('fill-mapping-panel').hidden=true;$('fill-result').hidden=true;$('fill-dirty-note').textContent='';}
function updateFillActions(){
  updateFillCopyActions();
  $('fill-preview').disabled=busy||fillReading||!$('fill-template').value||!fillSourceFile||fillSourceFile!==$('fill-file').files[0];
  const canDownload=!!fillReport&&!fillDirty&&!(fillReport.template.prefilled&&!$('fill-replace').checked);
  $('fill-download-draft').disabled=busy||!canDownload;$('fill-download-strict').disabled=busy||!canDownload||fillReport?.status!=='ready';$('fill-recheck').disabled=busy||!fillReport;
}
function updateFillOptions(){
  const previous=$('fill-template').value,kind=$('fill-type').value,market=$('fill-market').value;
  const scope=templates.filter(t=>(t.table_type||'upload')===kind&&t.marketplace===market&&!t.account_scope_unknown),shops=[...new Set(scope.map(t=>t.shop||'通用模板'))].sort(),oldShop=$('fill-shop').value;
  $('fill-shop').replaceChildren(...shops.map(shop=>new Option(shop,shop)));$('fill-shop').value=shops.includes(oldShop)?oldShop:shops[0]||'';
  const choices=scope.filter(t=>(t.shop||'通用模板')===$('fill-shop').value);
  $('fill-template').replaceChildren(new Option(choices.length?'选择原始模板':'暂无对应模板，请先上传',''),...choices.map(t=>new Option(t.category+' · '+t.version+' · '+(t.download_name||t.original_name),t.id)));
  if(choices.some(t=>t.id===previous))$('fill-template').value=previous;else if(choices.length)$('fill-template').value=choices[0].id;
  if(previous!==$('fill-template').value)resetFill();
  $('fill-mode-note').textContent=kind==='price_update'?'改价／库存模式：按 SKU 填写明确提供的售价、库存和对应报价字段，使用部分更新操作。标题、品牌、描述等字段不参与填写。':'新商品模式：可匹配 SKU、商品类型、标题、品牌、描述、价格、库存、颜色、尺码、图片链接和原模板其他明确字段。缺少资料会列入待核对清单。';updateFillActions();
}
function openFillTemplate(template){
  if(!['AE','SA'].includes(template.marketplace)){notify('当前填写支持 AE 和 SA 模板',true);return;}
  $('fill-type').value=template.table_type||'upload';$('fill-market').value=template.marketplace;updateFillOptions();$('fill-shop').value=template.shop||'通用模板';updateFillOptions();$('fill-template').value=template.id;resetFill();updateFillActions();location.hash='fill';showPage('fill');window.scrollTo({top:0,behavior:'instant'});
}
for(const id of ['fill-type','fill-market','fill-shop'])$(id).addEventListener('change',()=>{resetFill();updateFillOptions();});
$('fill-template').addEventListener('change',()=>{resetFill();updateFillActions();});
for(const id of ['fill-sheet','fill-header'])$(id).addEventListener('change',()=>{resetFill();updateFillActions();});
$('fill-replace').addEventListener('change',()=>{if(fillReport){fillDirty=true;$('fill-dirty-note').textContent='数据替换选项已变化，请重新检查。';}updateFillActions();});
$('fill-file').addEventListener('change',async()=>{
  const sequence=++fillInspectSequence,file=$('fill-file').files[0];resetFill();fillSourceFile=null;$('fill-sheet').replaceChildren();$('fill-sheet').disabled=true;
  if(!file){$('fill-source-status').textContent='请选择产品资料表。';updateFillActions();return;}
  fillReading=true;$('fill-source-status').textContent='正在读取工作表和表头…';updateFillActions();
  try{const info=await sendFile('/api/fill/source',file);if(sequence!==fillInspectSequence)return;fillSourceFile=file;$('fill-sheet').replaceChildren(...info.sheets.map(name=>new Option(name,name)));$('fill-sheet').value=info.sheet;$('fill-sheet').disabled=false;$('fill-header').value=info.header_row;$('fill-source-status').textContent='已读取 '+file.name+' · '+info.sheets.length+' 个工作表 · 表头第 '+info.header_row+' 行，可调整后匹配字段。';}
  catch(error){if(sequence===fillInspectSequence){$('fill-source-status').textContent=error.message;notify(error.message,true);}}
  finally{if(sequence===fillInspectSequence){fillReading=false;updateFillActions();}}
});
$('fill-source-example').addEventListener('click',()=>{
  const pricing=$('fill-type').value==='price_update',headers=pricing?['SKU','售价','库存','币种','配送渠道']:['SKU','产品类型','标题','品牌','售价','库存','币种','配送渠道','颜色','尺码','主图链接','父子关系','父SKU','变体主题','卖点1','卖点2','卖点3','卖点4','卖点5'];
  saveDownloadBlob(new Blob(['\ufeff'+headers.join(',')+'\r\n'],{type:'text/csv;charset=utf-8'}),pricing?'改价库存资料空表.csv':'新商品资料空表.csv');
});
function fillPayload(){return {by_sku:fillBySku,copy_confirmed:Object.keys(fillBySku).length>0,copy_language:$('fill-copy-language').value,alias_revision:fillReport?.alias_revision||null,template_id:$('fill-template').value,shop:$('fill-shop').value,marketplace:$('fill-market').value,table_type:$('fill-type').value,sheet:$('fill-sheet').value,header_row:Number($('fill-header').value),mapping:fillMapping,defaults:fillDefaults,replace_existing:$('fill-replace').checked};}
function markFillDirty(){clearFillCopy(false);fillDirty=true;fillOnlyCopyDirty=false;$('fill-dirty-note').textContent='字段对应关系已调整，请点击“按当前对应关系重新检查”后导出。';updateFillActions();}
function renderFillMappings(){
  if(!fillReport)return;const query=$('fill-field-search').value.trim().toLowerCase(),only=$('fill-only-mapped').checked;
  const flagged=reportFieldCounts(),flaggedKeys=Object.keys(flagged),onlyFlagged=$('fill-only-flagged').checked&&flaggedKeys.length>0;
  $('fill-report-note').hidden=!flaggedKeys.length;$('fill-only-flagged-label').hidden=!flaggedKeys.length;
  if(flaggedKeys.length)$('fill-report-note').textContent='亚马逊处理报告在 '+flaggedKeys.length+' 个字段上标出 '+Object.values(flagged).reduce((a,b)=>a+b,0)+' 条问题，已在下方标红。改好来源列或统一值（或修改产品资料后重新读取），再点“按当前对应关系重新检查”，然后生成新表格。';
  const fields=fillReport.fields.filter(f=>(!query||(f.label+' '+f.key+' '+f.canonical).toLowerCase().includes(query))&&(!only||f.required||f.conditional||fillMapping[f.key]!=null||fillDefaults[f.key])&&(!onlyFlagged||flagged[f.key]));
  $('fill-mapping-count').textContent=fillReport.fields.filter(f=>fillMapping[f.key]!=null||fillDefaults[f.key]).length+' 个字段已指定来源／统一值 · 共 '+fillReport.fields.length+' 个可填字段'+(fields.length>250?'；显示筛选结果前 250 项，可搜索定位其他字段':'');
  $('fill-mapping-rows').replaceChildren(...fields.slice(0,250).map(field=>{
    const tr=document.createElement('tr'),label=cell(tr,''),source=cell(tr,''),constant=cell(tr,'');label.append(document.createTextNode(field.label));
    if(field.required||field.conditional){const badge=document.createElement('span');badge.className='fill-field-required';badge.textContent=field.required?'必填':'条件必填';label.append(badge);}
    if(flagged[field.key]){tr.classList.add('report-flagged');const badge=document.createElement('span');badge.className='fill-field-flagged';badge.textContent='处理报告 '+flagged[field.key]+' 条';label.append(badge);}
    const path=document.createElement('small');path.className='fill-field-path';path.textContent=field.column+' · '+field.key;label.append(path);
    const select=document.createElement('select');select.setAttribute('aria-label','来源列 '+field.label+' '+field.column);select.append(new Option('暂不指定',''),...fillReport.source.headers.map((header,i)=>new Option(String(i+1)+' · '+(header||'空表头'),String(i))));select.value=fillMapping[field.key]==null?'':String(fillMapping[field.key]);source.append(select);
    const input=document.createElement('input');input.type='text';input.value=fillDefaults[field.key]||'';input.maxLength=4000;input.placeholder='不使用统一值';input.setAttribute('aria-label','统一值 '+field.label+' '+field.column);constant.append(input);
    select.addEventListener('change',()=>{fillMapping[field.key]=select.value===''?null:Number(select.value);fillDefaults[field.key]='';input.value='';markFillDirty();});
    input.addEventListener('input',()=>{fillDefaults[field.key]=input.value;fillMapping[field.key]=null;select.value='';markFillDirty();});
    if(field.options.length){const values=document.createElement('small');values.className='fill-field-options';values.textContent=(field.options_enforced?'模板限定选项：':'模板提示选项（可填写实际值）：')+field.options.slice(0,8).join(' / ')+(field.options_count>8?' … 共 '+field.options_count+' 项':'');constant.append(values);}
    if($('fill-type').value==='price_update'&&field.canonical==='update_delete'){select.disabled=true;input.disabled=true;const note=document.createElement('small');note.className='fill-field-options';note.textContent='由改价模式选择模板的部分更新选项';constant.append(note);}
    return tr;
  }));
}
function renderFillReport(report){
  fillReport=report;fillMapping=Object.fromEntries(report.fields.map(f=>[f.key,f.source_index??null]));fillDefaults=Object.fromEntries(report.fields.filter(f=>f.default).map(f=>[f.key,f.default]));fillDirty=false;fillOnlyCopyDirty=false;$('fill-dirty-note').textContent='当前对应关系已检查。'+(report.reviewed_copy_skus?.length?' 已填写 '+report.reviewed_copy_skus.length+' 个 SKU 的已采用文案。':'');$('fill-download-draft').textContent=report.status==='ready'?'生成并下载填写结果':'生成并下载填写草稿';renderFillMappings();
  $('fill-mapping-panel').hidden=false;$('fill-result').hidden=false;$('fill-result').classList.toggle('has-errors',report.errors>0);$('fill-result-title').textContent=report.status==='ready'?'填写检查通过':'填写草稿 · 存在待核对项';
  $('fill-result-scope').textContent=report.target.shop+' · '+report.marketplace+' · '+TABLE_TYPES[report.table_type]+' · '+report.target.category;
  $('fill-result-counts').replaceChildren(...[['资料行',report.source_rows],['输出商品行',report.output_rows],['错误',report.errors],['待确认',report.warnings]].map(([label,count])=>{const box=document.createElement('div'),value=document.createElement('strong'),name=document.createElement('span');value.textContent=count;name.textContent=label;box.append(value,name);return box;}));
  $('fill-validation-note').textContent='将填写 '+report.written_cells+' 个单元格，合并 '+report.merged_rows+' 行内容一致或互补的重复 SKU。'+(report.status==='ready'?'可导出，并在上传前核对生成文件。':'请处理问题后重新检查；也可下载草稿继续核对。')+(report.template.prefilled&&!$('fill-replace').checked?' 原模板已有数据，请在上方确认替换副本的数据区。':'')+(report.source.warnings.length?' '+report.source.warnings.join('；'):'');
  $('fill-issues-summary').textContent='查看待核对问题（'+(report.errors+report.warnings)+'）';$('fill-issues-details').open=report.errors>0;
  $('fill-issues').replaceChildren(...report.issues.slice(0,200).map(issue=>{const tr=document.createElement('tr');tr.className='fill-issue-'+issue.level;for(const value of [issue.level==='error'?'错误':'待确认',issue.row||'本批',issue.sku,issue.field,issue.message])cell(tr,value);return tr;}));
  $('fill-issues-limit').textContent=report.issues.length>200?'页面显示前 200 项，检查报告包含更多问题。':'';
  if(report.issues_truncated)$('fill-issues-limit').textContent+=' 问题较多，报告最多列出 10000 项明细，总数量仍完整统计；建议分批补充。';
  const labels=Object.fromEntries(report.fields.map(f=>[f.key,f.label]));
  $('fill-preview-rows').replaceChildren(...report.preview_rows.map(row=>{const tr=document.createElement('tr');cell(tr,row.sku||'缺少 SKU');cell(tr,row.source_rows.join('、'));const values=cell(tr,'');values.className='fill-preview-cell';const entries=Object.entries(row.values).filter(([,value])=>value!=='');for(const [key,value] of entries.slice(0,12)){const line=document.createElement('p');line.textContent=(labels[key]||key)+'：'+value;values.append(line);}if(entries.length>12){const note=document.createElement('p');note.textContent='另有 '+(entries.length-12)+' 项，将写入生成文件。';values.append(note);}return tr;}));updateFillActions();
}
async function previewFill(){
  if(busy||fillReading)return;const file=$('fill-file').files[0];if(!file||file!==fillSourceFile||!$('fill-template').value){notify('请先选择模板并读取产品资料表',true);return;}
  const revision=fillRevision;busy=true;const controls=[...$('fill-panel').querySelectorAll('input,select,button')],states=controls.map(c=>c.disabled);controls.forEach(c=>c.disabled=true);$('fill-preview').textContent='正在匹配与检查…';
  try{const report=await sendFile('/api/fill/preview',file,fillPayload());if(revision===fillRevision&&file===$('fill-file').files[0]){renderFillReport(report);notify(report.errors?'检查完成，请核对错误和字段对应关系':'字段匹配完成，可核对后生成表格');}}
  catch(error){notify(error.message,true);}finally{busy=false;controls.forEach((control,i)=>{if(control.isConnected)control.disabled=states[i];});$('fill-preview').textContent='读取模板并匹配字段';updateFillActions();}
}
$('fill-form').addEventListener('submit',event=>{event.preventDefault();previewFill();});$('fill-recheck').addEventListener('click',previewFill);
$('fill-field-search').addEventListener('input',renderFillMappings);$('fill-only-mapped').addEventListener('change',renderFillMappings);$('fill-only-flagged').addEventListener('change',renderFillMappings);
async function downloadFill(strict){
  if(busy)return;if(!fillReport||fillDirty){notify('请先重新检查当前字段对应关系',true);return;}
  const revision=fillRevision,file=$('fill-file').files[0],payload=fillPayload();busy=true;const controls=[...$('fill-panel').querySelectorAll('input,select,button')],states=controls.map(c=>c.disabled);controls.forEach(c=>c.disabled=true);
  try{if(file!==fillSourceFile)throw new Error('产品资料已变化，请重新读取');const response=await fetch('/api/fill/export',fileRequest(file,{...payload,strict}));if(!response.ok){const error=await response.json();throw new Error(error.error||'生成失败');}const blob=await response.blob();if(revision!==fillRevision||file!==fillSourceFile)return;const match=(response.headers.get('Content-Disposition')||'').match(/filename\*=UTF-8''([^;]+)/i);saveDownloadBlob(blob,match?decodeURIComponent(match[1]):'填写结果.zip');notify('已生成并校验填写副本，下载包内包含 Excel 和检查报告');}
  catch(error){if(revision===fillRevision)notify(error.message,true);}finally{busy=false;controls.forEach((c,i)=>{if(c.isConnected)c.disabled=states[i];});updateFillActions();}
}
$('fill-download-draft').addEventListener('click',()=>downloadFill(false));$('fill-download-strict').addEventListener('click',()=>downloadFill(true));
$('fill-download-report').addEventListener('click',()=>{if(fillReport)saveDownloadBlob(new Blob([JSON.stringify(fillReport,null,2)],{type:'application/json;charset=utf-8'}),'填写检查报告.json');});
$('logout').addEventListener('click',()=>{fillInspectSequence++;resetFill();fillSourceFile=null;fillReading=false;$('fill-file').value='';$('fill-sheet').replaceChildren();$('fill-sheet').disabled=true;$('fill-replace').checked=false;$('fill-source-status').textContent='请选择产品资料表。';updateFillActions();});

// AI proposes text; only explicitly adopted proposals enter the deterministic filler.
let fillCopyData=null,fillBySku={},fillOnlyCopyDirty=false,fillCopySequence=0;
const COPY_LANGUAGE_LABELS={en:'英文',ar:'阿拉伯文',zh:'中文'};
function clearFillCopy(revoke=false){
  fillCopySequence++;fillCopyData=null;fillBySku={};fillOnlyCopyDirty=false;
  $('fill-copy-review').hidden=true;$('fill-copy-errors-details').hidden=true;$('fill-copy-progress').hidden=true;$('fill-copy-sku').replaceChildren();$('fill-copy-status').textContent='先读取并核对原表字段，再生成候选。';$('fill-copy-start').value='1';
  $('fill-copy-reviewed-one').checked=false;$('fill-copy-reviewed-all').checked=false;if(revoke)$('fill-copy-consent').checked=false;
}
function updateFillCopyActions(){
  const visible=!!fillReport&&$('fill-type').value==='upload';$('fill-copy-panel').hidden=!visible;
  const configured=!!window.currentAI?.configured,missing=fillReport?.row_sources?.some(row=>!row.sku),ready=visible&&(!fillDirty||fillOnlyCopyDirty)&&!missing;
  $('fill-copy-generate').disabled=busy||!ready||!configured||!$('fill-copy-consent').checked;
  $('fill-copy-generate').textContent=busy?'正在处理…':'生成'+COPY_LANGUAGE_LABELS[$('fill-copy-language').value]+'文案候选';
  $('fill-copy-provider').textContent=configured?'使用 '+(window.currentAI.providers?.[window.currentAI.provider]?.name||window.currentAI.provider)+' / '+window.currentAI.model+'；每批最多 100 个 SKU，默认复用缓存。':'AI 尚未配置，请管理员在“AI 设置”中配置服务商和模型。';
  if(missing)$('fill-copy-provider').textContent+=' 当前有 SKU 缺失，请先指定原表 SKU 来源列；不会自动编号。';
  const count=Object.keys(fillBySku).length;$('fill-copy-adopted').textContent='累计已采用 '+count+' 个 SKU';$('fill-copy-apply').disabled=busy||!count;$('fill-copy-clear').disabled=busy||(!fillCopyData&&!count);
  $('fill-copy-next').disabled=busy||!fillCopyData||fillCopyData.selection_start+fillCopyData.requested>fillCopyData.total_skus;
  $('fill-copy-adopt-one').disabled=busy||!fillCopyData?.items.length;$('fill-copy-adopt-all').disabled=busy||!fillCopyData?.items.length;
  const current=currentCopy(),fields=copyFieldMap();$('fill-copy-type').disabled=busy||!!current?.known_product_type||fields.feed_product_type?.length!==1;
  $('fill-copy-start').max=String(fillReport?.output_rows||1000);
}
function copyFieldMap(){
  const result={};for(const field of fillCopyData?.fields||[]){if(!result[field.canonical])result[field.canonical]=[];result[field.canonical].push(field);}return result;
}
function currentCopy(){return fillCopyData?.items.find(item=>item.sku===$('fill-copy-sku').value);}
function copyMetrics(){
  $('fill-copy-title-count').textContent=Array.from($('fill-copy-title').value).length+'/75';$('fill-copy-highlight-count').textContent=Array.from($('fill-copy-highlight').value).length+'/125';
  for(let i=1;i<=5;i++){const input=$('fill-copy-bullet-'+i),label=$('fill-copy-bullet-count-'+i);if(input&&label)label.textContent=Array.from(input.value).length+'/125';}
}
function editCopyText(kind,index,value){
  const item=currentCopy();if(!item)return;
  if(kind==='bullet'){while(item.bullets.length<=index)item.bullets.push({text:'',source_refs:[]});item.bullets[index]={text:value,source_refs:[]};}
  else item[kind]={text:value,source_refs:[]};
  item.edited=true;$('fill-copy-reviewed-one').checked=false;$('fill-copy-reviewed-all').checked=false;copyMetrics();
  $('fill-copy-status').textContent='当前是编辑后的候选内容，核对并点击采用后才会进入填写结果。';
}
function renderCopyEditor(){
  const item=currentCopy();if(!item)return;const fields=copyFieldMap();
  const codes=fillCopyData.supported_product_types;const type=$('fill-copy-type');type.replaceChildren(new Option('未确定，沿用原表或保持待确认',''),...codes.map(t=>new Option(t.label+' · '+t.code,t.code)));
  if(item.known_product_type&&!codes.some(t=>t.code===item.known_product_type))type.append(new Option('沿用原表：'+item.known_product_type,item.known_product_type));
  type.value=item.known_product_type||item.product_type.value||'';type.disabled=!!item.known_product_type||fields.feed_product_type?.length!==1;
  $('fill-copy-title').value=item.title.text;$('fill-copy-title').readOnly=fields.item_name?.length!==1;
  $('fill-copy-highlight').value=item.highlight.text;$('fill-copy-highlight').readOnly=fields.item_highlight?.length!==1;
  $('fill-copy-bullets').replaceChildren(...Array.from({length:5},(_,index)=>{
    const label=document.createElement('label');label.className='fill-copy-text-label';label.append(document.createTextNode('五点描述 '+(index+1)));const count=document.createElement('small');count.id='fill-copy-bullet-count-'+(index+1);label.append(count);
    const input=document.createElement('textarea');input.id='fill-copy-bullet-'+(index+1);input.rows=2;input.maxLength=125;input.value=item.bullets[index]?.text||'';input.placeholder='资料不足时保持空白';input.readOnly=fields['bullet_point_'+(index+1)]?.length!==1;
    input.addEventListener('input',()=>editCopyText('bullet',index,input.value));label.append(input);return label;
  }));
  const unavailable=[];for(const [code,name] of [['item_name','标题'],['item_highlight','短卖点']])if(fields[code]?.length!==1)unavailable.push(name);
  const bulletSlots=Array.from({length:5},(_,i)=>fields['bullet_point_'+(i+1)]?.length===1).filter(Boolean).length;if(bulletSlots<5)unavailable.push('五点仅有 '+bulletSlots+' 个对应字段');
  $('fill-copy-field-note').textContent=(item.known_product_type?'已有商品类型沿用原表与模板的明确匹配。':'类目建议置信度：'+({high:'高',medium:'中',low:'低'}[item.product_type.confidence]||'待确认')+'。')+(unavailable.length?' 当前模板缺少唯一对应字段：'+unavailable.join('、')+'。只读候选可复制查看，采用时不会写入这些字段。':' 标题不超过 75 字符，短卖点和每条五点不超过 125 字符。');
  $('fill-copy-missing').textContent=(item.missing_information||[]).join(' ');
  $('fill-copy-facts').replaceChildren(...item.facts.map(fact=>{const p=document.createElement('p');p.textContent=fact.ref+' · '+fact.header+'：'+fact.value;return p;}));
  $('fill-copy-reviewed-one').checked=false;copyMetrics();updateFillCopyActions();
}
$('fill-copy-sku').addEventListener('change',renderCopyEditor);
$('fill-copy-title').addEventListener('input',()=>editCopyText('title',0,$('fill-copy-title').value));
$('fill-copy-highlight').addEventListener('input',()=>editCopyText('highlight',0,$('fill-copy-highlight').value));
$('fill-copy-type').addEventListener('change',()=>{const item=currentCopy();if(item){item.product_type={value:$('fill-copy-type').value||null,confidence:'low',source_refs:[]};item.edited=true;$('fill-copy-reviewed-one').checked=false;$('fill-copy-reviewed-all').checked=false;}});
$('fill-copy-consent').addEventListener('change',updateFillCopyActions);
$('fill-copy-language').addEventListener('change',()=>{const had=Object.keys(fillBySku).length>0;clearFillCopy(false);if(had){fillDirty=true;fillOnlyCopyDirty=true;$('fill-dirty-note').textContent='文案语言已变化，请重新生成／采用并检查；上次生成内容已移除。';}updateFillActions();});
function copyOverrides(item){
  const fields=copyFieldMap(),values={};
  function assign(code,value,maximum){if(fields[code]?.length!==1||!value?.trim())return;if(Array.from(value.trim()).length>maximum)throw new Error('SKU '+item.sku+' 的'+code+' 超过字符限制');values[fields[code][0].key]=value.trim();}
  assign('item_name',item.title.text,75);assign('item_highlight',item.highlight.text,125);item.bullets.slice(0,5).forEach((v,i)=>assign('bullet_point_'+(i+1),v.text,125));
  const type=item.known_product_type||item.product_type.value;if(type){if(fillCopyData.supported_product_types.some(t=>t.code===type))assign('feed_product_type',type,100);else if(!item.known_product_type)throw new Error('商品类型不在当前模板范围内');}
  if(!Object.keys(values).length)throw new Error('SKU '+item.sku+' 没有可写入当前模板的候选内容');return values;
}
function adoptedCopyMessage(){
  fillOnlyCopyDirty=!fillDirty||fillOnlyCopyDirty;fillDirty=true;$('fill-dirty-note').textContent='已累计采用 '+Object.keys(fillBySku).length+' 个 SKU 文案，请点击“将已采用文案填入并重新检查”后导出。';updateFillActions();
}
$('fill-copy-adopt-one').addEventListener('click',()=>{
  if(busy)return;if(!$('fill-copy-reviewed-one').checked){notify('请先核对当前 SKU 并勾选确认',true);return;}
  const item=currentCopy();if(!item)return;
  try{fillBySku[item.sku]=copyOverrides(item);adoptedCopyMessage();$('fill-copy-reviewed-one').checked=false;const index=fillCopyData.items.indexOf(item);if(index<fillCopyData.items.length-1){$('fill-copy-sku').value=fillCopyData.items[index+1].sku;renderCopyEditor();}notify('已采用 '+item.sku+' 的候选文案；重新检查后可导出');}catch(error){notify(error.message,true);}
});
$('fill-copy-adopt-all').addEventListener('click',()=>{
  if(busy)return;if(!$('fill-copy-reviewed-all').checked){notify('请先核对本批全部文案并勾选确认',true);return;}
  try{const proposed=Object.fromEntries(fillCopyData.items.map(item=>[item.sku,copyOverrides(item)]));Object.assign(fillBySku,proposed);adoptedCopyMessage();$('fill-copy-reviewed-all').checked=false;notify('已采用本批 '+Object.keys(proposed).length+' 个 SKU 文案，请重新检查后导出');}catch(error){notify(error.message,true);}
});
$('fill-copy-apply').addEventListener('click',()=>previewFill());
$('fill-copy-clear').addEventListener('click',()=>{if(busy)return;const had=Object.keys(fillBySku).length>0;clearFillCopy(false);if(had){fillOnlyCopyDirty=!fillDirty||fillOnlyCopyDirty;fillDirty=true;$('fill-dirty-note').textContent='已清除生成内容，请重新检查以恢复按原表填写。';}updateFillActions();});
$('fill-copy-next').addEventListener('click',()=>{if(fillCopyData){$('fill-copy-start').value=String(fillCopyData.selection_start+fillCopyData.requested);$('fill-copy-status').textContent='已切换到下一批起始序号，点击生成候选继续。';}});
function startCopyProgress(job,revision,sequence){
  let active=true,timer;const progress=$('fill-copy-progress');progress.hidden=false;progress.value=0;progress.max=1;
  async function poll(){
    if(!active||revision!==fillRevision||sequence!==fillCopySequence)return;
    try{const state=await request('/api/inventory/progress?job_id='+encodeURIComponent(job));if(!active||revision!==fillRevision||sequence!==fillCopySequence)return;if(state.total){progress.max=state.total;progress.value=state.completed||0;$('fill-copy-status').textContent='正在生成候选 '+(state.completed||0)+'/'+state.total+' · 复用 '+(state.cached||0)+' · 调用 '+(state.calls||0)+' 批';}}
    catch(_error){}if(active)timer=setTimeout(poll,1000);
  }
  poll();return ()=>{active=false;clearTimeout(timer);};
}
$('fill-copy-generate').addEventListener('click',async()=>{
  if(busy)return;if(!fillReport||(fillDirty&&!fillOnlyCopyDirty)){notify('请先核对并重新检查原表字段',true);return;}if(!$('fill-copy-consent').checked){notify('请先同意发送所选商品文字到 AI 服务商',true);return;}
  const file=$('fill-file').files[0];if(!file||file!==fillSourceFile)return;const revision=fillRevision,sequence=++fillCopySequence,job='copy-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  const payload={...fillPayload(),by_sku:{},copy_confirmed:false,copy_consent:true,copy_language:$('fill-copy-language').value,copy_start:Number($('fill-copy-start').value),copy_limit:Number($('fill-copy-limit').value),copy_cache:$('fill-copy-cache').checked,job_id:job};
  busy=true;const controls=[...$('fill-panel').querySelectorAll('input,select,textarea,button')],states=controls.map(c=>c.disabled);controls.forEach(c=>c.disabled=true);$('fill-copy-generate').textContent='正在生成文案…';const stop=startCopyProgress(job,revision,sequence);
  try{const result=await sendFile('/api/fill/generate',file,payload);if(revision!==fillRevision||sequence!==fillCopySequence||file!==fillSourceFile)return;
    fillCopyData=result;$('fill-copy-status').textContent='本批返回 '+result.generated+'/'+result.requested+' 个 SKU 候选 · 复用 '+result.cached+' · 实际调用 '+result.calls+' 批 · 资料共 '+result.total_skus+' 个 SKU。候选尚未自动写入，请核对后采用。';
    $('fill-copy-progress').max=result.requested||1;$('fill-copy-progress').value=result.generated;
    $('fill-copy-sku').replaceChildren(...result.items.map(item=>new Option(item.sku,item.sku)));$('fill-copy-review').hidden=!result.items.length;
    $('fill-copy-errors-details').hidden=!result.errors.length;$('fill-copy-errors-details').open=!!result.errors.length;$('fill-copy-errors').replaceChildren(...result.errors.map(error=>{const p=document.createElement('p');p.textContent=error.sku+'：'+error.message;return p;}));
    $('fill-copy-reviewed-all').checked=false;if(result.items.length)renderCopyEditor();notify(result.generated?'候选文案已生成，核对采用后才能填入模板':'本批未生成可用文案，请查看未完成原因',!result.generated);
  }catch(error){if(revision===fillRevision&&sequence===fillCopySequence){$('fill-copy-status').textContent=error.message;notify(error.message,true);}}
  finally{stop();busy=false;controls.forEach((c,i)=>{if(c.isConnected)c.disabled=states[i];});updateFillActions();}
});

// Processing reports: Amazon's verdict on an uploaded sheet, explained per SKU and field.
// Read-only; when the fill page holds a draft for the same template, rows and source columns are shown too.
const REPORT_LEVELS={error:'错误',warning:'警告',suggestion:'建议',unknown:'待确认'};
const REPORT_PROMPT='点击选择或拖入处理报告';
let reportData=null,reportLevel='',reportKind=null,reportFile=null,reportBusy=false,reportTemplateChosen=false;
function updateReportTemplates(){
  // Follow the template open in "填写表格" until the user picks one here.
  const select=$('report-template');if(!select)return;const previous=select.value;
  select.replaceChildren(new Option('不指定，按报告本身识别字段',''),...templates.map(t=>new Option([t.shop||'通用模板',t.marketplace,TABLE_TYPES[t.table_type||'upload'],t.category,t.version].join(' · '),t.id)));
  const preferred=reportTemplateChosen?previous:(fillReport?.target?.id||$('fill-template').value||previous);
  select.value=templates.some(t=>t.id===preferred)?preferred:'';
}
$('report-template').addEventListener('change',()=>{reportTemplateChosen=true;});
function reportDraft(){
  if(!fillReport||!reportData)return null;
  const id=reportData.template?.id;if(id&&id!==fillReport.target?.id)return null;
  const known=new Set(fillReport.fields.map(f=>f.key));
  if(!id&&!reportData.issues.some(issue=>known.has(issue.field_key)))return null;
  const rows={};for(const item of fillReport.row_sources||[])rows[item.sku]=item.source_rows;
  return {rows,known,headers:fillReport.source.headers,file:fillReport.source.filename};
}
function draftSource(draft,key){
  if(!draft||!key||!draft.known.has(key))return '';
  const index=fillMapping?.[key];
  if(index!=null)return '来源列：'+(draft.headers[index]||'第 '+(index+1)+' 列');
  if(fillDefaults?.[key])return '统一值：'+fillDefaults[key];
  return '未指定来源';
}
function reportFieldCounts(){
  const counts={};if(!reportDraft())return counts;
  for(const issue of reportData.issues)if(issue.field_key&&issue.severity!=='suggestion')counts[issue.field_key]=(counts[issue.field_key]||0)+1;
  return counts;
}
function reportChip(level){const chip=document.createElement('span');chip.className='report-level level-'+level;chip.textContent=REPORT_LEVELS[level];return chip;}
function renderReport(){
  const data=reportData;if(!data)return;$('report-result').hidden=false;
  const counts=[['错误',data.counts.error,'error'],['警告',data.counts.warning,'warning'],['建议',data.counts.suggestion,'suggestion']];
  if(data.counts.unknown)counts.push(['待确认',data.counts.unknown,'unknown']);
  counts.push(['涉及 SKU',data.skus,'']);
  $('report-counts').replaceChildren(...counts.map(([label,value,level])=>{const box=document.createElement('div'),strong=document.createElement('strong'),span=document.createElement('span');box.className='report-count'+(level?' level-'+level:'');strong.textContent=value;span.textContent=label;box.append(strong,span);return box;}));
  const meta=[data.filename,data.format==='excel'?'Excel 处理报告':'文本处理报告'];
  if(data.summary.processed!=null)meta.push('亚马逊处理 '+data.summary.processed+' 条'+(data.summary.successful!=null?'，成功 '+data.summary.successful+' 条':''));
  if(data.template)meta.push('按模板「'+data.template.category+' · '+data.template.version+'」识别字段');
  meta.push(data.matched_fields+' 条已对上具体字段');
  $('report-meta').textContent=meta.join(' · ')+(data.notes?.length?'。'+data.notes.join('；'):'')+(data.truncated?'。问题过多，只读取了前 20000 条。':'');
  const draft=reportDraft();
  const skuFound=draft&&data.issues.some(issue=>draft.rows[issue.sku]);
  $('report-link-text').textContent=draft?(skuFound?'已对照“填写表格”里的当前草稿（'+draft.file+'）：下方显示每个 SKU 在资料表里的行号和对应的来源列；出错的字段在填写页已标红，改好后重新检查并生成。':'“填写表格”里的当前草稿（'+draft.file+'）用的是同一份模板，但里面没有报告中的 SKU，可能不是这次提交的那批资料。字段的来源列仍可参考，资料表行号无法对照。'):fillReport?'“填写表格”里当前的草稿用的是另一份模板，暂不对照资料表行号。':'想看到每个 SKU 在资料表里的行号：先在“填写表格”读取提交时用的那份产品资料并检查，再回到这里。';
  $('report-link').classList.toggle('linked',!!draft);$('report-open-fill').hidden=currentRole==='viewer';
  renderReportGroups();renderReportRows();
}
function renderReportGroups(){
  if(!reportData.groups.length){const empty=document.createElement('p');empty.className='hint';empty.textContent='报告里没有错误或警告。';$('report-groups').replaceChildren(empty);return;}
  $('report-groups').replaceChildren(...reportData.groups.map(group=>{
    const key=group.kind+'|'+group.severity,button=document.createElement('button');button.type='button';button.className='report-group level-'+group.severity;button.setAttribute('aria-pressed',String(reportKind===key));
    const head=document.createElement('span'),title=document.createElement('b'),count=document.createElement('span'),hint=document.createElement('span');
    head.className='report-group-head';title.textContent=group.title;count.className='report-group-count';count.textContent=group.count+' 条 · '+group.skus.length+(group.skus.length>=200?'+':'')+' 个 SKU';head.append(reportChip(group.severity),title,count);
    hint.className='report-group-hint';hint.textContent=group.hint;button.append(head,hint);
    if(group.fields.length){const fields=document.createElement('span');fields.className='report-group-fields';fields.textContent='涉及字段：'+group.fields.join('、');button.append(fields);}
    button.addEventListener('click',()=>{reportKind=reportKind===key?null:key;renderReportGroups();renderReportRows();});
    return button;
  }));
}
function reportFiltered(){
  const query=$('report-search').value.trim().toLowerCase();
  return reportData.issues.filter(i=>(!reportLevel||i.severity===reportLevel)&&(!reportKind||i.kind+'|'+i.severity===reportKind)&&(!query||[i.sku,i.field_label,i.field_key,i.column,i.code,i.message,i.title].join(' ').toLowerCase().includes(query)));
}
function renderReportRows(){
  const rows=reportFiltered(),draft=reportDraft(),shown=rows.slice(0,300);
  $('report-filter-note').textContent=rows.length===reportData.issues.length?'共 '+rows.length+' 条':'筛选出 '+rows.length+' 条 / 共 '+reportData.issues.length+' 条';
  $('report-rows').replaceChildren(...shown.map(issue=>{
    const tr=document.createElement('tr');tr.className='report-row level-'+issue.severity;
    cell(tr,'').append(reportChip(issue.severity));
    const sku=cell(tr,''),name=document.createElement('b');name.textContent=issue.sku||'未标明 SKU';sku.append(name);
    const where=[];if(draft&&draft.rows[issue.sku])where.push('资料第 '+draft.rows[issue.sku].join('、')+' 行');if(issue.row)where.push('报告第 '+issue.row+' 行');else if(issue.record)where.push('第 '+issue.record+' 条记录');
    if(where.length){const small=document.createElement('small');small.textContent=where.join(' · ');sku.append(small);}
    const field=cell(tr,''),label=document.createElement('b');label.textContent=issue.field_label||'未标明字段';field.append(label);
    const detail=[issue.column?issue.column+' 列':'',draftSource(draft,issue.field_key)].filter(Boolean);
    if(detail.length){const small=document.createElement('small');small.textContent=detail.join(' · ');field.append(small);}
    const text=cell(tr,''),title=document.createElement('b'),hint=document.createElement('p'),original=document.createElement('small');
    title.textContent=issue.title;hint.textContent=issue.hint;original.className='report-original';original.textContent='亚马逊原文'+(issue.code?' '+issue.code:'')+'：'+issue.message;text.append(title,hint,original);
    return tr;
  }));
  $('report-limit').textContent=rows.length>shown.length?'页面显示前 300 条，导出的清单包含筛选出的全部 '+rows.length+' 条。':'';
}
$('report-file').addEventListener('change',()=>{reportFile=$('report-file').files[0]||null;$('report-file-name').textContent=reportFile?reportFile.name+' · '+fileSize(reportFile.size):REPORT_PROMPT;$('report-submit').disabled=!reportFile||reportBusy;});
$('report-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!reportFile||reportBusy)return;reportBusy=true;const button=$('report-submit');button.disabled=true;button.textContent='正在解析…';
  try{
    const result=await sendFile('/api/processing-report',reportFile,{template_id:$('report-template').value||null});
    reportData=result;reportLevel='';reportKind=null;$('report-search').value='';
    for(const other of document.querySelectorAll('#report-levels [data-level]'))other.setAttribute('aria-pressed',String(other.dataset.level===''));
    renderReport();if(fillReport)renderFillMappings();
    notify(result.issues.length?'已解析 '+result.issues.length+' 条问题，按类型和 SKU 列在下方':'报告里没有发现错误或警告');
  }catch(e){notify(e.message,true);}
  finally{reportBusy=false;button.disabled=!reportFile;button.textContent='解析报告';}
});
for(const button of document.querySelectorAll('#report-levels [data-level]'))button.addEventListener('click',()=>{reportLevel=button.dataset.level;for(const other of document.querySelectorAll('#report-levels [data-level]'))other.setAttribute('aria-pressed',String(other===button));if(reportData)renderReportRows();});
$('report-search').addEventListener('input',()=>{if(reportData)renderReportRows();});
$('report-open-fill').addEventListener('click',()=>{
  const id=reportData?.template?.id||$('report-template').value,template=templates.find(t=>t.id===id);
  if(template&&!reportDraft()&&$('fill-template').value!==id&&['AE','SA'].includes(template.marketplace)){openFillTemplate(template);return;}
  location.hash='fill';
});
$('report-export').addEventListener('click',()=>{
  if(!reportData)return;const draft=reportDraft();
  // A leading = + - @ would make spreadsheet apps run the cell as a formula.
  const csvCell=value=>{let text=String(value??'');if(/^[=+\-@\t\r]/.test(text))text="'"+text;return /[",\r\n]/.test(text)?'"'+text.replace(/"/g,'""')+'"':text;};
  const lines=[['级别','SKU','资料表行','字段','模板列','来源','错误代码','问题','处理建议','亚马逊原文'],...reportFiltered().map(i=>[REPORT_LEVELS[i.severity],i.sku,draft&&draft.rows[i.sku]?draft.rows[i.sku].join('、'):'',i.field_label,i.column,draftSource(draft,i.field_key),i.code,i.title,i.hint,i.message])];
  saveDownloadBlob(new Blob(['﻿'+lines.map(row=>row.map(csvCell).join(',')).join('\r\n')+'\r\n'],{type:'text/csv;charset=utf-8'}),'处理报告问题清单.csv');
});
$('logout').addEventListener('click',()=>{reportData=null;reportFile=null;reportKind=null;reportLevel='';reportTemplateChosen=false;$('report-file').value='';$('report-file-name').textContent=REPORT_PROMPT;$('report-submit').disabled=true;$('report-result').hidden=true;});
