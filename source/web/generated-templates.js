// Official product type rules -> our fill workbook -> the existing preview/submit gates.
let genEpoch=0,genBusy=false,genReady=null,genResult=null,genChosen='',genArtifact=null,genExpires=0,genExpiryTimer=null;
function genClearArtifact(){
  clearTimeout(genExpiryTimer);genArtifact=null;genExpires=0;
  $('gen-preview').hidden=true;$('gen-fields').replaceChildren();$('gen-save-status').textContent='';
}
function genInvalidate(){
  ++genEpoch;genClearArtifact();genResult=null;genChosen='';
  $('gen-types-panel').hidden=true;$('gen-types').replaceChildren();$('gen-status').textContent='';genControls();
}
function genReset(){
  genBusy=false;genReady=null;genInvalidate();
  $('gen-query').value='';$('gen-shop').value='';$('gen-category').value='';$('gen-version').value='';$('gen-field-search').value='';
  $('gen-connection').hidden=true;
}
function genControls(){
  const expired=!!genArtifact&&Date.now()>=genExpires;
  for(const id of ['gen-market','gen-mode','gen-language','gen-query'])$(id).disabled=genBusy;
  $('gen-search').disabled=genBusy||genReady===false;
  $('gen-generate').disabled=genBusy||!genChosen||genReady===false;
  for(const radio of document.querySelectorAll('#gen-types input'))radio.disabled=genBusy;
  for(const id of ['gen-download-template','gen-download-source','gen-download-schema','gen-save-fill'])$(id).disabled=genBusy||!genArtifact||expired;
  for(const id of ['gen-shop','gen-category','gen-version'])$(id).disabled=genBusy;
  if(genArtifact)$('gen-expiry').textContent=expired?'已过期，请重新生成':'本次产物 10 分钟内有效';
}
async function genRun(button,label,work){
  if(genBusy||button.disabled)return;
  const epoch=genEpoch,text=button.textContent;genBusy=true;button.textContent=label;genControls();
  try{await work(epoch);}catch(error){if(epoch===genEpoch){$('gen-status').textContent=error.message;notify(error.message,true);}}
  finally{if(epoch===genEpoch){genBusy=false;button.textContent=text;genControls();}}
}
async function enterGeneratedTemplates(){
  const epoch=genEpoch;genControls();
  try{
    const status=await request('/api/amazon/status');if(epoch!==genEpoch)return;
    genReady=!!status.configured;$('gen-connection').hidden=genReady;
    $('gen-connection-text').textContent=currentRole==='admin'?'先在设置中连接亚马逊，才能读取此卖家的商品类型规则。':'请联系管理员配置亚马逊连接，然后重新进入本页。';
    $('gen-connection-link').hidden=currentRole!=='admin';genControls();
  }catch(error){if(epoch===genEpoch){$('gen-status').textContent=error.message;genReady=false;genControls();}}
}
function genRenderTypes(){
  const items=genResult?.items||[];$('gen-types-panel').hidden=!items.length;
  $('gen-type-count').textContent=items.length+' 个结果'+(genResult?.truncated?'，结果较多，请细化关键词':'');
  $('gen-types').replaceChildren(...items.map(item=>{
    const row=el('tr'),choice=cell(row,''),radio=el('input');radio.type='radio';radio.name='generated-product-type';radio.value=item.name;radio.checked=genChosen===item.name;
    radio.setAttribute('aria-label','选择 '+item.display_name+'（'+item.name+'）');
    radio.addEventListener('change',()=>{genChosen=item.name;genClearArtifact();genControls();});choice.append(radio);
    const label=el('label','','');const title=el('span','',item.display_name||item.name);label.append(title);
    label.addEventListener('click',()=>{if(!genBusy){radio.checked=true;radio.dispatchEvent(new Event('change',{bubbles:true}));}});
    cell(row,'').append(label);cell(row,item.name,'gen-type-code');return row;
  }));genControls();
}
function genRenderFields(){
  const fields=genArtifact?.fields||[],query=$('gen-field-search').value.trim().toLocaleLowerCase();
  const matched=fields.filter(f=>!query||[f.key,f.label,f.description].join(' ').toLocaleLowerCase().includes(query));
  $('gen-fields').replaceChildren(...matched.slice(0,200).map(field=>{
    const row=el('tr'),name=cell(row,''),rules=cell(row,''),help=cell(row,'');
    name.append(el('b','',field.label||field.key),el('small','gen-field-key',field.key));
    rules.append(el('span','tag',field.required?'必填':field.conditional?'条件必填':'按需填写'),el('small','',field.value_type||'文本'));
    help.append(el('p','',field.description||''));
    if(field.options?.length)help.append(el('small','', '可选值：'+field.options.slice(0,15).join('、')+(field.options.length>15?' …（完整列表见表格）':'')));
    if(String(field.key).endsWith('::json'))help.append(el('p','gen-json-note','高级 JSON 属性：必须提供完整且有效的 JSON；未知事实不能补造。'));
    return row;
  }));
  $('gen-field-note').textContent='显示 '+Math.min(matched.length,200)+' / '+matched.length+' 个匹配字段，共 '+fields.length+' 列。'+(matched.length>200?'可继续搜索定位其他字段。':'');
}
function genShowArtifact(result){
  genArtifact=result;genExpires=Date.now()+Math.max(0,Number(result.expires_in)||0)*1000;
  $('gen-preview').hidden=false;$('gen-title').textContent=(result.display_name||result.product_type)+' · '+result.marketplace;
  $('gen-meta').textContent='类型 '+result.product_type+' · 规则版本 '+(result.version?.number||'未提供')+' · 获取于 '+localTime(result.generated_at);
  $('gen-summary').textContent='已生成 '+result.field_count+' 列，其中 '+result.required_count+' 列必填、'+result.conditional_count+' 列条件必填，'+result.json_count+' 列使用高级 JSON。商品资料区为空，不会自动生成 SKU 或商品事实。';
  $('gen-warnings').replaceChildren(...(result.warnings||[]).map(warning=>el('li','',warning)));
  $('gen-warnings').hidden=!(result.warnings||[]).length;$('gen-category').value='';$('gen-version').value='';$('gen-field-search').value='';
  $('gen-save-status').textContent='建议先下载配套商品资料空表。保存后会直接进入填写页，无需重新上传模板。';
  genRenderFields();genControls();clearTimeout(genExpiryTimer);
  const epoch=genEpoch;genExpiryTimer=setTimeout(()=>{if(epoch===genEpoch)genControls();},Math.max(0,genExpires-Date.now())+50);
}
$('gen-search-form').addEventListener('submit',event=>{
  event.preventDefault();genRun($('gen-search'),'正在查找…',async epoch=>{
    const query=$('gen-query').value.trim();if(!query)throw new Error('请输入商品名称或类型关键词');
    const result=await postJSON('/api/generated-templates/search',{marketplace:$('gen-market').value,query,mode:$('gen-mode').value,search_locale:$('gen-language').value});
    if(epoch!==genEpoch)return;genClearArtifact();genChosen='';genResult=result;genRenderTypes();
    $('gen-status').textContent=result.items?.length?'请人工核对并选择符合实际商品的类型。':'官方接口未返回匹配类型。可调整名称、改用英文，或切换查找方式；不要任意选择不匹配的类型。';
  });
});
$('gen-generate').addEventListener('click',()=>genRun($('gen-generate'),'正在拉取最新规则并生成…',async epoch=>{
  const result=await postJSON('/api/generated-templates/generate',{marketplace:$('gen-market').value,product_type:genChosen});
  if(epoch!==genEpoch)return;genShowArtifact(result);$('gen-status').textContent='填写表已生成，尚未写入模板库，也没有修改店铺。';
}));
for(const id of ['gen-market','gen-mode','gen-language'])$(id).addEventListener('change',genInvalidate);
$('gen-query').addEventListener('input',()=>{if(genResult||genArtifact)genInvalidate();});
$('gen-field-search').addEventListener('input',genRenderFields);
async function genDownload(kind){
  const button=$('gen-download-'+kind);
  await genRun(button,'正在下载…',async epoch=>{
    const artifact=genArtifact;
    const response=await fetch('/api/generated-templates/artifact',{method:'POST',headers:{'Content-Type':'application/json','X-Template-Request':'1'},body:JSON.stringify({token:artifact.token,kind,store:currentStore})});
    if(response.status===401){$('workspace').hidden=true;$('login-screen').hidden=false;throw new Error('请先登录');}
    if(!response.ok){const data=await response.json();throw new Error(data.error||'文件下载失败');}
    const blob=await response.blob();if(epoch!==genEpoch)return;
    let filename=(kind==='schema'?'规则快照-':kind==='source'?'配套商品资料空表-':'本工具生成填写表-')+artifact.marketplace+'-'+artifact.product_type+(kind==='schema'?'.json':'.xlsx');
    const disposition=response.headers.get('Content-Disposition')||'',match=disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if(match){try{const candidate=decodeURIComponent(match[1]);if(candidate&&!/[\/\\\x00-\x1f]/.test(candidate))filename=candidate;}catch(error){/* Keep safe fallback name. */}}
    saveDownloadBlob(blob,filename);
  });
}
for(const kind of ['template','source','schema'])$('gen-download-'+kind).addEventListener('click',()=>genDownload(kind));
$('gen-save-fill').addEventListener('click',()=>genRun($('gen-save-fill'),'正在保存…',async epoch=>{
  const shop=$('gen-shop').value.trim();if(!shop||shop==='通用模板')throw new Error('请填写与当前 SP-API 卖家对应的真实店铺名称，不能保存为通用模板');
  const body={token:genArtifact.token,shop};if($('gen-category').value.trim())body.category=$('gen-category').value.trim();if($('gen-version').value.trim())body.version=$('gen-version').value.trim();
  const record=await postJSON('/api/generated-templates/save',body);if(epoch!==genEpoch)return;
  $('gen-save-status').textContent='已保存到模板库，正在打开填写页。';
  try{await load();}catch(error){$('gen-save-status').textContent='模板已保存，但刷新模板列表失败。请在模板库刷新后打开；无需重复生成。';notify(error.message,true);return;}
  if(epoch!==genEpoch)return;
  const template=templates.find(item=>item.id===record.id);if(!template)throw new Error('模板已保存，但未在列表中找到，请刷新模板库后查看');
  openFillTemplate(template);notify('生成表已保存并选中。上传你填好的商品资料，核对字段后继续预检。');
}));
$('logout').addEventListener('click',genReset);
new MutationObserver(()=>{if($('workspace').hidden)genReset();}).observe($('workspace'),{attributes:true,attributeFilter:['hidden']});
window.addEventListener('focus',genControls);
if(!$('workspace').hidden&&$('workspace').dataset.page==='generate')enterGeneratedTemplates();
