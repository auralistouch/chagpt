/* Device credentials stay on the local Mac; never echo them in status output. */
(() => {
  const el = id => document.getElementById(id);
  const status = message => { el('phone-status').textContent = message; };
  let busy = false;
  let timer;
  let lastState;
  async function request(path, values) {
    const response = await fetch(path, values === undefined ? {cache:'no-store'} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(values), cache:'no-store'
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '手机提醒请求失败');
    return result;
  }
  function show(data, updateControls=false) {
    lastState=data;
    const source = data.mode === 'coinbase' ? 'Coinbase BTC/USD（不发合约提醒）' : '币安 BTCUSDT 指数';
    status(data.available === false ? data.message : `${data.configured ? '推送地址已保存在本机' : '尚未保存推送地址'}；${data.enabled ? '手机监测已启用' : '手机监测未启用'}。${data.message}。监测配置：${source}，模拟净收益 ${Math.round((data.payout || .8)*100)}%。`);
    if (data.last_check) status(el('phone-status').textContent+` 上次检查：${new Date(data.last_check).toLocaleString('zh-CN',{hour12:false})}。`);
    if (el('phone-counts')) el('phone-counts').textContent = `本次运行已检查 ${data.check_count || 0} 次；拦截统计：${JSON.stringify(data.block_counts || {})}；上次推送结果：${data.last_push_result?.result || '尚未尝试'}（accepted仅指服务接受，unknown指未确认）。`;
    if (el('phone-counts')) el('phone-counts').textContent += ` 每日UTC实测（checks=检查，candidate_minutes=五项全过分钟，signal_attempts=正式尝试，accepted=服务接受）：${JSON.stringify(data.daily_utc || {})}。候选不是买卖信号；日志累计不代表已验证胜率。`;
    if (el('phone-events')) el('phone-events').textContent = [...(data.recent_events || [])].reverse().map(row => JSON.stringify(row)).join('\n');
    if (updateControls) el('phone-enabled').checked = data.configured ? !!data.enabled : true;
    el('phone-test').disabled = !data.configured;
  }
  async function poll(initial=false) {
    if (busy) { timer=setTimeout(()=>poll(),15000);return; }
    try { show(await request('/api/phone'), initial); }
    catch { status('无法获取手机监测状态，请确认 Mac 程序仍在运行。'); }
    finally { timer=setTimeout(()=>poll(),15000); }
  }
  async function action(kind='save') {
    if (busy) return;
    busy = true; el('phone-save').disabled = true; el('phone-test').disabled = true; el('phone-stop').disabled = true; el('phone-enabled').disabled = true;
    try {
      if (kind === 'test') {
        status((await request('/api/phone/test', {})).message);
      } else if (kind === 'stop') {
        show(await request('/api/phone/stop', {}),true);
      } else if (kind === 'enable') {
        show(await request('/api/phone', {key:'', enabled:true, mode:lastState.mode, payout:lastState.payout}),true);
      } else {
        const result = await request('/api/phone', {
          key:el('phone-key').value, enabled:el('phone-enabled').checked,
          mode:el('mode').value, payout:Number(el('payout').value)/100
        });
        el('phone-key').value='';
        show(result,true);
      }
    } catch (error) { status(error instanceof TypeError ? '浏览器未能连接本机程序，请保持 start.command 运行。' : error.message); }
    finally {
      busy=false;el('phone-save').disabled=false;el('phone-stop').disabled=false;el('phone-enabled').disabled=false;
      clearTimeout(timer);timer=setTimeout(()=>poll(),20000);
      // A successful configuration can always be tested; failures are also
      // rechecked without exposing the saved credential.
      try { const data=await request('/api/phone');el('phone-test').disabled=!data.configured; } catch { /* Keep disabled. */ }
    }
  }
  el('phone-save').addEventListener('click',()=>action());
  el('phone-test').addEventListener('click',()=>action('test'));
  el('phone-stop').addEventListener('click',()=>action('stop'));
  el('phone-enabled').addEventListener('change',()=>{
    if (!lastState?.configured) {status('默认启用手机监测；请先填写 Bark 地址并保存一次，之后刷新会自动恢复。');return;}
    action(el('phone-enabled').checked?'enable':'stop');
  });
  poll(true);
})();
