/* 同济场馆空位提醒 · 多场馆只读版 */
const S = {
  config: null,
  venues: [],
  draft: [],
  status: null,
  avail: null,
  availVenueId: "",
  selected: null,
  dirty: false,
  venueSig: "",
  lastAlertId: null,
};

const SLOTS = Array.from({ length: 15 }, (_, i) => {
  const h = 7 + i;
  return `${String(h).padStart(2, "0")}:00-${String((h + 1) % 24).padStart(2, "0")}:00`;
});

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(path, method = "GET", body = null) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== null) opt.body = JSON.stringify(body);
  try {
    const r = await fetch(path, opt);
    let data;
    try { data = await r.json(); } catch (e) { data = { ok: false, msg: "响应解析失败" }; }
    return data;
  } catch (e) {
    return { ok: false, msg: "网络错误：" + e.message };
  }
}

function $(id) { return document.getElementById(id); }
function val(id) { const el = $(id); return el ? el.value.trim() : ""; }
function checked(id) { const el = $(id); return !!(el && el.checked); }
function setVal(id, v) { const el = $(id); if (el) el.value = v == null ? "" : v; }
function setChecked(id, v) { const el = $(id); if (el) el.checked = !!v; }
function deepCopy(x) { return JSON.parse(JSON.stringify(x)); }

function beep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 880;
    gain.gain.value = 0.06;
    osc.start();
    setTimeout(() => { try { osc.stop(); ctx.close(); } catch (e) {} }, 200);
  } catch (e) { /* 忽略 */ }
}

