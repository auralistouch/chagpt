/* Opt-in, local alerts only. No account, push server, or trading integration. */
(() => {
  const el = id => document.getElementById(id);
  const status = message => { el('alert-status').textContent = message; };
  const storageKey = 'btc-observer-last-alert-v1';
  const preferenceKey = 'btc-observer-alert-preferences-v1';
  let preferences = {enabled:true, sound:true, desktop:true, auto:true};
  try {
    const saved=JSON.parse(localStorage.getItem(preferenceKey));
    if (saved && ['enabled','sound','desktop','auto'].every(key=>typeof saved[key]==='boolean')) preferences=saved;
  } catch { /* Defaults remain usable when browser storage is unavailable. */ }
  let enabled = false;
  let context;
  let activeNotification;
  let closeTimer;
  let lastAlert;

  function remember() {
    preferences={enabled,sound:el('sound-alerts').checked,desktop:el('desktop-alerts').checked,auto:el('auto').checked};
    try { localStorage.setItem(preferenceKey,JSON.stringify(preferences)); } catch { /* Keep current page state. */ }
  }
  function desktopReady() {
    return el('desktop-alerts').checked && 'Notification' in window && Notification.permission==='granted';
  }
  function updateControls() {
    const soundReady=el('sound-alerts').checked && context?.state==='running';
    el('enable-alerts').textContent=enabled?'停用提醒':'启用提醒并自动刷新';
    el('test-alerts').disabled=!enabled || !(soundReady || desktopReady());
    el('unlock-alerts').hidden=!enabled || ((!el('sound-alerts').checked || soundReady) && (!el('desktop-alerts').checked || desktopReady()));
  }
  function describe() {
    updateControls();
    if (!enabled) return status('提醒已停用，已记住此设置。');
    const messages=['提醒已启用，刷新后会自动恢复'];
    if (el('sound-alerts').checked) messages.push(context?.state==='running'?'声音已就绪':'声音等待浏览器解锁，请点击“授权通知 / 启用声音”');
    if (el('desktop-alerts').checked) messages.push(desktopReady()?'桌面通知权限已获准':'桌面通知尚未获准或此浏览器不支持，请点击授权；若已拒绝需在浏览器设置中允许');
    if (!el('sound-alerts').checked && !el('desktop-alerts').checked) messages.push('尚未选择提醒通道');
    status(messages.join('；')+'。请测试实际效果。');
  }

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
    remember();updateControls();
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
  async function enable(force=false) {
    if (enabled && !force) return disable();
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
      enabled = true;
      describe();
      if (!soundReady && !desktopReady) status('已记住启用设置，但声音和通知均未就绪。请检查浏览器权限，或换用支持的浏览器后再测试。');
      if (!el('auto').checked) {
        el('auto').checked = true;
        el('auto').dispatchEvent(new Event('change'));
      }
      remember();
    } catch {
      disable('提醒权限请求失败，请检查浏览器设置后重试。');
    } finally { el('enable-alerts').disabled = false; }
  }
  async function test() {
    if (!enabled) return;
    if (el('sound-alerts').checked) await armSound();
    const sound = tone();
    const notified = desktop('测试提醒 · 不是交易信号', '这只是通知测试，请确认是否看见；没有产生买点。', 'btc-observer-test');
    status(sound || notified ? '已请求发送测试提醒。请确认是否听到声音或看见通知；若没有，请检查音量、浏览器权限及 Mac 专注模式。' : '测试未能发送。请重新启用提醒，并检查权限或浏览器支持。');
  }
  function maybeNotify(data) {
    if (!enabled) return;
    // Include the entire browser request as a conservative allowance for slow
    // delivery; an observation that arrives late must never sound fresh.
    const age = data.data_age_seconds+(data.request_duration_seconds || 0);
    if (data.price_kind !== 'index' || data.demo || !['UP', 'DOWN'].includes(data.action) || !Number.isFinite(age) || age < 0 || age >= 75 || !Number.isFinite(data.signal_time)) {
      pause(); return;
    }
    const quoteAge = data.quote_age_seconds+(data.request_duration_seconds || 0);
    if (!Number.isFinite(quoteAge) || quoteAge < 0 || quoteAge >= 15) { pause(); return; }
    const previous = readLast();
    // Suppress repeated refreshes, reloads, setting changes, and overlapping
    // ten-minute observations. Test alerts never consume this cooldown.
    if (previous && data.signal_time-previous.time < 601000) return;
    const direction = data.action === 'UP' ? '看涨' : '看跌';
    const stamp = new Date(data.signal_time).toLocaleString('zh-CN', {hour12: false});
    const sound = tone();
    const notified = desktop(`BTC 观察到${direction}方向`, `${data.source} · ${data.pair}\n指数快照 ${data.reference_price} · 指标时间 ${stamp}\n仅供观察，不是下单指令。`, 'btc-observer-signal', Math.max(0, Math.min(75000-age*1000, 15000-quoteAge*1000)));
    if (sound || notified) {
      lastAlert = {time:data.signal_time};
      try { localStorage.setItem(storageKey, JSON.stringify(lastAlert)); } catch { /* Use in-memory cooldown. */ }
      status(`已请求提醒：${direction} · ${stamp}。601秒内不重复提醒；请打开页面核对当前行情。`);
    } else {
      status('发现观察方向，但提醒未能发送。请重新启用并测试声音和通知权限。');
    }
  }
  el('enable-alerts').addEventListener('click', ()=>enable());
  el('unlock-alerts').addEventListener('click', ()=>enable(true));
  el('test-alerts').addEventListener('click', test);
  ['sound-alerts', 'desktop-alerts'].forEach(id => el(id).addEventListener('change', () => {remember();describe();}));
  el('auto').addEventListener('change',remember);
  window.addEventListener('observer-ready',()=>{
    enabled=preferences.enabled;
    el('sound-alerts').checked=preferences.sound;
    el('desktop-alerts').checked=preferences.desktop;
    el('auto').checked=preferences.auto;
    if (enabled && preferences.sound) {
      try { const Audio=window.AudioContext || window.webkitAudioContext;if(Audio) context=new Audio(); } catch { /* Show pending sound status. */ }
    }
    describe();
    if (preferences.auto) el('auto').dispatchEvent(new Event('change'));
  },{once:true});
  // Browsers may suspend sound after a reload. A real user interaction can
  // resume it; notification permission is requested only by the explicit button.
  ['pointerdown','keydown'].forEach(name=>document.addEventListener(name,event=>{
    if (event.isTrusted && enabled && el('sound-alerts').checked && context?.state!=='running') armSound().then(()=>{if(enabled) describe();});
  }));
  window.signalAlerts = {maybeNotify, pause};
})();
