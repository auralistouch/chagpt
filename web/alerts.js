/* Opt-in, local alerts only. No account, push server, or trading integration. */
(() => {
  const el = id => document.getElementById(id);
  const status = message => { el('alert-status').textContent = message; };
  const storageKey = 'btc-observer-last-alert-v1';
  let enabled = false;
  let context;
  let activeNotification;
  let closeTimer;
  let lastAlert;

  function readLast() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey));
      if (Number.isFinite(value?.time) && value.time > 0) lastAlert = value;
    } catch { /* Private browsing or unavailable storage: deduplicate in memory. */ }
    return lastAlert;
  }
  function pause() {
    clearTimeout(closeTimer);
    if (activeNotification) {
      try { activeNotification.close(); } catch { /* Unsupported browser close. */ }
      activeNotification = undefined;
    }
  }
  function disable(message = '提醒已停用。') {
    enabled = false; pause();
    el('enable-alerts').textContent = '启用提醒并自动刷新';
    el('test-alerts').disabled = true;
    status(message);
  }
  async function armSound() {
    try {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return false;
      if (!context || context.state === 'closed') context = new Audio();
      await context.resume();
      return context.state === 'running';
    } catch { return false; }
  }
  function tone() {
    if (!el('sound-alerts').checked || context?.state !== 'running') return false;
    try {
      [880, 1046, 880].forEach((frequency, i) => {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        const start = context.currentTime+i*0.28;
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.001, start);
        gain.gain.exponentialRampToValueAtTime(0.12, start+0.025);
        gain.gain.exponentialRampToValueAtTime(0.001, start+0.20);
        oscillator.connect(gain); gain.connect(context.destination);
        oscillator.start(start); oscillator.stop(start+0.22);
      });
      return true;
    } catch { return false; }
  }
  function desktop(title, body, tag, lifetime = 10000) {
    if (!el('desktop-alerts').checked || !('Notification' in window) || Notification.permission !== 'granted') return false;
    try {
      pause();
      activeNotification = new Notification(title, {body, tag});
      activeNotification.onclick = () => { window.focus(); pause(); };
      closeTimer = setTimeout(pause, lifetime);
      return true;
    } catch { return false; }
  }
  async function enable() {
    if (enabled) return disable();
    if (!el('sound-alerts').checked && !el('desktop-alerts').checked) {
      status('请先勾选声音或 Mac 桌面通知。'); return;
    }
    el('enable-alerts').disabled = true;
    try {
      // Start both permission operations directly in this button's user gesture.
      const sound = el('sound-alerts').checked ? armSound() : Promise.resolve(false);
      let permission = Promise.resolve('unavailable');
      if (el('desktop-alerts').checked && 'Notification' in window && window.isSecureContext) {
        permission = Notification.permission === 'default' ? Notification.requestPermission() : Promise.resolve(Notification.permission);
      }
      const [soundReady, desktopPermission] = await Promise.all([sound, permission]);
      const desktopReady = el('desktop-alerts').checked && desktopPermission === 'granted';
      enabled = soundReady || desktopReady;
      if (!enabled) {
        status('未能启用提醒。请检查浏览器声音权限和通知权限，或换用支持的浏览器后重试。'); return;
      }
      const messages = [];
      if (el('sound-alerts').checked) messages.push(soundReady ? '声音已启用' : '声音不可用');
      if (el('desktop-alerts').checked) messages.push(desktopReady ? '桌面通知权限已获准' : '桌面通知被拒绝或此浏览器不支持');
      status(messages.join('；')+'。请点击“测试提醒”确认实际能听到或看到。');
      el('enable-alerts').textContent = '停用提醒';
      el('test-alerts').disabled = false;
      if (!el('auto').checked) {
        el('auto').checked = true;
        el('auto').dispatchEvent(new Event('change'));
      }
    } catch {
      disable('提醒权限请求失败，请检查浏览器设置后重试。');
    } finally { el('enable-alerts').disabled = false; }
  }
  function test() {
    if (!enabled) return;
    const sound = tone();
    const notified = desktop('测试提醒 · 不是交易信号', '这只是通知测试，请确认是否看见；没有产生买点。', 'btc-observer-test');
    status(sound || notified ? '已请求发送测试提醒。请确认是否听到声音或看见通知；若没有，请检查音量、浏览器权限及 Mac 专注模式。' : '测试未能发送。请重新启用提醒，并检查权限或浏览器支持。');
  }
  function maybeNotify(data) {
    if (!enabled) return;
    // Include the entire browser request as a conservative allowance for slow
    // delivery; an observation that arrives late must never sound fresh.
    const age = data.data_age_seconds+(data.request_duration_seconds || 0);
    if (data.demo || !['UP', 'DOWN'].includes(data.action) || !Number.isFinite(age) || age < 0 || age >= 90 || !Number.isFinite(data.signal_time)) {
      pause(); return;
    }
    const previous = readLast();
    // Suppress repeated refreshes, reloads, setting changes, and overlapping
    // ten-minute observations. Test alerts never consume this cooldown.
    if (previous && data.signal_time-previous.time < 600000) return;
    const direction = data.action === 'UP' ? '看涨' : '看跌';
    const stamp = new Date(data.signal_time).toLocaleString('zh-CN', {hour12: false});
    const sound = tone();
    const notified = desktop(`BTC 观察到${direction}方向`, `${data.source} · ${data.pair}\n参考价 ${data.reference_price} · 基准时间 ${stamp}\n仅供观察，不是下单指令。`, 'btc-observer-signal', Math.max(0, 90000-age*1000));
    if (sound || notified) {
      lastAlert = {time:data.signal_time};
      try { localStorage.setItem(storageKey, JSON.stringify(lastAlert)); } catch { /* Use in-memory cooldown. */ }
      status(`已请求提醒：${direction} · ${stamp}。十分钟内不重复提醒；请打开页面核对当前行情。`);
    } else {
      status('发现观察方向，但提醒未能发送。请重新启用并测试声音和通知权限。');
    }
  }
  el('enable-alerts').addEventListener('click', enable);
  el('test-alerts').addEventListener('click', test);
  ['sound-alerts', 'desktop-alerts'].forEach(id => el(id).addEventListener('change', () => disable('提醒选项已改变，请重新点击“启用提醒并自动刷新”。')));
  window.signalAlerts = {maybeNotify, pause};
})();
