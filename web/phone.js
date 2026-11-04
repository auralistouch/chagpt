/* Device credentials stay on the local Mac; never echo them in status output. */
(() => {
  const currentPolicy = 'latest_v6';
  const currentVersion = 'precision_v6.0';
  const strategyName = '多周期指数共识研究';
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
    status(data.available === false ? data.message : `${data.configured ? '推送地址已保存在本机' : '尚未保存推送地址'}；${data.enabled ? '手机监测已启用' : '手机监测未启用'}。${data.message}。监测策略：${strategyName}，${source}，模拟净收益 ${Math.round((data.payout || .8)*100)}%。研究方向按当前行情条件产生，不设最低次数；准确率待持续验证，历史命中率不决定下一次方向。`);
    if (data.last_check) status(el('phone-status').textContent+` 上次检查：${new Date(data.last_check).toLocaleString('zh-CN',{hour12:false})}。`);
    if (el('phone-counts')) el('phone-counts').textContent = `本次运行已检查 ${data.check_count || 0} 次；拦截统计：${JSON.stringify(data.block_counts || {})}；上次推送结果：${data.last_push_result?.result || '尚未尝试'}（accepted仅指服务接受，unknown指未确认）。`;
    if (el('phone-counts')) el('phone-counts').textContent += ` 每日UTC实测（checks=检查，candidate_minutes=策略满足分钟，signal_attempts=方向发送尝试，accepted=服务接受）：${JSON.stringify(data.daily_utc || {})}。候选不是买卖信号；日志累计不代表已验证胜率。`;
    const activity=data.signal_activity_12h;
    const count=value => Number.isFinite(value) ? value : '未知';
    if (el('phone-activity')) {
      el('phone-activity').textContent=activity ? `最近12小时：候选 ${count(activity.candidate_count)} 个；研究方向发送尝试 ${count(activity.signal_attempts)} 次；Bark接受 ${count(activity.accepted)} 次、拒绝 ${count(activity.rejected)} 次、结果未知 ${count(activity.unknown)} 次。` : '尚无最近12小时推送统计。';
    }
    if (el('phone-activity-coverage')) {
      const observed=Number.isFinite(activity?.observation_start_ms) ? new Date(activity.observation_start_ms).toLocaleString('zh-CN',{hour12:false}) : '未知';
      el('phone-activity-coverage').textContent=activity ? `${activity.full_window && activity.tracking_complete ? '已覆盖完整12小时。' : '统计覆盖不足完整12小时或记录不完整，以上仅为已记录次数。'} 本机观察起点：${observed}。${activity.note || 'Bark接受仅指服务接受，不证明手机已显示。'}测试通知不计入；这里只记录实际发送，不设置次数配额。` : '需要当前程序持续记录；旧研究表中的记录不计作实际手机发送。';
    }
    const currentEvents = (data.recent_events || []).filter(row => row.policy === currentPolicy || row.strategy_version === currentVersion);
    if (el('phone-events')) el('phone-events').textContent = [...currentEvents].reverse().map(row => JSON.stringify(row)).join('\n');
    if (updateControls) {
      el('phone-enabled').checked = data.configured ? !!data.enabled : true;
      el('policy').value = currentPolicy;
    }
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
        show(await request('/api/phone', {key:'', enabled:true, mode:lastState.mode, payout:lastState.payout, policy:currentPolicy}),true);
      } else {
        const result = await request('/api/phone', {
          key:el('phone-key').value, enabled:el('phone-enabled').checked,
          mode:el('mode').value, payout:Number(el('payout').value)/100, policy:currentPolicy
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

