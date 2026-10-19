// Shared by every page: MQTT v5 connection, header/navigation, alarm banner + sound,
// AMR commands with request/response, toasts and formatting helpers.
const App = (() => {
  const BROKER = `ws://${location.hostname || "localhost"}:9001`;
  const cfg = window.MQTT_CONFIG || {};
  const $ = id => document.getElementById(id);

  const SEVERITY = { critical: { label: "嚴重", c: "var(--bad)" }, warning: { label: "警告", c: "var(--warn)" } };
  const ALARM_TYPE = {
    OFFLINE: "失去連線", ERROR: "故障", BATTERY_LOW: "電量偏低", BATTERY_CRITICAL: "電量極低",
    STUCK: "疑似卡住", MONITOR_OFFLINE: "監控中斷", CPU_HIGH: "CPU 過高", MEM_HIGH: "記憶體過高",
  };
  const typeLabel = t => ALARM_TYPE[t] || (t.startsWith("DISK_LOW_") ? `磁碟 ${t.slice(9)}: 空間不足` : t);

  /* ---------- formatting ---------- */
  const pad = n => String(n).padStart(2, "0");
  const fmtTime = ts => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const fmtDateTime = ts => { const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${fmtTime(ts)}`; };
  const fmtDuration = s => {
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
    return `${Math.floor(s / 3600)} 時 ${Math.floor(s % 3600 / 60)} 分`;
  };
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ---------- header ---------- */
  function mountHeader(page) {
    document.body.insertAdjacentHTML("afterbegin", `
      <header>
        <h1>AMR 監控</h1>
        <nav>
          <a href="index.html" class="${page === "dashboard" ? "active" : ""}">儀表板</a>
          <a href="alarms.html" class="${page === "alarms" ? "active" : ""}">異常記錄 <span class="count" id="navCount"></span></a>
        </nav>
        <div class="conn">
          <button class="icon-btn" id="soundBtn" title="新的嚴重異常時播放提示音"></button>
          <span id="connText">連線中…</span><span class="dot" id="connDot"></span>
        </div>
      </header>
      <div id="alarmBanner"><span class="pulse"></span><span class="msg" id="bannerMsg"></span>
        <a href="alarms.html">查看異常記錄 →</a></div>
      <div id="toasts"></div>`);
    $("soundBtn").onclick = () => { sound = !sound; store("alarmSound", sound ? "1" : "0"); renderSound();
      if (sound) beep(); };
    renderSound();
  }

  /* ---------- toasts ---------- */
  function toast(text, color = "var(--accent)", ms = 3500) {
    const el = document.createElement("div");
    el.className = "toast"; el.style.setProperty("--c", color); el.textContent = text;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  /* ---------- sound (per-viewer preference) ---------- */
  const store = (k, v) => { try { v === undefined ? null : localStorage.setItem(k, v); return localStorage.getItem(k); } catch { return null; } };
  let sound = store("alarmSound") === "1";
  let audio;
  function renderSound() { $("soundBtn").textContent = sound ? "🔔 提示音開" : "🔕 提示音關"; }
  function beep() {
    try {
      audio = audio || new AudioContext();
      [0, 0.25].forEach(t => {
        const o = audio.createOscillator(), g = audio.createGain();
        o.frequency.value = 880; g.gain.value = 0.15;
        o.connect(g).connect(audio.destination);
        o.start(audio.currentTime + t); o.stop(audio.currentTime + t + 0.15);
      });
    } catch { /* audio not available */ }
  }

  // pages include this script at the end of <body data-page="...">
  mountHeader(document.body.dataset.page);

  /* ---------- MQTT ---------- */
  const handlers = [];   // [filter, fn, qos]
  const match = (filter, topic) => {
    const f = filter.split("/"), t = topic.split("/");
    for (let i = 0; i < f.length; i++) {
      if (f[i] === "#") return true;
      if (f[i] !== "+" && f[i] !== t[i]) return false;
    }
    return f.length === t.length;
  };

  const client = mqtt.connect(BROKER, {
    protocolVersion: 5, username: cfg.username, password: cfg.password,
    clientId: "dash-" + Math.random().toString(16).slice(2, 8), reconnectPeriod: 2000,
  });
  const setConn = (on, txt) => { $("connText").textContent = txt; $("connDot").classList.toggle("on", on); };
  client.on("connect", () => {
    setConn(true, "已連線");
    const subs = {};
    handlers.forEach(([f, , q]) => subs[f] = { qos: Math.max(q, subs[f]?.qos ?? 0) });
    if (Object.keys(subs).length) client.subscribe(subs);
  });
  client.on("reconnect", () => setConn(false, "重新連線中…"));
  client.on("offline", () => setConn(false, "已斷線"));
  client.on("error", e => setConn(false, /auth/i.test(e.message) ? "帳號驗證失敗（config.js）" : "連線錯誤"));
  client.on("message", (topic, payload, packet) => {
    for (const [f, fn] of handlers) if (match(f, topic)) fn(topic, payload.toString(), packet);
  });

  function subscribe(filter, fn, qos = 0) {
    handlers.push([filter, fn, qos]);
    if (client.connected) client.subscribe(filter, { qos });
  }

  /* ---------- AMR commands: MQTT v5 request / response ---------- */
  const pending = {};
  subscribe("amr/+/cmd/result", (topic, text) => {
    const r = JSON.parse(text), p = pending[r.req_id];
    if (p) { clearTimeout(p.timer); delete pending[r.req_id]; p.resolve(r); }
  }, 1);

  function sendCommand(amrId, cmd, timeoutMs = 3000) {
    const req_id = Math.random().toString(36).slice(2, 10);
    return new Promise((resolve, reject) => {
      pending[req_id] = { resolve, timer: setTimeout(() => { delete pending[req_id]; reject(new Error("timeout")); }, timeoutMs) };
      client.publish(`amr/${amrId}/cmd`, JSON.stringify({ cmd, req_id }), {
        qos: 1,
        properties: {
          messageExpiryInterval: 5,                    // don't execute stale commands later
          responseTopic: `amr/${amrId}/cmd/result`,
          userProperties: { sender: "dashboard" },
        },
      });
    });
  }

  /* ---------- alarms (shared state for banner, nav badge, pages) ---------- */
  const alarmListeners = [];
  let active = [], serviceOnline = true;
  const onAlarms = fn => { alarmListeners.push(fn); fn(active); };

  function renderBanner() {
    const crit = active.filter(a => a.severity === "critical");
    const banner = $("alarmBanner"), count = $("navCount");
    count.textContent = active.length || "";
    count.classList.toggle("warn", !crit.length);
    if (!serviceOnline) {
      banner.className = "show warning";
      $("bannerMsg").textContent = "⚠ 異常偵測服務（alarmd）離線，目前不會偵測與記錄異常";
      return;
    }
    if (!active.length) { banner.className = ""; return; }
    const top = crit[0] || active[0];
    banner.className = "show " + (crit.length ? "critical" : "warning");
    $("bannerMsg").textContent = `${active.length} 個異常進行中` +
      (crit.length ? `（${crit.length} 個嚴重）` : "") + `：${top.message}` +
      (active.length > 1 ? ` …等 ${active.length} 項` : "");
  }

  subscribe("alarms/active", (t, text) => {
    active = JSON.parse(text).alarms;
    renderBanner();
    alarmListeners.forEach(fn => fn(active));
  }, 1);
  subscribe("alarms/service/online", (t, text) => { serviceOnline = text === "online"; renderBanner(); }, 1);
  subscribe("alarms/event", (t, text) => {
    const { action, alarm } = JSON.parse(text);
    if (action !== "raised") return;
    const sev = SEVERITY[alarm.severity] || SEVERITY.warning;
    toast(`⚠ ${alarm.message}`, sev.c, 6000);
    if (sound && alarm.severity === "critical") beep();
  }, 1);

  function ack(id) {
    client.publish("alarms/ack", JSON.stringify(id === "all" ? { all: true, by: "dashboard" } : { id, by: "dashboard" }), { qos: 1 });
  }

  return { $, client, subscribe, sendCommand, ack, onAlarms, toast, esc,
           fmtTime, fmtDateTime, fmtDuration, SEVERITY, typeLabel, activeAlarms: () => active };
})();
