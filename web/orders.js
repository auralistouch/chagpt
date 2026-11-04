/* CSV stays in the local process/browser; not persisted or sent to Binance. */
(() => {
  const el=id=>document.getElementById(id);
  const fieldNames='order_id,direction,open_time_ms,expiry_time_ms,open_index,close_index,net_payout,stake';
  const stamp=t=>new Date(t).toLocaleString('zh-CN',{hour12:false});
  const tile=(label,value)=>{const div=document.createElement('div');div.className='stat';const span=document.createElement('span');span.textContent=label;const strong=document.createElement('strong');strong.textContent=value;div.append(span,strong);return div;};
  el('order-template').addEventListener('click',()=>{
    const url=URL.createObjectURL(new Blob(['\ufeff'+fieldNames+'\n'],{type:'text/csv;charset=utf-8'}));
    const link=document.createElement('a');link.href=url;link.download='order-template.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    el('order-status').textContent='模板只含列名，请按币安订单记录填写：时间为整数毫秒，方向UP/DOWN，net_payout为净收益比例（例如0.8，不是80），stake为本金。没有到期指数可留空，记未验证。';
  });
  el('order-file').addEventListener('change',async()=>{
    el('order-stats').replaceChildren();el('order-trades').replaceChildren();
    const file=el('order-file').files[0];if(!file)return;
    try {
      if(file.size>1000000)throw new Error('CSV须小于1MB');
      const response=await fetch('/api/order-review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv:await file.text()}),cache:'no-store'});
      const data=await response.json();if(!response.ok)throw new Error(data.error || '无法核算');
      el('order-status').textContent=data.note+' 这些数据不参与策略选参或提醒。';
      el('order-stats').replaceChildren(tile('已导入',data.imported),tile('单点完整订单',data.completed),tile('累计命中率（平价计入总数）',data.win_rate_including_ties_pct===null?'无数据':data.win_rate_including_ties_pct+'%'),tile('胜 / 亏 / 平 / 未验证',`${data.wins} / ${data.losses} / ${data.ties} / ${data.unverified}`),tile('按导入赔率算净盈亏',data.profit_usdt+' USDT'));
      el('order-trades').replaceChildren(...data.trades.slice(-100).reverse().map(row=>{
        const tr=document.createElement('tr');
        for(const value of [row.order_id,row.direction==='UP'?'看涨':'看跌',stamp(row.open_time),row.entry??'无端点',stamp(row.expiry),row.exit??'无端点',{WIN:'胜',LOSS:'亏',TIE:'平价退款',UNVERIFIED:'未验证'}[row.outcome]]){const td=document.createElement('td');td.textContent=value;tr.append(td);}return tr;
      }));
    }catch(error){el('order-status').textContent='未完成核算：'+error.message;}
  });
})();
