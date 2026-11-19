// Offers watch (hijackers and the Buy Box) and restock suggestions. Both only read the store.
(function(){
  const id=name=>document.getElementById(name);
  const STATE={ours:['购物车在我们这','state-ok'],shared:['有跟卖','state-warn'],lost:['购物车被抢','state-bad'],no_buy_box:['无购物车','state-warn'],not_selling:['我们没在卖','state-dim'],error:['读取失败','state-dim']};
  const RESTOCK_CLASS={out:'state-bad',urgent:'state-bad',soon:'state-warn',ok:'state-ok',slow:'state-dim',idle:'state-dim'};
  // view goes up on every store or site change; replies that come back after that are dropped.
  let watchResult=null,restockResult=null,restockRun=0,view=0;
  const current=()=>{const at=view;return ()=>at===view;};
  const tag=(text,cls)=>el('span',cls,text);
  const money=o=>o?o.price+' '+o.currency:'—';

  // --- offers watch ---
  id('watch-from-fba').addEventListener('click',()=>withButton(id('watch-from-fba'),'正在读取…',async()=>{
    const fresh=current();let stock;
    try{stock=await postJSON('/api/amazon/fba-inventory',{marketplace:id('watch-market').value});}catch(e){if(!fresh())return;throw e;}
    if(!fresh())return;
    // Out-of-stock items are kept: that is when another seller most easily takes the Buy Box.
    const ranked=stock.items.filter(i=>i.asin).sort((a,b)=>(b.fulfillable||0)-(a.fulfillable||0));
    const asins=[...new Set(ranked.map(i=>i.asin))];
    id('watch-asins').value=asins.slice(0,20).join('\n');
    id('watch-status').textContent=asins.length?'已从物流库存带入 '+Math.min(asins.length,20)+' 个 ASIN'+(asins.length>20?'（共 '+asins.length+' 个，一次最多查 20 个，其余请分批）':'')+'。':'物流库存里没有读到 ASIN。';
  }));
  id('watch-run').addEventListener('click',()=>withButton(id('watch-run'),'正在检查…',async()=>{
    const asins=id('watch-asins').value.split(/[\s,;，；]+/).map(s=>s.trim()).filter(Boolean);
    if(!asins.length){notify('请先填写要检查的 ASIN',true);return;}
    id('watch-status').textContent='正在读取 '+asins.length+' 个 ASIN 的报价，大约需要 '+Math.ceil(asins.length*2.2)+' 秒…';
    const fresh=current();let data;
    try{data=await postJSON('/api/amazon/offers-watch',{marketplace:id('watch-market').value,asins});}
    catch(e){if(!fresh())return;id('watch-status').textContent=e.message;throw e;}
    if(!fresh())return;
    watchResult=data;renderWatch();
  }));
  function renderWatch(){
    const data=watchResult;id('watch-result').hidden=!data;id('watch-export').hidden=!data;if(!data)return;
    id('watch-rows').replaceChildren(...data.items.map(item=>{
      const tr=el('tr');cell(tr,'').append(el('b','',item.asin));
      if(item.error){const c=cell(tr,item.error);c.colSpan=4;return tr;}
      const [label,cls]=STATE[item.state]||STATE.error;
      const state=cell(tr,'');state.append(tag(label,'watch-state '+cls),el('small','',item.note));
      cell(tr,'').append(el('b','',item.buy_box_own?'我们':item.buy_box_seller?'其他卖家':'—'),el('small','',item.buy_box_price?item.buy_box_price.amount+' '+item.buy_box_price.currency:''));
      cell(tr,'').append(el('b','',money(item.mine)),el('small','',item.mine?(item.mine.fba?'亚马逊配送':'自配送'):''));
      const others=cell(tr,'');
      if(!item.others.length)others.textContent=item.first_check?'没有其他卖家（首次检查）':'没有其他卖家';
      for(const o of item.others.slice(0,6)){
        const line=el('small',item.new_sellers.includes(o.seller)?'watch-new':'');
        const link=el('a','',o.seller||'未知卖家');if(o.link){link.href=o.link;link.target='_blank';link.rel='noopener noreferrer';}
        line.append(item.new_sellers.includes(o.seller)?'新 · ':'',link,' · '+money(o)+(o.fba?' · 亚马逊配送':'')+(o.buy_box?' · 拿着购物车':''));others.append(line);
      }
      if(item.other_count>6)others.append(el('small','','还有 '+(item.other_count-6)+' 个'));
      return tr;
    }));
    const c=data.counts,lost=(c.lost||0),shared=(c.shared||0);
    id('watch-status').textContent=data.marketplace+' 站点检查完成：购物车被抢 '+lost+' 个，有跟卖 '+shared+' 个，正常 '+(c.ours||0)+' 个。'+(data.previous_at?'和上次检查（'+new Date(data.previous_at).toLocaleString('zh-CN',{hour12:false}).slice(0,-3)+'）对比。':'这是第一次检查，下次检查会标出新出现的卖家。');
    if(lost||data.items.some(i=>i.new_sellers&&i.new_sellers.length))notify('有商品的购物车被抢或出现了新卖家，请查看',true);
  }
  id('watch-export').addEventListener('click',()=>{if(!watchResult)return;saveCsv([['ASIN','情况','说明','购物车','我们的价格','其他卖家数','其他卖家（卖家编号 价格）','新出现的卖家'],...watchResult.items.map(i=>i.error?[i.asin,'读取失败',i.error,'','','','','']:[i.asin,(STATE[i.state]||STATE.error)[0],i.note,i.buy_box_own?'我们':i.buy_box_seller,money(i.mine),i.other_count,i.others.map(o=>o.seller+' '+o.price).join('; '),i.new_sellers.join('; ')])],'跟卖监控_'+watchResult.marketplace+'.csv');});

  // --- restock ---
  const int=name=>Number.parseInt(id(name).value,10);
  id('restock-run').addEventListener('click',()=>withButton(id('restock-run'),'正在生成…',async()=>{
    const run=++restockRun,market=id('restock-market').value,days=int('restock-days'),lead=int('restock-lead'),cover=int('restock-cover');
    id('restock-status').textContent='正在请亚马逊生成最近 '+days+' 天的订单报告…';
    const store=currentStore;let started;
    try{started=await postJSON('/api/amazon/restock/start',{marketplace:market,days,store});}catch(e){if(run!==restockRun)return;throw e;}
    if(run!==restockRun)return;
    for(let attempt=0;attempt<60;attempt++){
      await new Promise(r=>setTimeout(r,attempt?10000:5000));
      if(run!==restockRun)return;
      let result;
      try{result=await postJSON('/api/amazon/restock',{marketplace:market,store,id:started.id,lead_days:lead,cover_days:cover});}catch(e){if(run!==restockRun)return;throw e;}
      if(run!==restockRun)return;
      if(result.status==='DONE'){restockResult=result;renderRestock();return;}
      if(result.status==='CANCELLED'||result.status==='FATAL'){id('restock-status').textContent=result.note||'亚马逊没有生成订单报告';return;}
      id('restock-status').textContent='亚马逊正在生成订单报告（'+(result.status==='IN_PROGRESS'?'处理中':'排队中')+'），已等 '+Math.round((5+attempt*10)/60*10)/10+' 分钟…';
    }
    id('restock-status').textContent='等了 10 分钟订单报告还没生成好，请稍后再试。';
  }));
  function renderRestock(){
    const data=restockResult;id('restock-result').hidden=!data;id('restock-export').hidden=!data;if(!data)return;
    id('restock-summary').replaceChildren(...['out','urgent','soon','slow','ok'].filter(k=>data.counts[k]).map(k=>tag(data.labels[k]+' '+data.counts[k],'restock-state '+RESTOCK_CLASS[k])));
    id('restock-rows').replaceChildren(...data.items.filter(i=>i.state!=='idle').map(i=>{
      const tr=el('tr');cell(tr,'').append(el('b','',i.sku),el('small','',[i.asin,i.title].filter(Boolean).join(' · ').slice(0,80)));
      cell(tr,'').append(tag(i.state_label,'restock-state '+RESTOCK_CLASS[i.state]));
      cell(tr,'').append(el('b','',String(i.daily)),el('small','','近 7 天 '+i.sold_7+' · 共 '+i.sold));
      cell(tr,String(i.fulfillable));cell(tr,String(i.inbound));
      cell(tr,'').append(el('b','',i.days_left==null?'—':i.days_left+' 天'),el('small','',i.days_left_with_inbound==null?'':'含在途 '+i.days_left_with_inbound+' 天'));
      cell(tr,'').append(el('b','',i.suggest?String(i.suggest)+' 件':'—'));
      return tr;
    }));
    id('restock-status').textContent=data.marketplace+' 站点：按最近 '+data.days+' 天销量、到货 '+data.lead_days+' 天、覆盖 '+data.cover_days+' 天计算。建议数量 = 日均销量 ×（到货天数 + 覆盖天数）− 可售 − 入库途中。只统计亚马逊配送的订单。'+(data.stock_truncated?'物流库存 SKU 太多，只读到一部分。':'');
  }
  id('restock-export').addEventListener('click',()=>{if(!restockResult)return;saveCsv([['SKU','ASIN','商品','情况','统计期销量','近7天销量','日均销量','可售','入库途中','预留','可售天数','含在途可售天数','建议补货'],...restockResult.items.map(i=>[i.sku,i.asin,i.title,i.state_label,i.sold,i.sold_7,i.daily,i.fulfillable,i.inbound,i.reserved,i.days_left??'',i.days_left_with_inbound??'',i.suggest])],'补货建议_'+restockResult.marketplace+'.csv');});
  window.resetWatch=()=>{view++;watchResult=null;restockResult=null;restockRun++;renderWatch();renderRestock();for(const n of ['watch-status','restock-status'])id(n).textContent='';};
})();
