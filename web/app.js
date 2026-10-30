const $ = id => document.getElementById(id);
const time = value => new Date(value).toLocaleString('zh-CN', {hour12: false});
const price = value => Number(value).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
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
  generation++;
  hasResult = false;
  clearTimeout(expiryTimer);
  $('results').hidden = true;
  $('empty').hidden = false;
  text('source', '设置已变更 · 请重新获取');
}
function pauseSignal(message) {
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
  hasResult = true;
  $('empty').hidden = true; $('error').hidden = true; $('results').hidden = false;
  text('source', data.demo ? '演示数据 · 禁止交易' : `${data.source} · 已获取`);
  text('pair-label', `最近收盘参考价 · ${data.pair}`);
  text('action', {WAIT: '等待 · 暂无可用买点', UP: '观察看涨 ↑', DOWN: '观察看跌 ↓'}[data.action]);
  $('action').className = data.action === 'UP' ? 'positive' : data.action === 'DOWN' ? 'negative' : '';
  text('signal-note', data.demo ? '这是合成行情，用于熟悉界面，任何回测数值均无交易意义。' : `基于 ${data.source} 的已收盘一分钟 K 线，仅研究价格方向，不等于事件合约交易信号。`);
  list('blockers', data.blockers);
  text('price', price(data.reference_price)); text('signal-time', time(data.signal_time));
  text('expiry', time(data.estimated_expiry)); text('age', `${data.data_age_seconds} 秒（请求时）`);
  const m = data.metrics;
  text('score', `当前满足 ${m.score} / 5 项，指标趋势：${m.direction === 'UP' ? '向上' : '向下'}；只有全部通过才进入历史表现检查。`);
  $('metrics').replaceChildren(tile('EMA20', price(m.ema20)), tile('EMA50', price(m.ema50)), tile('RSI14（简单均值）', m.rsi), tile('ATR / 价格', m.atr_pct+'%'), tile('成交量 / 前 20 分钟', m.volume_ratio+' 倍'));
  list('reasons', m.reasons.length ? m.reasons : ['尚无达标指标']);
  const b = data.holdout;
  $('stats').replaceChildren(tile('已结束的模拟笔数', b.total), tile('历史胜率', b.win_rate === null ? '无样本' : b.win_rate+'%'), tile('95% Wilson 保守下界', b.lower_bound+'%'), tile('按所填赔率计算盈亏', b.profit_units+' 单位', b.profit_units >= 0 ? 'positive' : 'negative'), tile('最大回撤', b.max_drawdown_units+' 单位'), tile('盈亏平衡胜率', data.breakeven_pct+'%'));
  text('range', `${time(b.from_time)} — ${time(b.to_time)}。胜 ${b.wins} 笔 / 亏 ${b.losses} 笔，其中平价 ${b.ties} 笔保守记亏。`);
  text('gate', `过滤门槛：后段至少 30 笔，且保守下界 > ${(data.breakeven_pct+2).toFixed(2)}%。这只是审慎过滤，不能证明未来有优势；交易结果相关时，此统计区间也可能偏乐观。`);
  text('early', `前段描述：${data.early.total} 笔，历史胜率 ${data.early.win_rate === null ? '无样本' : data.early.win_rate+'%'}。规则未根据这些结果调整。`);
  $('trades').replaceChildren(...[...b.trades].reverse().map(trade => {
    const tr = document.createElement('tr');
    for (const value of [time(trade.time), trade.direction === 'UP' ? '看涨' : '看跌', price(trade.entry), price(trade.exit), trade.tie ? '平价（记亏）' : trade.won ? '胜' : '亏']) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    tr.lastChild.className = trade.won ? 'positive' : 'negative'; return tr;
  }));
  if (!b.total) { const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 5; cell.textContent = '本段未产生已结束的模拟交易，不代表策略有效。'; row.append(cell); $('trades').append(row); }
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
    text('action', '等待 · 行情已过期'); $('action').className = '';
    text('source', '行情已过期 · 请刷新');
    text('signal-note', '已停止显示方向，请重新获取分析。');
  }, Math.max(0, 90000-data.data_age_seconds*1000));
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
  pauseSignal('正在更新行情；上次数据仅供回看，暂停方向提示。');
  text('source', '正在更新 · 暂停信号');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 100000);
  let nextDelay = 30000;
  try {
    let response;
    try {
      response = await fetch(`/api/signals?mode=${$('mode').value}&payout=${payout}`, {signal: controller.signal, cache: 'no-store'});
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('浏览器未能连接本机程序。请保持 start.command 终端打开，并确认 Mac 未进入睡眠；连接恢复后会按设置自动重试。');
    }
    let data;
    try { data = await response.json(); }
    catch { throw new Error('本机程序返回了不完整的响应，请查看 start.command 终端中的错误提示。'); }
    if (ticket !== generation) return;
    if (!response.ok) throw new Error(data.error || '行情获取失败');
    render(data);
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