function toast(msg, type = "info") {
  let box = $("toastBox");
  if (!box) {
    box = document.createElement("div");
    box.id = "toastBox";
    box.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:99;display:flex;" +
      "flex-direction:column;gap:8px;max-width:360px";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  const colors = {
    info: ["#e8f0fe", "#1f6feb"], ok: ["#e6f4ea", "#1a7f37"],
    warn: ["#fff4d6", "#9a6700"], bad: ["#fdecea", "#b42318"],
  };
  const [bg, fg] = colors[type] || colors.info;
  el.style.cssText = `background:${bg};color:${fg};padding:9px 12px;border-radius:8px;` +
    "box-shadow:0 4px 16px rgba(0,0,0,.12);font-size:13px";
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* ---------------- 状态 ---------------- */
async function loadState(first = false) {
  const r = await api("/api/state");
  if (!r.ok) { toast(r.msg || "状态读取失败", "bad"); return; }
  S.status = r.status;
  S.config = r.config;
  S.venues = r.venues || [];

  const aid = Number(r.last_alert_id || 0);
  if (S.lastAlertId === null) S.lastAlertId = aid;
  else if (aid > S.lastAlertId) {
    S.lastAlertId = aid;
    if ((S.config.system || {}).local_alert) beep();
  }

  if (first) {
    fillForms();
    S.draft = deepCopy(S.venues);
    S.venueSig = JSON.stringify(sigOf(S.venues));
    renderVenueManager();
    renderTracked();
    populateSelects();
    loadAutostart();
  } else {
    const sig = JSON.stringify(sigOf(S.venues));
    if (!S.dirty && sig !== S.venueSig) {
      S.draft = deepCopy(S.venues);
      S.venueSig = sig;
      renderVenueManager();
    }
    renderTracked();
    populateSelects();
  }
  renderStatus();
}

function sigOf(venues) {
  return venues.map(v => [v.id, v.enabled, v.interval_min, v.days,
    v.times, v.fields, v.known_fields]);
}

function renderStatus() {
  const st = S.status || {};
  const enabled = S.venues.filter(v => v.enabled);
  $("cTracked").textContent = `${enabled.length} 个`;
  const last = enabled.map(v => v.last_scan).filter(Boolean).sort().pop();
  $("cLastScan").textContent = last || "—";
  const next = enabled.map(v => v.next_scan).filter(Boolean).sort()[0];
  $("cNextScan").textContent = st.paused ? "已暂停" : (next || "—");
  $("cBudget").textContent = `${st.requests_last_hour || 0}/${st.max_requests_per_hour || 60}`;
  $("cHours").textContent = st.active_hours || "07:00~22:00";

  const pill = $("statusPill");
  pill.className = "pill";
  if (st.paused) { pill.textContent = "已暂停（不读取）"; pill.classList.add("warn"); }
  else if (!enabled.length) { pill.textContent = "未选择场馆"; pill.classList.add("warn"); }
  else if (!st.in_active_hours) { pill.textContent = "非运行时段"; pill.classList.add("warn"); }
  else if (st.last_error) { pill.textContent = "异常"; pill.classList.add("bad"); }
  else { pill.textContent = "只读运行中"; pill.classList.add("ok"); }
  $("pauseBtn").textContent = st.paused ? "恢复" : "暂停";
}

/* ---------------- 跟踪卡片 ---------------- */
function renderTracked() {
  const wrap = $("trackedVenues");
  const enabled = S.venues.filter(v => v.enabled);
  if (!enabled.length) {
    wrap.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
      <strong>还没有跟踪任何场馆</strong><br>
      请在下方“场馆管理”里勾选需要的场馆；未勾选的场馆不会发任何请求。
      <div style="margin-top:10px"><button class="btn primary" id="gotoManager">去选择场馆</button></div>
    </div>`;
    const b = $("gotoManager");
    if (b) b.addEventListener("click", () => $("venueManagerPanel").scrollIntoView({ behavior: "smooth" }));
    return;
  }
  wrap.innerHTML = enabled.map(v => {
    const free = v.cells ? `${v.free} 格空闲` : "尚未扫描";
    return `<div class="venue-card">
      <div class="vc-head"><span class="sport-tag">${esc(v.sport)}</span>
        <strong>${esc(v.name)}</strong><span class="muted">${esc(v.campus)}</span></div>
      <div class="vc-stats">${free} · 每 ${v.interval_min} 分钟 · 未来 ${v.days} 天</div>
      <div class="vc-stats">上次 ${esc(v.last_scan || "—")} · 下次 ${esc(v.next_scan || "—")}</div>
      <div class="vc-actions">
        <button class="btn small" data-avail="${esc(v.id)}">查看空位</button>
        <button class="btn small ghost" data-scan="${esc(v.id)}">扫描一次</button>
        <button class="btn small ghost" data-edit="${esc(v.id)}">设置</button>
      </div>
    </div>`;
  }).join("");

  wrap.querySelectorAll("[data-avail]").forEach(b => b.addEventListener("click", () => {
    const id = b.dataset.avail;
    $("availVenue").value = id;
    $("availWrap").scrollIntoView({ behavior: "smooth", block: "center" });
    loadAvailability(id, true);
  }));
  wrap.querySelectorAll("[data-scan]").forEach(b => b.addEventListener("click", () => scanOne(b.dataset.scan)));
  wrap.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    $("venueManagerPanel").scrollIntoView({ behavior: "smooth" });
  }));
}

/* ---------------- 场馆管理 ---------------- */
function renderVenueManager() {
  const wrap = $("venueGroups");
  const groups = {};
  S.draft.forEach(v => { (groups[v.sport] = groups[v.sport] || []).push(v); });
  wrap.innerHTML = Object.keys(groups).map(sport => `
    <div class="sport-group">
      <h3>${esc(sport)}</h3>
      ${groups[sport].map(v => venueRowHtml(v)).join("")}
    </div>`).join("");

  S.draft.forEach(v => {
    const row = wrap.querySelector(`.venue-row[data-id="${v.id}"]`);
    if (!row) return;
    const en = row.querySelector(".v-enabled");
    en.addEventListener("change", e => { v.enabled = e.target.checked; S.dirty = true; });
    const iv = row.querySelector(".v-interval");
    iv.addEventListener("input", e => { v.interval_min = Number(e.target.value || 30); S.dirty = true; });
    const dy = row.querySelector(".v-days");
    dy.addEventListener("change", e => { v.days = Number(e.target.value); S.dirty = true; });
    renderRowChips(v, row);
  });
}

function venueRowHtml(v) {
  const days = Array.from({ length: 7 }, (_, i) => i + 1)
    .map(d => `<option value="${d}" ${Number(v.days) === d ? "selected" : ""}>${d} 天</option>`).join("");
  return `<div class="venue-row ${v.enabled ? "" : "venue-off"}" data-id="${esc(v.id)}">
    <div class="v-head">
      <label class="check"><input type="checkbox" class="v-enabled" ${v.enabled ? "checked" : ""}>
        <strong>${esc(v.name)}</strong></label>
      <span class="sport-tag">${esc(v.campus)}</span>
      <span class="muted">${esc(v.note || "")}</span>
    </div>
    <div class="venue-settings">
      <label>扫描间隔（分钟）<input type="number" class="v-interval" min="15" step="1" value="${Number(v.interval_min) || 30}"></label>
      <label>监控天数<select class="v-days">${days}</select></label>
      <div class="field"><div class="field-label">时段偏好 <span class="muted">不选 = 不限</span></div>
        <div class="chips v-times"></div></div>
      <div class="field"><div class="field-label">场地偏好 <span class="muted">不选 = 不限</span></div>
        <div class="chips v-fields"></div></div>
    </div>
  </div>`;
}

function renderRowChips(v, row) {
  const timesEl = row.querySelector(".v-times");
  const fieldsEl = row.querySelector(".v-fields");
  chipList(timesEl, SLOTS.map(x => ({ value: x, label: x })), v.times, () => {
    S.dirty = true; renderRowChips(v, row);
  });
  const known = v.known_fields || [];
  if (!known.length) {
    fieldsEl.innerHTML = '<span class="muted">首次扫描后获取场地列表</span>';
  } else {
    chipList(fieldsEl, known.map(x => ({ value: x, label: x })), v.fields, () => {
      S.dirty = true; renderRowChips(v, row);
    });
  }
}

function chipList(el, items, selected, onChange) {
  el.innerHTML = "";
  items.forEach(it => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    const idx = selected.indexOf(it.value);
    if (idx >= 0) b.classList.add("on");
    b.appendChild(document.createTextNode(it.label));
    b.addEventListener("click", () => {
      const i = selected.indexOf(it.value);
      if (i >= 0) selected.splice(i, 1); else selected.push(it.value);
      onChange();
    });
    el.appendChild(b);
  });
}

async function saveVenues() {
  const payload = S.draft.map(v => ({
    id: v.id, enabled: !!v.enabled,
    interval_min: Number(v.interval_min) || 30,
    days: Number(v.days) || 7,
    times: v.times || [],
    fields: v.fields || [],
  }));
  const r = await api("/api/venues", "POST", { venues: payload });
  if (!r.ok) { toast((r.errors || [r.msg || "保存失败"]).join("；"), "bad"); return; }
  S.dirty = false;
  toast("场馆设置已保存", "ok");
  await loadState(false);
  S.draft = deepCopy(S.venues);
  S.venueSig = JSON.stringify(sigOf(S.venues));
  renderVenueManager();
  renderTracked();
  populateSelects();
}

/* ---------------- 下拉框 ---------------- */
function populateSelects() {
  const enabled = S.venues.filter(v => v.enabled);
  const opts = enabled.map(v => `<option value="${esc(v.id)}">${esc(v.campus)} · ${esc(v.name)}</option>`).join("");
  ["availVenue"].forEach(id => {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = opts || '<option value="">（无已启用场馆）</option>';
    if (enabled.some(v => v.id === cur)) sel.value = cur;
    else if (enabled.length) sel.value = enabled[0].id;
  });
}

/* ---------------- 表单 ---------------- */
function fillForms() {
  const c = S.config || {};
  const m = c.monitor || {}, n = c.notify || {}, sys = c.system || {};
  setVal("aUser", c.username || "");
  setVal("aPass", "");
  setVal("mStart", m.active_start || "07:00");
  setVal("mEnd", m.active_end || "22:00");
  setVal("mCap", m.max_requests_per_hour || 60);
  setVal("mDedup", m.dedup_minutes || 0);
  setChecked("mAlertQuota", m.alert_quota_full);
  setVal("nChannel", n.channel || "email");
  setVal("nHost", n.smtp_host || "");
  setVal("nPort", n.smtp_port || 465);
  setChecked("nSsl", n.smtp_ssl !== false);
  setVal("nUser", n.smtp_user || "");
  setVal("nPass", "");
  setVal("nFrom", n.mail_from || "");
  setVal("nTo", n.mail_to || "");
  setVal("nWebType", n.webhook_type || "wecom");
  setVal("nWebUrl", n.webhook_url || "");
  setChecked("sSleep", sys.prevent_sleep);
  setChecked("sLocal", sys.local_alert);
}

function collectConfig() {
  const c = deepCopy(S.config);
  c.username = val("aUser");
  c.password = val("aPass");
  c.monitor.active_start = val("mStart") || "07:00";
  c.monitor.active_end = val("mEnd") || "22:00";
  c.monitor.max_requests_per_hour = Number(val("mCap") || 60);
  c.monitor.dedup_minutes = Number(val("mDedup") || 0);
  c.monitor.alert_quota_full = checked("mAlertQuota");
  c.notify.channel = val("nChannel") || "email";
  c.notify.smtp_host = val("nHost");
  c.notify.smtp_port = Number(val("nPort") || 465);
  c.notify.smtp_ssl = checked("nSsl");
  c.notify.smtp_user = val("nUser");
  c.notify.smtp_pass = val("nPass");
  c.notify.mail_from = val("nFrom");
  c.notify.mail_to = val("nTo");
  c.notify.webhook_type = val("nWebType");
  c.notify.webhook_url = val("nWebUrl");
  c.system.prevent_sleep = checked("sSleep");
  c.system.local_alert = checked("sLocal");
  return c;
}

async function saveAll(silent = false) {
  const r = await api("/api/settings", "POST", collectConfig());
  if (!r.ok) { toast((r.errors || [r.msg || "保存失败"]).join("；"), "bad"); return false; }
  S.config = r.config;
  fillForms();
  if (!silent) toast("已保存", "ok");
  return true;
}

/* ---------------- 空位 / 订单 ---------------- */
async function loadAvailability(vid, force = false) {
  const wrap = $("availWrap");
  if (!vid) { wrap.innerHTML = '<div class="empty">请选择已跟踪的场馆</div>'; return; }
  S.availVenueId = vid;
  if (force) wrap.innerHTML = '<div class="empty">正在查询…</div>';
  const r = await api(`/api/availability?venue_id=${encodeURIComponent(vid)}${force ? "&refresh=1" : ""}`);
  if (!r.ok) { wrap.innerHTML = `<div class="empty">${esc(r.msg || "查询失败")}</div>`; return; }
  S.avail = r;
  renderAvail();
}

function cellClass(s) {
  const map = { "0": "free", "-3": "full", "-1": "closed", "-2": "expired" };
  return map[s.status] || "closed";
}

function renderAvail() {
  const wrap = $("availWrap");
  if (!S.avail || !S.avail.dates.length) {
    wrap.innerHTML = '<div class="empty">暂无可显示的日期</div>';
    return;
  }
  const slots = S.avail.slots;
  const html = S.avail.dates.map(d => {
    const head = `<div class="date-head"><span>${esc(d.label)}</span>` +
      `<span class="tag">${d.is_all ? "全部约满/未开放" : "有空位"}</span></div>`;
    if (!d.fields.length) return `<div class="date-block">${head}<div class="empty">该日期暂无明细</div></div>`;
    const header = `<div class="gh">场地</div>` +
      slots.map(s => `<div class="gh" title="${esc(s)}">${esc(s.slice(0, 2))}</div>`).join("");
    const rows = d.fields.map(f => {
      const cells = f.slots.map(s => {
        const cls = cellClass(s) + (s.predicted_closed ? " predicted" : "");
        return `<div class="cell ${cls}" data-date="${esc(d.date)}" data-field="${esc(f.field_id)}"` +
          ` data-fname="${esc(f.field_name)}" data-slot="${s.idx}" data-label="${esc(s.label)}"` +
          ` data-status="${esc(s.status)}" title="${esc(f.field_name + " " + s.label + " " + s.text)}">` +
          `${esc(s.text)}</div>`;
      }).join("");
      return `<div class="gf">${esc(f.field_name)}</div>${cells}`;
    }).join("");
    return `<div class="date-block">${head}<div class="grid">${header}${rows}</div></div>`;
  }).join("");
  wrap.innerHTML = html;
  wrap.querySelectorAll(".cell").forEach(el => el.addEventListener("click", () => {
    wrap.querySelectorAll(".cell.selected").forEach(x => x.classList.remove("selected"));
    el.classList.add("selected");
    S.selected = {
      date: el.dataset.date, field_id: el.dataset.field, field_name: el.dataset.fname,
      slot: Number(el.dataset.slot), label: el.dataset.label, status: el.dataset.status,
      date_label: (S.avail.dates.find(d => d.date === el.dataset.date) || {}).label || el.dataset.date,
    };
    const txt = { "0": "空闲", "-3": "已约满", "-1": "未开放", "-2": "已过期" }[S.selected.status] || S.selected.status;
    $("cellText").textContent = `${S.selected.date_label} ${S.selected.label} ${S.selected.field_name} · ${txt}`;
    $("cellBar").classList.remove("hidden");
  }));
}

function addToVenuePrefs() {
  const s = S.selected;
  if (!s || !S.availVenueId) return;
  const v = S.draft.find(x => x.id === S.availVenueId);
  if (!v) return;
  if (!v.times.includes(s.label)) v.times.push(s.label);
  if (v.known_fields.includes(s.field_name) && !v.fields.includes(s.field_name)) {
    v.fields.push(s.field_name);
  }
  S.dirty = true;
  renderVenueManager();
  $("cellBar").classList.add("hidden");
  toast("已加入该场馆偏好，记得点“保存场馆设置”", "ok");
}

async function scanOne(vid) {
  toast("正在扫描…");
  const r = await api("/api/scan", "POST", { venue_id: vid, full: true });
  if (!r.ok) { toast(r.msg || "扫描失败", "bad"); return; }
  toast(`扫描完成：${r.venue_name || ""} 变化 ${(r.changes || []).length} 个`, "ok");
  await loadState(false);
  if (S.availVenueId === vid) await loadAvailability(vid, true);
  loadLogs();
}

/* ---------------- 日志 / 系统 ---------------- */
async function loadLogs() {
  const r = await api("/api/logs?limit=200");
  if (!r.ok) return;
  $("logs").innerHTML = (r.logs || []).map(l =>
    `<div class="l"><span class="ts">${esc(l.ts)}</span>` +
    `<span class="${esc(l.level)}">[${esc(l.level)}]</span> ${esc(l.msg)}</div>`).join("")
    || '<div class="l">暂无日志</div>';
  $("logs").scrollTop = $("logs").scrollHeight;
}

async function loadAutostart() {
  const r = await api("/api/autostart");
  if (r.ok) setChecked("sAuto", r.enabled);
}

async function setAutostart(enabled) {
  const r = await api("/api/autostart", "POST", { enabled });
  if (!r.ok) { toast(r.msg || "设置失败", "bad"); return; }
  toast(enabled ? "已设置开机自启" : "已取消开机自启", "ok");
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach(p => p.classList.toggle("active", p.dataset.pane === name));
}

/* ---------------- 事件 ---------------- */
function bind() {
  const notice = $("releaseNotice");
  if (notice) {
    try {
      if (localStorage.getItem("releaseNoticeClosed") === "1") notice.classList.add("hidden");
    } catch (e) { /* 忽略 */ }
    const close = $("closeReleaseNotice");
    if (close) close.addEventListener("click", () => {
      notice.classList.add("hidden");
      try { localStorage.setItem("releaseNoticeClosed", "1"); } catch (e) { /* 忽略 */ }
    });
  }

  $("tabs").addEventListener("click", e => {
    const t = e.target.closest(".tab");
    if (t) switchTab(t.dataset.tab);
  });

  $("pauseBtn").addEventListener("click", async () => {
    const paused = !(S.status && S.status.paused);
    const r = await api("/api/pause", "POST", { paused });
    if (r.ok) {
      await loadState(false);
      toast(paused ? "已暂停：不会访问学校服务器" : "已恢复读取", "ok");
      if (paused) $("availWrap").innerHTML = '<div class="empty">已暂停，不会读取学校服务器</div>';
    }
  });

  $("saveVenues").addEventListener("click", saveVenues);
  $("saveMonitor").addEventListener("click", () => saveAll());
  $("saveEmail").addEventListener("click", () => saveAll());
  $("saveAccount").addEventListener("click", () => saveAll());
  $("saveSystem").addEventListener("click", async () => {
    const ok = await saveAll(true);
    if (ok) await setAutostart(checked("sAuto"));
  });

  $("refreshAvail").addEventListener("click", () => loadAvailability($("availVenue").value, true));
  $("availVenue").addEventListener("change", e => loadAvailability(e.target.value, true));
  $("cellPrefer").addEventListener("click", addToVenuePrefs);
  $("cellClose").addEventListener("click", () => $("cellBar").classList.add("hidden"));

  $("authHelpBtn").addEventListener("click", () => $("authHelpModal").classList.remove("hidden"));
  $("authHelpClose").addEventListener("click", () => $("authHelpModal").classList.add("hidden"));
  $("authHelpModal").addEventListener("click", e => {
    if (e.target === $("authHelpModal")) $("authHelpModal").classList.add("hidden");
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") $("authHelpModal").classList.add("hidden");
  });

  $("testEmail").addEventListener("click", async () => {
    const saved = await saveAll(true);
    if (!saved) return;
    toast("正在发送测试邮件…");
    const r = await api("/api/test-email", "POST", {});
    const email = (r.result || {}).email;
    if (r.ok) toast("测试邮件已发送，请查收（含垃圾箱）", "ok");
    else if (email === "未配置") toast("邮箱未配置：请填写 QQ 邮箱授权码（不是登录密码），保存后再测试", "bad");
    else toast("发送失败：" + (email || r.msg || "未知错误"), "bad");
  });
  $("openLogs").addEventListener("click", loadLogs);
}

async function init() {
  bind();
  await loadState(true);
  await loadLogs();
  setInterval(() => loadState(false), 5000);
  setInterval(() => loadLogs(), 8000);
}

document.addEventListener("DOMContentLoaded", init);
