// Stores of other legal entities: each authorizes the public application through a consent link.
// The browser never sees the application's secret or any store's token.
(function(){
  const DEFAULT_RETURN='https://edgerqzh.github.io/amazon-king/authorize.html';
  let link=null,busy=false;
  const $id=id=>document.getElementById(id);

  function render(){
    const s=window.amazonStatusForOAuth?.(),admin=window.currentRoleForOAuth?.()==='admin',box=$id('amazon-oauth');
    box.hidden=!admin||!s?.vault;if(box.hidden)return;
    const app=s.public_app||{configured:false,missing:[]},stores=s.stores||[],full=stores.length>=(s.limit||25);
    $id('oauth-app-state').textContent=app.configured?'公共应用已设置（'+app.application_id+'）。':'还没有设置公共应用，缺少：'+app.missing.join('、')+'。';
    $id('oauth-app').open=!app.configured&&!$id('oauth-app').dataset.touched;
    if(!$id('oauth-redirect').value)$id('oauth-redirect').value=app.redirect_uri||DEFAULT_RETURN;
    $id('oauth-start').disabled=busy||!app.configured;
    $id('oauth-finish').disabled=busy||!link;
    $id('oauth-store').replaceChildren(new Option('新店铺（填下面的名称）',''),...stores.filter(x=>x.store!=='default').map(x=>new Option('重新授权：'+x.name,x.store)));
    if(link&&link.store!==undefined)$id('oauth-store').value=link.store;
    $id('oauth-name').disabled=!!$id('oauth-store').value;
    $id('oauth-full').hidden=!full;
    $id('oauth-link-box').hidden=!link;
    if(link){$id('oauth-link').value=link.url;$id('oauth-link-note').textContent='这条链接 '+link.valid_minutes+' 分钟内有效，用于“'+link.name+'”。在登录了那个店铺卖家后台的浏览器里打开，确认授权后会跳到授权结果页。';}
  }
  window.renderOAuth=render;

  async function call(url,body,doing){
    busy=true;render();$id('oauth-status').textContent=doing;
    try{return await postJSON(url,body);}catch(e){$id('oauth-status').textContent=e.message;notify(e.message,true);return null;}
    finally{busy=false;render();}
  }

  $id('oauth-app').addEventListener('toggle',()=>{$id('oauth-app').dataset.touched='1';});
  $id('oauth-app-form').addEventListener('submit',async event=>{
    event.preventDefault();
    const body={store:'',application_id:$id('oauth-app-id').value.trim(),client_id:$id('oauth-client-id').value.trim(),client_secret:$id('oauth-client-secret').value.trim(),redirect_uri:$id('oauth-redirect').value.trim()};
    const result=await call('/api/amazon/public-app',body,'正在保存公共应用…');
    if(!result)return;
    $id('oauth-client-secret').value='';$id('oauth-client-id').value='';
    $id('oauth-status').textContent='公共应用已保存。';notify('公共应用已保存');
    $id('oauth-app').open=false;await loadAmazonStatus();
  });
  $id('oauth-store').addEventListener('change',()=>{$id('oauth-name').disabled=!!$id('oauth-store').value;});
  $id('oauth-start').addEventListener('click',async()=>{
    const store=$id('oauth-store').value,name=$id('oauth-name').value.trim();
    if(!store&&!name){notify('请先填写店铺名称',true);$id('oauth-name').focus();return;}
    const result=await call('/api/amazon/oauth/start',{store,name,marketplace:$id('oauth-market').value},'正在生成授权链接…');
    if(!result)return;
    link={...result,store};$id('oauth-grant').value='';
    $id('oauth-status').textContent='授权链接已生成。';render();$id('oauth-link').select();
  });
  $id('oauth-copy').addEventListener('click',async()=>{
    if(!link)return;
    try{await navigator.clipboard.writeText(link.url);notify('授权链接已复制');}
    catch(e){$id('oauth-link').select();document.execCommand('copy');notify('授权链接已复制');}
  });
  $id('oauth-open').addEventListener('click',()=>{if(link)window.open(link.url,'_blank','noopener,noreferrer');});
  $id('oauth-finish').addEventListener('click',async()=>{
    const text=$id('oauth-grant').value.trim();
    if(!text){notify('请把授权结果页上的“授权信息”粘贴进来',true);$id('oauth-grant').focus();return;}
    const result=await call('/api/amazon/oauth/finish',{text},'正在向亚马逊换取这个店铺的授权…');
    $id('oauth-grant').value='';
    if(!result)return;
    link=null;$id('oauth-name').value='';
    $id('oauth-status').textContent='店铺“'+result.name+'”已接入。可以在上面选中它，点“测试连接”。';
    notify('店铺“'+result.name+'”已接入');
    await loadAmazonStatus();
  });
})();
