// Store analysis page: profit, returns, listing issues, inbound shipments, review requests.
// Everything reads only, except "发送评论请求", which a person ticks and confirms batch by batch.
(function(){
  const id=name=>document.getElementById(name);
  const market=()=>id('insights-market').value;
  const VAT={AE:5,SA:15,US:0};
  const state={profit:null,returns:null,issues:null,inbound:null,reviews:null,run:0,stop:false};
  const small=(text,cls)=>el('small',cls||'',text);
  const kpis=(box,items)=>box.replaceChildren(...items.map(([label,value,neg])=>{const k=el('div','insights-kpi');k.append(el('span','',label),el('b',neg?'negative':'',value));return k;}));
  const neg=v=>Number(v)<0;
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  function syncVat(){if(id('profit-vat').dataset.touched!=='1')id('profit-vat').value=VAT[market()]??0;id('profit-vat-included').checked=market()!=='US';}
  id('profit-vat').addEventListener('input',()=>{id('profit-vat').dataset.touched='1';});
  id('insights-market').addEventListener('change',()=>{syncVat();reset();});
  syncVat();

  // --- profit ---
  id('profit-run').addEventListener('click',()=>withButton(id('profit-run'),'正在读取财务流水…',async()=>{
    syncVat();id('profit-status').textContent='正在读取亚马逊财务流水，约需 10–40 秒…';
    const vat=Number.parseFloat(id('profit-vat').value);
    try{state.profit=await postJSON('/api/amazon/insights/profit',{marketplace:market(),days:Number(id('profit-days').value),vat_rate:Number.isFinite(vat)?vat:null,vat_included:id('profit-vat-included').checked});}
    catch(e){id('profit-status').textContent=e.message;throw e;}
    renderProfit();
  }));
  function renderProfit(){
    const d=state.profit;id('profit-result').hidden=!d;id('profit-export').hidden=!d;id('profit-costs-fill').hidden=!(d&&d.cost_missing.length);if(!d)return;
    const t=d.totals,c=d.currency;
    kpis(id('profit-summary'),[['销售额',t.sales+' '+c],['亚马逊费用',t.fees,neg(t.fees)],['退款',t.refunds,neg(t.refunds)],...(Number(t.promotions)?[['促销',t.promotions,neg(t.promotions)]]:[]),['增值税（估算）',t.vat],['成本',t.cost],['利润',t.profit+' '+c,neg(t.profit)],['店铺月租等',t.service_fees,neg(t.service_fees)],['扣除月租后',t.profit_after_service,neg(t.profit_after_service)]]);
    id('profit-rows').replaceChildren(...d.items.map(i=>{const tr=el('tr');
      cell(tr,'').append(el('b','',i.sku),i.cost_missing?small('未填成本','negative'):'');
      cell(tr,'').append(el('b','',String(i.units)),small(i.refund_units?'退款 '+i.refund_units+' 件':''));
      cell(tr,'').append(el('b','',i.sales),small(Number(i.other_income)?'运费等 '+i.other_income:''));
      cell(tr,i.fees,neg(i.fees)?'negative':'');
      cell(tr,'').append(el('b',neg(i.refunds)?'negative':'',i.refunds),small(Number(i.promotions)?'促销 '+i.promotions:''));
      cell(tr,i.vat);cell(tr,'').append(el('b','',i.cost??'—'),small(i.unit_cost?'单件 '+i.unit_cost:''));
      cell(tr,'').append(el('b',neg(i.profit)?'negative':'',i.profit));cell(tr,i.margin==null?'—':i.margin+'%',neg(i.margin)?'negative':'');return tr;}));
    id('profit-status').textContent=d.marketplace+' 站点最近 '+d.days+' 天，'+d.items.length+' 个 SKU，币种 '+(c||'—')+'。增值税按 '+d.vat_rate+'%'+(d.vat_included?'从含税售价里扣出':'不扣')+'，仅为估算，以税务申报为准。'+(d.cost_missing.length?d.cost_missing.length+' 个 SKU 没填成本，利润偏高。':'')+(d.truncated?'流水太多，只读了一部分，请缩短天数。':'');
  }
  id('profit-costs-toggle').addEventListener('click',()=>withButton(id('profit-costs-toggle'),'正在读取…',async()=>{
    const box=id('profit-costs');if(!box.hidden){box.hidden=true;return;}
    const data=await postJSON('/api/amazon/insights/costs',{marketplace:market()});
    id('profit-costs-text').value=Object.entries(data.costs).map(([sku,cost])=>sku+', '+cost).join('\n');box.hidden=false;
  }));
  id('profit-costs-fill').addEventListener('click',()=>{if(!state.profit)return;id('profit-costs').hidden=false;const area=id('profit-costs-text'),have=new Set(area.value.split('\n').map(l=>l.split(',')[0].trim()));area.value=[area.value.trim(),...state.profit.cost_missing.filter(s=>!have.has(s)).map(s=>s+', ')].filter(Boolean).join('\n');area.focus();});
  id('profit-costs-save').addEventListener('click',()=>withButton(id('profit-costs-save'),'正在保存…',async()=>{
    const values={};
    for(const line of id('profit-costs-text').value.split('\n')){if(!line.trim())continue;const at=line.lastIndexOf(',');const sku=(at<0?line:line.slice(0,at)).trim(),cost=at<0?'':line.slice(at+1).trim();if(sku)values[sku]=cost;}
    const data=await postJSON('/api/amazon/insights/costs',{marketplace:market(),values});
    notify('已保存 '+Object.keys(data.costs).length+' 个 SKU 的成本');if(state.profit)id('profit-status').textContent='成本已保存，请重新点“计算利润”。';
  }));
  id('profit-export').addEventListener('click',()=>{const d=state.profit;if(!d)return;saveCsv([['SKU','销量','退款件数','销售额','运费等收入','促销','亚马逊费用','退款','增值税','单件成本','成本','利润','利润率%'],...d.items.map(i=>[i.sku,i.units,i.refund_units,i.sales,i.other_income,i.promotions,i.fees,i.refunds,i.vat,i.unit_cost??'',i.cost??'',i.profit,i.margin??''])],'利润_'+d.marketplace+'_'+d.days+'天.csv');});

  // --- returns ---
  id('returns-run').addEventListener('click',()=>withButton(id('returns-run'),'正在生成…',async()=>{
    const run=++state.run,days=Number(id('returns-days').value);
    id('returns-status').textContent='正在请亚马逊生成退货报告和订单报告…';
    const ids=await postJSON('/api/amazon/insights/returns/start',{marketplace:market(),days});
    for(let attempt=0;attempt<60;attempt++){
      await wait(attempt?10000:5000);if(run!==state.run)return;
      const result=await postJSON('/api/amazon/insights/returns',{marketplace:market(),...ids});
      if(result.status==='DONE'){state.returns=result;renderReturns();return;}
      if(result.status==='FATAL'){id('returns-status').textContent=result.note||'亚马逊生成报告失败，请稍后重试';return;}
      id('returns-status').textContent='亚马逊正在生成报告，已等 '+Math.round((5+attempt*10)/6)/10+' 分钟…';
    }
    id('returns-status').textContent='等了 10 分钟报告还没生成好，请稍后再试。';
  }));
  function renderReturns(){
    const d=state.returns;id('returns-result').hidden=!d;id('returns-export').hidden=!(d&&d.items.length);if(!d)return;
    kpis(id('returns-summary'),[['退货件数',String(d.total_returned)],['同期销量',String(d.total_sold)],['整体退货率',d.total_sold?(Math.round(d.total_returned/d.total_sold*1000)/10)+'%':'—'],['需要关注的商品',String(d.flagged),d.flagged>0]]);
    id('returns-rows').replaceChildren(...d.items.map(i=>{const tr=el('tr');
      cell(tr,'').append(el('b','',i.sku),small([i.asin,i.title].filter(Boolean).join(' · ').slice(0,80)),i.flag?small('需要关注','negative'):'');
      cell(tr,i.returned+' / '+(i.sold||'—'));cell(tr,i.rate==null?'—':i.rate+'%',i.flag?'negative':'');
      cell(tr,'').append(...i.top_reasons.map(r=>small(r.label+' × '+r.count)));
      cell(tr,'').append(...i.disposition_list.map(r=>small(r.label+' × '+r.count)));return tr;}));
    id('returns-status').textContent=d.empty?'这段时间没有亚马逊物流退货。':d.marketplace+' 站点最近 '+d.days+' 天：'+d.items.length+' 个 SKU 有退货。主要原因：'+(d.reasons.slice(0,3).map(r=>r.label+' '+r.count).join('、')||'—')+'。';
  }
  id('returns-export').addEventListener('click',()=>{const d=state.returns;if(!d)return;saveCsv([['SKU','ASIN','商品','退货件数','同期销量','退货率%','需要关注','主要原因','退回去向'],...d.items.map(i=>[i.sku,i.asin,i.title,i.returned,i.sold,i.rate??'',i.flag?'是':'',i.top_reasons.map(r=>r.label+'×'+r.count).join('; '),i.disposition_list.map(r=>r.label+'×'+r.count).join('; ')])],'退货分析_'+d.marketplace+'.csv');});

  // --- listing issues ---
  id('issues-run').addEventListener('click',()=>withButton(id('issues-run'),'正在读取…',async()=>{
    id('issues-status').textContent='正在读取有问题的商品…';
    try{state.issues=await postJSON('/api/amazon/insights/issues',{marketplace:market()});}catch(e){id('issues-status').textContent=e.message;throw e;}
    renderIssues();
  }));
  function renderIssues(){
    const d=state.issues;id('issues-result').hidden=!d;id('issues-export').hidden=!(d&&d.items.length);if(!d)return;
    id('issues-rows').replaceChildren(...d.items.map(i=>{const tr=el('tr');
      cell(tr,'').append(el('b','',i.sku),small([i.asin,i.title].filter(Boolean).join(' · ').slice(0,80)));
      const s=cell(tr,'');const chip=el('span','report-level '+(i.buyable?'level-ok':'level-error'),i.buyable?'可售':'不可售');s.append(chip);
      const box=cell(tr,'');for(const issue of i.issues.slice(0,6)){const line=el('div','issue-line');line.append(reportChip(issue.severity),' ',el('b','',issue.title+(issue.attributes.length?' · '+issue.attributes.join('、'):'')),small(issue.hint),small('亚马逊原文'+(issue.code?' '+issue.code:'')+'：'+issue.message,'report-original'));box.append(line);}
      if(i.issues.length>6)box.append(small('还有 '+(i.issues.length-6)+' 条'));return tr;}));
    id('issues-status').textContent=d.items.length?d.marketplace+' 站点：'+d.items.length+' 个商品有问题，其中 '+d.counts.not_buyable+' 个不能售卖、'+d.counts.errors+' 个有错误。'+(d.truncated?'只读了前 500 个。':''):'没有被亚马逊标出错误或警告的商品。';
  }
  id('issues-export').addEventListener('click',()=>{const d=state.issues;if(!d)return;saveCsv([['SKU','ASIN','商品','可售','错误数','警告数','问题'],...d.items.map(i=>[i.sku,i.asin,i.title,i.buyable?'是':'否',i.errors,i.warnings,i.issues.map(x=>x.title+'：'+x.message).join(' | ')])],'商品问题_'+d.marketplace+'.csv');});

  // --- inbound shipments ---
  id('inbound-run').addEventListener('click',()=>withButton(id('inbound-run'),'正在读取…',async()=>{
    id('inbound-status').textContent='正在读取入库货件，约需 10–20 秒…';
    try{state.inbound=await postJSON('/api/amazon/insights/inbound',{marketplace:market(),days:Number(id('inbound-days').value)});}catch(e){id('inbound-status').textContent=e.message;throw e;}
    renderInbound();
  }));
  function renderInbound(){
    const d=state.inbound;id('inbound-result').hidden=!d;id('inbound-export').hidden=!(d&&d.items.length);if(!d)return;
    id('inbound-rows').replaceChildren(...d.items.map(s=>{const tr=el('tr');
      cell(tr,'').append(el('b','',s.id),small(s.name));cell(tr,'').append(el('span','report-level '+(s.short?'level-error':s.status==='CLOSED'?'level-ok':'level-warning'),s.short?'少收':s.status_label),s.short?small(s.status_label):'');
      cell(tr,s.center||'—');cell(tr,s.shipped==null?'—':String(s.shipped));cell(tr,s.received==null?'—':String(s.received));
      const diff=s.shipped==null?null:s.received-s.shipped;cell(tr,diff==null?(s.error||'—'):String(diff),diff<0?'negative':'');return tr;}));
    id('inbound-status').textContent=d.items.length?d.marketplace+' 站点：'+d.items.length+' 个货件，进行中 '+d.counts.open+' 个'+(d.counts.short?'，已完成但少收 '+d.counts.short+' 个，请去卖家后台核对':'')+'。读取了 '+d.detailed+' 个货件的明细；进行中的货件“已接收”少于“发出”是正常的。':'这段时间没有入库货件。';
  }
  id('inbound-export').addEventListener('click',()=>{const d=state.inbound;if(!d)return;const rows=[['货件编号','货件名称','状态','仓库','SKU','发出','已接收','差异']];for(const s of d.items){if(s.skus&&s.skus.length)for(const k of s.skus)rows.push([s.id,s.name,s.status_label,s.center,k.sku,k.shipped,k.received,k.received-k.shipped]);else rows.push([s.id,s.name,s.status_label,s.center,'',s.shipped??'',s.received??'',s.shipped==null?'':s.received-s.shipped]);}saveCsv(rows,'入库货件_'+d.marketplace+'.csv');});

  // --- review requests ---
  const RESULT={sent:'已发送',not_eligible:'不能请求',failed:'失败',skipped:'已处理过'};
  id('reviews-load').addEventListener('click',()=>withButton(id('reviews-load'),'正在读取…',async()=>{
    try{state.reviews=await postJSON('/api/amazon/reviews/orders',{marketplace:market()});}catch(e){id('reviews-status').textContent=e.message;throw e;}
    state.reviews.results={};renderReviews();
  }));
  function picked(){return [...document.querySelectorAll('#reviews-rows input[type=checkbox]:checked')].map(b=>b.value);}
  function renderReviews(){
    const d=state.reviews;id('reviews-result').hidden=!d;if(!d)return;
    id('reviews-rows').replaceChildren(...d.orders.map(o=>{const tr=el('tr'),done=d.results[o.id]||(o.done?{result:o.done==='sent'?'sent':'not_eligible',note:o.done==='sent'?'之前已发送':'之前检查过，不能请求'}:null);
      const box=el('input');box.type='checkbox';box.value=o.id;box.disabled=!!done&&done.result!=='failed';box.addEventListener('change',updateSend);cell(tr,'').append(box);
      cell(tr,o.id);cell(tr,(o.purchased||'').slice(0,10));cell(tr,o.channel==='AFN'?'亚马逊配送':'自配送');cell(tr,o.total.amount?o.total.amount+' '+o.total.currency:'—');
      cell(tr,'').append(done?el('span','report-level '+(done.result==='sent'?'level-ok':done.result==='failed'?'level-error':'level-warning'),RESULT[done.result]||done.result):'',done&&done.note&&done.note!==RESULT[done.result]?small(done.note):'');return tr;}));
    id('reviews-all').checked=false;updateSend();
    const left=d.orders.filter(o=>!o.done&&!d.results[o.id]).length;
    id('reviews-status').textContent=d.orders.length?d.marketplace+' 站点：'+d.orders.length+' 个已发货订单，'+left+' 个还没处理。'+(d.truncated?'订单较多，只读了前 500 个。':''):'5–35 天前没有已发货的订单。';
  }
  function updateSend(){const n=picked().length;id('reviews-send').disabled=!n;id('reviews-send').textContent=n?'发送评论请求（'+n+' 个）':'发送评论请求';}
  id('reviews-all').addEventListener('change',()=>{for(const b of document.querySelectorAll('#reviews-rows input[type=checkbox]'))if(!b.disabled)b.checked=id('reviews-all').checked;updateSend();});
  id('reviews-stop').addEventListener('click',()=>{state.stop=true;id('reviews-stop').textContent='正在停止…';});
  id('reviews-send').addEventListener('click',async()=>{
    const orders=picked();if(!orders.length)return;
    if(!confirm('确定让亚马逊向这 '+orders.length+' 个订单的买家发送评论请求吗？\n\n每个订单只能请求一次，发出后不能撤回。'))return;
    state.stop=false;id('reviews-send').disabled=true;id('reviews-stop').hidden=false;id('reviews-stop').textContent='停止';
    let sent=0;
    try{
      for(let start=0;start<orders.length&&!state.stop;start+=20){
        const batch=orders.slice(start,start+20);
        id('reviews-status').textContent='正在发送第 '+(start+1)+'–'+(start+batch.length)+' 个，共 '+orders.length+' 个（每个约 2 秒）…';
        const result=await postJSON('/api/amazon/reviews/send',{marketplace:market(),orders:batch,confirmed:true});
        for(const r of result.items)state.reviews.results[r.id]=r;sent+=result.sent;renderReviews();
      }
      notify(state.stop?'已停止，发送了 '+sent+' 个':'发送完成，成功 '+sent+' 个');
    }catch(e){notify(e.message,true);id('reviews-status').textContent=e.message+'（已发送 '+sent+' 个，其余没有发送）';}
    finally{id('reviews-stop').hidden=true;updateSend();}
  });

  function reset(){state.run++;state.stop=true;for(const k of ['profit','returns','issues','inbound','reviews'])state[k]=null;renderProfit();renderReturns();renderIssues();renderInbound();renderReviews();id('profit-costs').hidden=true;for(const n of ['profit-status','returns-status','issues-status','inbound-status','reviews-status'])id(n).textContent='';}
  window.resetInsights=reset;
  // The global site switch sets this page's (hidden) site select; results of the old site are cleared.
  let last=market();
  window.onInsightsMarket=value=>{if(value!==last){last=value;syncVat();reset();}};
})();
