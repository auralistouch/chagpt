const $ = id => document.getElementById(id);
const CURRENT_POLICY = 'latest_v6';
const CURRENT_VERSION = 'precision_v6.0';
const STRATEGY_NAME = '多周期指数共识研究';
const time = value => Number.isFinite(value) ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '无数据';
const price = value => Number(value).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const percent = value => Number.isFinite(value) ? `${value}%` : '无样本';
let loading = false;
let timer;
let expiryTimer;
let generation = 0;
let failures = 0;
let hasResult = false;

function text(id, value) { $(id).textContent = value; }
function list(id, values) {
  $(id).replaceChildren(...values.map(value => {
    const li = document.createElement('li'); li.textContent = value; return li;
  }));
}
function tile(title, value, className = '') {
  const div = document.createElement('div'); div.className = 'stat';
  const label = document.createElement('span'); label.textContent = title;
  const strong = document.createElement('strong'); strong.textContent = value; strong.className = className;
  div.append(label, strong); return div;
}
function invalidate() {
  window.signalAlerts?.pause();
  generation++;
  hasResult = false;
  clearTimeout(expiryTimer);
  $('results').hidden = true;
  $('empty').hidden = false;
  text('source', '设置已变更 · 请重新获取');
}
function pauseSignal(message) {
  window.signalAlerts?.pause();
  clearTimeout(expiryTimer);
  text('action', '等待 · 暂停信号'); $('action').className = '';
  text('signal-note', message);
}
function scheduleRefresh(delay) {
  clearTimeout(timer);
  if (!$('auto').checked) return;
  text('refresh-status', failures ? `连接暂时失败，${delay/1000} 秒后自动重试（第 ${failures} 次失败）。` : `${delay/1000} 秒后自动刷新。`);
  timer = setTimeout(() => load(), delay);
}
function render(data) {
  const researchMode = data.research_mode === true;
  const action = researchMode ? data.action : 'WAIT';
  hasResult = true;
  $('empty').hidden = true; $('error').hidden = true; $('results').hidden = false;
  text('source', data.demo ? '演示数据 · 禁止交易' : `${data.source} · 已获取`);
  text('pair-label', `${data.price_kind === 'index' ? '当前指数快照' : '研究收盘价'} · ${data.pair}`);
  text('action', {WAIT: '等待 · 暂无可用买点', UP: researchMode ? '看涨研究信号 ↑' : '看涨信号 ↑', DOWN: researchMode ? '看跌研究信号 ↓' : '看跌信号 ↓'}[action] || '等待');
  $('action').className = action === 'UP' ? 'positive' : action === 'DOWN' ? 'negative' : '';
  const setup = data.setup ? ` · ${data.setup}` : '';
  const validity = Number.isFinite(data.valid_until) ? `参考有效至 ${time(data.valid_until)}。` : '';
  const note = data.demo ? '这是合成行情，用于熟悉界面，研究数值均无交易意义。' : data.price_kind === 'index' ? `${STRATEGY_NAME}${setup}。${validity}指标使用已收盘K线；当前指数快照不是实际开仓价，实际赔率和结算以订单为准。` : '独立现货行情研究，不能验证币安事件合约；不会发送合约观察提醒。';
  text('signal-note', `${researchMode ? '研究方向，准确率待持续验证。' : ''}${note}`);
  list('blockers', [...new Set([...(data.blockers || []), ...(data.warnings || [])])]);
  const quality = data.quality_assessment;
  const qualityStatus = quality?.status === 'insufficient_evidence' ? '已输出研究记录暂不足以判断稳定性' : '已输出方向的研究结果';
  text('quality-status', `${qualityStatus}。${quality?.note || '准确率待持续验证；下方结果仅作回顾，不决定下一次方向。'}`);
  text('quality-details', quality ? `已完成输出样本 ${Number.isFinite(quality.test_samples) ? quality.test_samples : '未知'}，累计命中率 ${percent(quality.test_win_rate_pct)}。统计包括策略已输出方向的全部完整已结算研究参考记录，缺失或未到期另列；这是多笔结果的累计比例，不是某一笔的成功概率，也不是未经输出的原始候选统计。` : '等待策略实际输出方向的完整已结算研究记录。回顾命中率不作为下一次方向的准入门槛，实际订单结果须另行核算。');
  text('price', price(data.reference_price)); text('signal-time', time(data.signal_time));
  text('reference-entry', time(data.reference_entry_time));
  text('reference-expiry', time(data.reference_expiry_time));
  text('expiry', time(data.estimated_expiry)); text('age', `指标 ${data.data_age_seconds} 秒${data.price_kind === 'index' ? `；指数快照 ${data.quote_age_seconds} 秒` : ''}（请求时）`);
  const m = data.metrics || {};
  const checks = Array.isArray(m.checks) ? m.checks : [];
  text('score', `当前触发检查 ${m.score ?? 0} / ${checks.length}。检查项只是规则条件，不是胜率。`);
  const metrics = Array.isArray(data.display_metrics) ? data.display_metrics : [];
  $('metrics').replaceChildren(...metrics.map(metric => tile(String(metric.label ?? ''), String(metric.value ?? '—'))));
  list('reasons', m.reasons?.length ? m.reasons : ['尚无达标指标']);
  const d = data.diagnostics;
  text('diagnostics', d ? `本次：${d.candles}根K线，策略满足${d.qualified_minutes}分钟。逐项未通过次数：${Object.entries(d.failed_rule_counts || {}).map(([k,v])=>`${k}=${v}`).join('；')}。触发分钟不等于推送次数。` : '');
  const b = data.price_kind === 'index' && !data.demo ? data.reference_study?.holdout : null;
  if (b) {
    $('stats').replaceChildren(tile('已输出方向的完整参考样本', b.total), tile('研究累计命中率（含平价样本）', b.total ? (100*b.wins/b.total).toFixed(2)+'%' : '无样本'), tile('平价', b.ties), tile('未验证（不计胜负）', b.unverified), tile('假设净盈亏', b.profit_units+' 单位'));
    text('range', `${time(b.from_time)} — ${time(b.to_time)}。研究胜 ${b.wins} / 亏 ${b.losses} / 平 ${b.ties}。不是你的实际订单胜率。`);
    text('gate', '这些是策略按当时行情输出方向后的固定时程参考记录；累计命中率只回顾完整已结算输出，不作为下一次方向的准入条件，也不代表实单结果。');
    const early = data.reference_study.early;
    text('early', early ? `前段研究样本 ${early.total}；两段都只是历史研究，不能代替新行情的持续验证。仅展示当前策略，表内为最近30笔记录。` : '仅展示当前策略，表内为最近30笔记录。');
    const rows = [...(b.trades || []), ...(b.unverified_trades || [])].sort((a,b)=>b.signal_time-a.signal_time).slice(0,30);
    $('trades').replaceChildren(...rows.map(trade => {
      const tr = document.createElement('tr');
      for (const value of [time(trade.signal_time), trade.direction === 'UP' ? '看涨' : '看跌',time(trade.entry_time),trade.entry ?? '无端点数据',time(trade.expiry),trade.exit ?? '无端点数据',{WIN:'研究胜',LOSS:'研究亏',TIE:'平价',UNVERIFIED:'未验证'}[trade.outcome]]) {
        const td=document.createElement('td');td.textContent=value;tr.append(td);
      }
      tr.lastChild.className=trade.outcome==='WIN'?'positive':trade.outcome==='LOSS'?'negative':'';return tr;
    }));
  } else {
    $('stats').replaceChildren();$('trades').replaceChildren();
    text('range','尚无策略输出方向的指数单点参考记录。');
    text('gate','实际合约胜率须使用对应订单开仓和到期单点；缺失时显示无数据。');text('early','');
  }
  const values = data.chart.map(c => c.price);
  const low = Math.min(...values), high = Math.max(...values), spread = high-low || 1;
  const points = values.map((v, i) => `${(i/(values.length-1)*980+10).toFixed(2)},${(200-(v-low)/spread*180).toFixed(2)}`).join(' ');
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', points); line.setAttribute('fill', 'none'); line.setAttribute('stroke', '#f4c15b'); line.setAttribute('stroke-width', '2');
  $('chart').replaceChildren(line);
  text('chart-range', `${time(data.chart[0].time)} — ${time(data.chart.at(-1).time)} · 最低 ${price(low)} / 最高 ${price(high)} ${data.quote}`);
  // Do not leave a once-valid direction on screen after data expires.
  clearTimeout(expiryTimer);
  expiryTimer = setTimeout(() => {
    window.signalAlerts?.pause();
    text('action', '等待 · 行情已过期'); $('action').className = '';
    text('source', '行情已过期 · 请刷新');
    text('signal-note', '已停止显示方向，请重新获取分析。');
  }, Math.max(0, Math.min((data.freshness_limit_seconds || 75)*1000-data.data_age_seconds*1000, data.price_kind === 'index' ? 15000-(data.quote_age_seconds || 0)*1000 : 75000)));
}
async function load() {
  if (loading) return;
  clearTimeout(timer);
  const payout = Number($('payout').value)/100;
  if (!Number.isFinite(payout) || payout < 0.01 || payout > 2) {
    invalidate(); text('error', '请填写 1% 至 200% 之间的净收益比例。'); $('error').hidden = false;
    text('refresh-status', '参数无效，请修改后重新获取。'); return;
  }
  loading = true; $('refresh').disabled = true; text('refresh', '正在获取…');
  text('refresh-status', '正在请求，完成后才安排下一次刷新。');
  $('error').hidden = true;
  const ticket = ++generation;
  const requestStarted = performance.now();
  pauseSignal('正在更新行情；上次数据仅供回看，暂停方向提示。');
  text('source', '正在更新 · 暂停信号');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 100000);
  let nextDelay = 30000;
  try {
    let response;
    try {
      response = await fetch(`/api/signals?mode=${$('mode').value}&payout=${payout}&policy=${CURRENT_POLICY}`, {signal: controller.signal, cache: 'no-store'});
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('浏览器未能连接本机程序。请保持 start.command 终端打开，并确认 Mac 未进入睡眠；连接恢复后会按设置自动重试。');
    }
    let data;
    try { data = await response.json(); }
    catch { throw new Error('本机程序返回了不完整的响应，请查看 start.command 终端中的错误提示。'); }
    if (ticket !== generation) return;
    if (!response.ok) throw new Error(data.error || '行情获取失败');
    if (data.policy !== CURRENT_POLICY || data.strategy_version !== CURRENT_VERSION) throw new Error('当前服务未返回最新版策略，请重新启动本机程序。');
    render(data);
    window.signalAlerts?.maybeNotify({...data, request_duration_seconds: Math.max(0, performance.now()-requestStarted)/1000});
    failures = 0;
  } catch (error) {
    if (ticket !== generation) return;
    failures++;
    nextDelay = Math.min(60000, 5000*2**Math.min(failures-1, 4));
    $('results').hidden = !hasResult; $('empty').hidden = true;
    pauseSignal('连接已中断。下方若有数据，是上次成功获取的记录，不能作为当前信号。');
    text('error', error.name === 'AbortError' ? '行情请求超时，已暂停信号。请检查本机网络及 start.command 终端。' : error.message);
    $('error').hidden = false; text('source', '未连接 · 暂停信号');
  } finally {
    clearTimeout(timeout); loading = false; $('refresh').disabled = false; text('refresh', '获取分析');
    if ($('auto').checked) scheduleRefresh(ticket === generation ? nextDelay : 0);
    else text('refresh-status', '自动刷新已关闭。可点击“获取分析”重试。');
  }
}
$('refresh').addEventListener('click', () => load());
$('mode').addEventListener('change', invalidate);
$('payout').addEventListener('input', invalidate);
$('auto').addEventListener('change', () => {
  clearTimeout(timer);
  if ($('auto').checked) { failures = 0; load(); }
  else text('refresh-status', '自动刷新已关闭。');
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && $('auto').checked && !loading) scheduleRefresh(0);
});
window.addEventListener('online', () => {
  if ($('auto').checked && !loading) scheduleRefresh(0);
});
window.dispatchEvent(new Event('observer-ready'));

