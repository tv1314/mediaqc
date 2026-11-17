'use strict';

const S = {
  flag: 'all', q: '', sort: 'total', qfilter: 'all', verdict: 'all',
  page: 1, size: 100, rows: [], total: 0, dp: '',
  sel: new Map(), roots: [], cfg: {}, stats: null,
  pick: { mode: 'roots', path: '' },
  pv: { path: '', mode: 'remux', fallbackTried: false },
};
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const PIN = () => localStorage.getItem('mqc_pin') || '';

/* 统一网关下页面位于 /app/mediaqc/，所有请求都要带这个前缀 */
const BASE = (() => {
  let p = location.pathname;
  if (!p.endsWith('/')) p += '/';
  return p.replace(/index\.html$/, '');
})();
const URL_ = (rel) => (rel.startsWith('/') ? BASE + rel.slice(1) : BASE + rel);

function showLogin(msg) {
  const m = $('#loginMask');
  if (!m) return;
  $('#loginMsg').textContent = msg || '未登录飞牛，请先登录 fnOS 后再打开本应用';
  m.hidden = false;
}
function hideLogin() {
  const m = $('#loginMask');
  if (m) m.hidden = true;
}

const fmtSize = (b) => {
  b = b || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 && i > 0 ? 2 : (i ? 1 : 0)) + ' ' + u[i];
};
const fmtDur = (s) => {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
           : `${m}:${String(ss).padStart(2, '0')}`;
};
const qs = (o) => {
  const src = Object.assign({ pin: PIN() }, o || {});
  const p = new URLSearchParams();
  Object.keys(src).forEach((k) => {
    const v = src[k];
    if (v === undefined || v === null || v === '') return;
    p.append(k, v);
  });
  return p.toString();
};
function toast(msg, ms) {
  const d = document.createElement('div');
  d.className = 'toast';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), ms || 2600);
}
const openM = (id) => { $(id).hidden = false; };
const closeM = (id) => { $(id).hidden = true; };
$$('[data-close]').forEach((b) => b.addEventListener('click', (e) => {
  const m = e.target.closest('.modal');
  if (m) m.hidden = true;
  if (m && m.id === 'mPreview') stopVideo();
}));

async function api(path, opts) {
  const o = Object.assign({ headers: { 'Content-Type': 'application/json', 'X-Pin': PIN() } }, opts || {});
  const r = await fetch(URL_(path), o);
  let j;
  try { j = await r.json(); } catch (e) { j = { ok: false, error: 'HTTP ' + r.status }; }
  if (r.status === 401 && j.need_login) {
    showLogin(j.error);
    throw new Error(j.error || '未登录');
  }
  if (r.status === 401) {
    const v = prompt('请输入访问 PIN');
    if (v) { localStorage.setItem('mqc_pin', v); return api(path, opts); }
    throw new Error('需要访问 PIN');
  }
  if (!j.ok) throw new Error(j.error || ('HTTP ' + r.status));
  hideLogin();
  return j;
}

/* ---------------- 目录 ---------------- */
function renderRoots() {
  const bar = $('#rootsBar');
  if (!S.roots.length) {
    bar.innerHTML = '<span class="hint">还没有扫描目录，点「添加目录」选择（例如 /vol00/... 或 /vol1/1000/电影）。</span>'
      + '<button class="btn small" id="btnAddRoot">添加目录</button>';
  } else {
    bar.innerHTML = S.roots.map((r) => `<span class="rootchip" title="${r}">${r.split('/').filter(Boolean).pop() || r}<b data-root="${r}" title="移除">×</b></span>`).join('')
      + '<button class="btn small ghost" id="btnAddRoot">添加目录</button>';
  }
  $('#btnAddRoot').onclick = () => openPick('roots');
  $$('#rootsBar b[data-root]').forEach((b) => b.addEventListener('click', () => removeRoot(b.dataset.root)));
  $('#rootInfo').textContent = S.roots.length ? `${S.roots.length} 个扫描目录` : '未选择扫描目录';
}
function renderDp() {
  const box = $('#dpBox');
  if (!S.dp) { box.innerHTML = ''; return; }
  const short = S.dp.split('/').filter(Boolean).slice(-2).join('/');
  box.innerHTML = `<span class="rootchip" title="${S.dp}">仅看：${short}<b id="dpClear">×</b></span>`;
  $('#dpClear').onclick = () => { S.dp = ''; S.page = 1; renderDp(); loadList(); };
}
function normRoots(list) {
  const uniq = [...new Set(list.map((r) => (r || '').replace(/\/+$/, '') || '/'))].sort((a, b) => a.length - b.length);
  const out = [];
  uniq.forEach((r) => { if (!out.some((c) => r === c || r.startsWith(c.replace(/\/+$/, '') + '/'))) out.push(r); });
  return out;
}
async function saveRoots(list, tip) {
  const j = await api('/api/config?' + qs(), { method: 'POST', body: JSON.stringify({ roots: normRoots(list) }) });
  S.roots = j.config.roots;
  renderRoots();
  await refreshAll();
  toast((tip || '已更新目录') + (j.pruned ? `，清理 ${j.pruned} 条记录` : ''));
}
/* 应用内确认框：飞牛窗口是 iframe，原生 confirm() 会被浏览器直接拦掉（点了没反应） */
function askConfirm(title, body, okText) {
  return new Promise((resolve) => {
    $('#cfTitle').textContent = title || '确认';
    $('#cfBody').textContent = body || '';
    const ok = $('#cfOk');
    ok.textContent = okText || '确认执行';
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      ok.onclick = null;
      closeM('#mConfirm');
      resolve(v);
    };
    ok.onclick = () => done(true);
    $$('#mConfirm [data-close]').forEach((b) => { b.onclick = () => done(false); });
    openM('#mConfirm');
  });
}
async function removeRoot(path) {
  const short = path.split('/').filter(Boolean).pop() || path;
  const ok = await askConfirm('移除扫描目录',
    `从扫描目录移除「${short}」？该目录下已有扫描记录会一并清除（磁盘文件不会删除）。`, '确认移除');
  if (!ok) return;
  saveRoots(S.roots.filter((r) => r !== path), '已移除');
}

/* ---------------- 目录选择器 ---------------- */
function openPick(mode) {
  S.pick = { mode, path: '' };
  $('#pickTitle').textContent = mode === 'move' ? '选择移动目标目录' : '选择要扫描的目录';
  $('#pickOk').textContent = mode === 'move' ? '移动到此目录' : '加入扫描目录';
  openM('#mPick');
  browse('');
}
async function browse(path) {
  const j = await api('/api/browse?' + qs({ path: path || '' }));
  S.pick.path = j.path || '';
  $('#pickPath').textContent = j.path || '/（根目录）';
  const list = $('#pickList');
  list.innerHTML = j.dirs.length
    ? j.dirs.map((d) => `<li data-p="${d.path}"><span>${d.name}</span><span class="arrow">›</span></li>`).join('')
    : '<li class="hint">（没有子目录）</li>';
  $$('#pickList li[data-p]').forEach((li) => li.addEventListener('click', () => browse(li.dataset.p)));
  $('#pickUp').onclick = () => browse(j.up || '');
  if (j.skipped) toast(`已隐藏 ${j.skipped} 个无权访问的目录`);
  else if (j.hidden_sys) toast(`已隐藏 ${j.hidden_sys} 个系统目录`);
}
async function pickOk() {
  const p = S.pick.path;
  if (!p) { toast('请先进入一个子目录'); return; }
  if (S.pick.mode === 'move') {
    closeM('#mPick');
    await doAct('move', { target: p });
  } else {
    if (S.roots.includes(p)) { toast('该目录已在列表中'); return; }
    closeM('#mPick');
    await saveRoots(S.roots.concat([p]), '已添加目录');
  }
}

/* ---------------- 列表 ---------------- */
async function loadList() {
  const j = await api('/api/files?' + qs({
    flag: S.flag, q: S.q, sort: S.sort, page: S.page, size: S.size,
    qfilter: S.qfilter, verdict: S.verdict, dirprefix: S.dp || undefined,
  }));
  S.rows = j.rows; S.total = j.total;
  const pages = Math.max(1, Math.ceil(S.total / S.size));
  if (S.page > pages) { S.page = pages; return loadList(); }
  renderTable();
}
const Q_LABEL = {
  f: ['模糊', 'bad'], s: ['略软', 'warn'], p: ['假高清', 'bad'], k: ['块效应', 'warn'],
  d: ['过暗', 'warn'], c: ['对比低', 'warn'], u: ['4K 保底', 'ok'],
};
/* 结论只分两档：差（清理/换源）/ 优秀（可保留/优质） */
const V_LABEL = { clean: ['差', 'bad'], replace: ['差', 'bad'], keep: ['优秀', 'ok'], good: ['优秀', 'ok'],
                  bad: ['差', 'bad'], ok: ['优秀', 'ok'] };
/* 分辨率档标签：每个文件都会显示，避免"4K 片没标签" */
function resTag(r) {
  const sh = Math.min(r.width || 0, r.height || 0);
  const ls = Math.max(r.width || 0, r.height || 0);
  if (!ls) return { t: '', cls: '' };
  if (ls >= 3400 || sh >= 2000) return { t: '4K', cls: 'blue' };
  if (ls >= 2400) return { t: '1440p', cls: 'ok' };
  if (ls >= 1800 || sh >= 1000) return { t: '1080p', cls: 'ok' };
  if (ls >= 1200 || sh >= 700) return { t: '720p', cls: 'warn' };
  return { t: 'SD', cls: 'bad' };
}
function resBadge(r) {
  const x = resTag(r);
  if (!x.t) return '';
  return `<span class="badge b-${x.cls}" title="分辨率档位（按长边/短边判定）">${x.t}</span>`;
}
function flagsBadges(r) {
  const map = { A: ['低分辨率', 'bad'], B: ['低码率', 'warn'], C: ['老编码', 'warn'], D: ['疑似低质', 'bad'], E: ['同片冗余', 'blue'] };
  let out = '';
  (r.flags || '').split('').forEach((c) => {
    if (map[c]) out += `<span class="badge b-${map[c][1]}" title="${((r.reason || '').replace(/"/g, ''))}">${c} ${map[c][0]}</span>`;
  });
  return out;
}
function qBadges(r) {
  if (r.q_checked == null) return '<span class="badge">未体检</span>';
  if (!r.q_flags) return '<span class="badge b-ok">正常</span>';
  let detail = '';
  try { detail = (JSON.parse(r.q_detail || '{}').why || '').replace(/"/g, ''); } catch (e) { detail = ''; }
  return r.q_flags.split('').map((c) => (Q_LABEL[c] ? `<span class="badge b-${Q_LABEL[c][1]}" title="${detail}">${Q_LABEL[c][0]}</span>` : '')).join('');
}
function scoreCell(v, empty) {
  if (v == null) return `<span class="hint">${empty || '—'}</span>`;
  const cls = v < 60 ? 'low' : (v < 80 ? 'mid' : '');
  return `<span class="score ${cls}">${v}</span>`;
}
function renderTable() {
  const tb = $('#tbody');
  tb.innerHTML = S.rows.map((r) => {
    const vb = r.vbitrate || r.bitrate || 0;
    return `<tr>
      <td class="c-chk"><input type="checkbox" data-id="${r.id}" ${S.sel.has(r.id) ? 'checked' : ''}></td>
      <td class="name" title="${r.path}">${r.name}
        <span class="p" data-dir="${r.dir}" title="点击：只看此目录">${r.dir}</span></td>
      <td>${r.width}×${r.height}</td>
      <td title="容器总计 ${(r.bitrate / 1e6).toFixed(2)} / 音轨 ${(((r.abitrate || 0)) / 1e6).toFixed(2)} Mbps">${(vb / 1e6).toFixed(2)} Mbps</td>
      <td>${r.vcodec}${(r.pix_fmt || '').includes('10') ? ' 10bit' : ''}</td>
      <td>${fmtDur(r.duration)}</td>
      <td>${fmtSize(r.size)}</td>

      <td>${scoreCell(r.total_score == null ? r.score : r.total_score)}</td>
      <td>${V_LABEL[r.verdict] ? `<span class="badge b-${V_LABEL[r.verdict][1]}">${V_LABEL[r.verdict][0]}</span>` : '<span class="badge">—</span>'}</td>
      <td>${resBadge(r)}${flagsBadges(r)}${qBadges(r)}</td>
      <td class="c-act">
        <button class="btn ghost small" data-pv="${r.id}">预览</button>
        <button class="btn ghost small" data-qi="${r.id}" title="只重新体检这一个文件（刷新清晰度/块效应数据）">重检</button>
      </td>
    </tr>`;
  }).join('');
  $('#empty').style.display = S.rows.length ? 'none' : 'block';
  if (!S.rows.length) {
    $('#empty').innerHTML = S.roots.length
      ? `<div>没有数据可显示</div>
         <div class="hint">可能原因：① 还没扫描 ② 目录里没有视频 ③ 筛选太窄（当前：${S.flag === 'all' ? '全部档' : S.flag + ' 档'}${S.verdict !== 'all' ? ' · 结论=' + ({ bad: '差', ok: '优秀' }[S.verdict] || S.verdict) : ''}${S.qfilter !== 'all' ? ' · 体检=' + S.qfilter : ''}${S.q ? ' · 搜索“' + S.q + '”' : ''}）。</div>
         <button class="btn primary" id="emptyScan">扫描</button>
         <button class="btn ghost" id="emptyAdd">改选目录</button>`
      : `<div>还没有扫描数据</div><div class="hint">先选择扫描目录，再点「扫描+体检」。</div><button class="btn primary" id="emptyAdd">选择目录</button>`;
    if ($('#emptyScan')) $('#emptyScan').onclick = () => startScan();
    if ($('#emptyAdd')) $('#emptyAdd').onclick = () => openPick('roots');
  }
  $$('#tbody input[type=checkbox]').forEach((c) => c.addEventListener('change', (e) => {
    const id = +e.target.dataset.id;
    const row = S.rows.find((x) => x.id === id);
    if (e.target.checked) S.sel.set(id, row ? row.size : 0); else S.sel.delete(id);
    syncChkAll();
    updateSel();
  }));
  $$('#tbody [data-pv]').forEach((b) => b.addEventListener('click', () => openPreview(+b.dataset.pv)));
  $$('#tbody [data-qi]').forEach((b) => b.addEventListener('click', () => reinspectOne(+b.dataset.qi, b)));
  $$('#tbody .p[data-dir]').forEach((s) => s.addEventListener('click', () => {
    S.dp = s.dataset.dir;
    S.page = 1;
    renderDp();
    loadList();
  }));
  renderDp();
  const pages = Math.max(1, Math.ceil(S.total / S.size));
  $('#pageInfo').textContent = `${S.page} / ${pages}（共 ${S.total} 条）`;
  $('#prev').disabled = S.page <= 1;
  $('#next').disabled = S.page >= pages;
  syncChkAll();
}
/* 单文件重检：只刷新这一条，完成后自动重载列表 */
async function reinspectOne(id, btn) {
  if (!id) return;
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '重检中…'; }
  try {
    await api('/api/inspect?' + qs(), { method: 'POST', body: JSON.stringify({ ids: [id], force: true }) });
  } catch (e) {
    toast('重检启动失败：' + e.message, 4000);
    if (btn) { btn.disabled = false; btn.textContent = old; }
    return;
  }
  $('#progress').hidden = false;
  if (!poll) poll = setInterval(pollJob, 700);
  const ok = await waitJobIdle();
  if (btn) { btn.disabled = false; btn.textContent = old; }
  if (ok) { toast('重检完成'); await refreshAll(); }
  else { toast('重检未完成（可能被停止）', 4000); }
}
function waitJobIdle(limitMs = 300000) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const iv = setInterval(async () => {
      let j = null;
      try { j = (await api('/api/job?' + qs())).job; } catch (e) { /* ignore */ }
      if ((!j || j.state !== 'scanning') && Date.now() - t0 > 1200) {
        clearInterval(iv);
        resolve(true);
      } else if (Date.now() - t0 > limitMs) {
        clearInterval(iv);
        resolve(false);
      }
    }, 800);
  });
}
function syncChkAll() {
  const c = $('#chkAll');
  const ids = S.rows.map((r) => r.id);
  const n = ids.filter((id) => S.sel.has(id)).length;
  c.checked = ids.length > 0 && n === ids.length;
  c.indeterminate = n > 0 && n < ids.length;
}
function updateSel() {
  let bytes = 0;
  S.sel.forEach((v) => { bytes += v || 0; });
  $('#selCount').textContent = S.sel.size;
  $('#selSize').textContent = fmtSize(bytes);
  $('#selHint').textContent = S.sel.size > S.rows.filter((r) => S.sel.has(r.id)).length ? '（含其他页）' : '';
}

/* ---------------- 统计 ---------------- */
async function loadStats() {
  const j = await api('/api/stats?' + qs());
  S.stats = j;
  const sum = (keys) => {
    const acc = { count: 0, size: 0 };
    keys.forEach((k) => {
      const v = (j.by_verdict || {})[k] || { count: 0, size: 0 };
      acc.count += v.count;
      acc.size += v.size;
    });
    return acc;
  };
  const bad = sum(['bad', 'clean', 'replace']);
  const ok = sum(['ok', 'keep', 'good']);
  $('#stats').innerHTML =
    `<div class="stat"><div class="k">已扫描</div><div class="v">${j.count} 个 · ${fmtSize(j.size)}</div></div>`
    + `<div class="stat hi-red"><div class="k">差</div><div class="v">${bad.count} 个 · ${fmtSize(bad.size)}</div></div>`
    + `<div class="stat hi"><div class="k">优秀</div><div class="v">${ok.count} 个 · ${fmtSize(ok.size)}</div></div>`
    + `<div class="stat"><div class="k">平均综合评分</div><div class="v">${j.avg_total == null ? '—' : j.avg_total}</div></div>`
    + (j.q_todo ? `<div class="stat"><div class="k">未体检</div><div class="v">${j.q_todo} 个</div></div>` : '');
}

/* ---------------- 扫描 / 体检 ---------------- */
let poll = null;
function fmtEta(sec) {
  if (sec == null) return '';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ' 分' + (s ? ` ${s} 秒` : '');
}
function renderJob(j, last) {
  const busy = j.state === 'scanning';
  $('#btnScan').disabled = busy;
  $('#btnInspect').disabled = busy;
  $('#btnStop').disabled = !busy;
  if (busy) {
    if (j.phase === 'enum') {
      $('#pFill').style.width = '4%';
      $('#pText').textContent = `① 正在枚举文件…已发现 ${j.found || 0} 个视频（机械盘读目录可能较慢）`;
    } else {
      const pct = j.total ? Math.round((j.done * 100) / j.total) : 50;
      $('#pFill').style.width = Math.max(4, pct) + '%';
      const eta = j.eta != null ? ` · 预计剩余 ${fmtEta(j.eta)}` : '';
      const rate = j.per_sec ? ` · ${j.per_sec} 个/秒` : '';
      const cur = j.cur ? ` · 当前：${j.cur}` : '';
      let label;
      if (j.phase === 'inspect') label = '③ 画面体检（抽帧）';
      else label = (S.cfg.inspect_on_scan ? '② 探测+体检' : '② 探测元数据');
      $('#pText').textContent = `${label} ${j.done}/${j.total}（${pct}%）${eta}${rate}${cur}`;
    }
    return;
  }
  if (j.msg) {
    $('#pFill').style.width = j.state === 'stopped' ? '0%' : '100%';
    $('#pText').textContent = j.msg;
    return;
  }
  if (last) {
    const t = new Date(last.ts * 1000).toLocaleString('zh-CN', { hour12: false });
    $('#pFill').style.width = '100%';
    $('#pText').textContent = `上次扫描 ${t}：发现 ${last.found} 个，新录入 ${last.new}，更新 ${last.updated}，用时 ${last.seconds} 秒`;
    return;
  }
  $('#pFill').style.width = '0%';
  $('#pText').textContent = S.roots.length
    ? (S.cfg.inspect_on_scan ? '尚未扫描：点「扫描+体检」一次性完成参数探测与画面抽帧（约 1 秒/文件）'
                             : '尚未扫描：点「扫描」只做参数探测（设置里可开启画面体检）')
    : '尚未扫描，先选择扫描目录';
}
async function startScan() {
  if (!S.roots.length) { openPick('roots'); return; }
  try {
    await api('/api/scan?' + qs(), { method: 'POST', body: JSON.stringify({ roots: S.roots }) });
  } catch (e) { toast('无法开始扫描：' + e.message, 5000); return; }
  $('#progress').hidden = false;
  if (!poll) poll = setInterval(pollJob, 700);
  await pollJob();
}
async function startInspect() {
  if (!S.stats || !S.stats.count) { toast('先扫描出文件再体检'); return; }
  const todo = S.stats.q_todo || 0;
  let force = false;
  if (!todo) {
    force = await askConfirm('重新体检',
      '所有文件都已体检过。要重新抽帧体检全部吗？（较慢，但会刷新旧的测量数据）', '重新体检全部');
    if (!force) return;
  } else {
    force = await askConfirm('体检',
      `有 ${todo} 个文件还没体检。点「全部重检」会连同已体检的一起重做；点「取消」只体检未体检的。`,
      '全部重检');
  }
  try {
    await api('/api/inspect?' + qs(), { method: 'POST', body: JSON.stringify({ force }) });
  } catch (e) { toast('体检启动失败：' + e.message, 5000); return; }
  $('#progress').hidden = false;
  toast(force ? '开始重新体检全部' : `开始体检 ${todo} 个未体检文件`);
  if (!poll) poll = setInterval(pollJob, 700);
  await pollJob();
}
async function pollJob() {
  let res;
  try { res = await api('/api/job?' + qs()); } catch (e) { return; }
  renderJob(res.job, res.last_scan);
  if (res.job.state !== 'scanning') {
    if (poll) { clearInterval(poll); poll = null; }
    S.sel.clear();
    updateSel();
    await refreshAll();
  }
}

/* ---------------- 预览 ---------------- */
const PV_MODES = ['direct', 'remux', 'transcode'];
const PV_NAME = { direct: '原文件直出', remux: '转封装', transcode: '硬件转码' };
const ICON_PLAY = '<svg viewBox="0 0 16 16"><path d="M5 3.2v9.6L13 8z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 16 16"><rect x="4.5" y="3" width="2.6" height="10" rx="1"/><rect x="9" y="3" width="2.6" height="10" rx="1"/></svg>';
function pvTimeHtml(cur, total) {
  return `<b>${fmtDur(cur)}</b><i>/ ${fmtDur(total)}</i>`;
}
function pvBtn(mode) {
  return $('#' + { direct: 'pvDirect', remux: 'pvRemux', transcode: 'pvTrans' }[mode]);
}
/* 只记两类信息：① 该格式直出确实成功过（ok）② 直出连续失败次数（fails）
   注意：绝不要把"降级后的转码"记成默认，否则同类文件会被永久误判 */
const PLAN_PREFIX = 'mqc_plan2_';
function planKey(r) {
  const ext = ((r.name || '').split('.').pop() || '').toLowerCase();
  const px = (r.pix_fmt || '').toLowerCase();
  const ten = (px.includes('10') || px.includes('12')) ? '10' : '8';
  return `${PLAN_PREFIX}${ext}_${(r.vcodec || 'x').toLowerCase()}_${ten}_${(r.acodec || 'x').toLowerCase()}`;
}
function planState(r) {
  try { return JSON.parse(localStorage.getItem(planKey(r)) || '{}') || {}; } catch (e) { return {}; }
}
function setPlanState(r, patch) {
  try { localStorage.setItem(planKey(r), JSON.stringify(Object.assign(planState(r), patch))); } catch (e) {}
}
function resetPlanHints() {
  try {
    Object.keys(localStorage).forEach((k) => { if (k.indexOf('mqc_plan') === 0) localStorage.removeItem(k); });
  } catch (e) {}
}
/* 直出失败计数 +1（用于"这类格式直出确实不行"的判断） */
function markDirectFail(r) {
  const st = planState(r);
  setPlanState(r, { ok: '', fails: (st.fails || 0) + 1 });
}
/* 按容器/编码/音频自动挑选播放方式 */
function planFor(r) {
  const ext = ((r.name || '').split('.').pop() || '').toLowerCase();
  const v = (r.vcodec || '').toLowerCase();
  const px = (r.pix_fmt || '').toLowerCase();
  const a = (r.acodec || '').toLowerCase();
  const ten = px.includes('10') || px.includes('12');
  const st = planState(r);
  const okBox = ['mp4', 'm4v', 'webm', 'mov', 'mkv'].includes(ext);
  const okV = ['h264', 'hevc', 'av1', 'vp9', 'vp8'].includes(v);
  // 只保留浏览器真的能解码的音轨：AC3/EAC3 在 Chrome/Edge 里通常静音，
  // 所以它们要走「转封装 → 音轨转 AAC」，否则就是"有画面没声音"
  const okA = ['aac', 'mp3', 'opus', 'vorbis'].includes(a);

  if (st.ok) return { mode: st.ok, why: `这种格式上次用「${PV_NAME[st.ok]}」成功播放过` };
  // 判断画质只用画面：音轨能不能解不重要，一律优先直出（无声也照播）
  if (okBox && okV) {
    const silent = okA ? '' : `（音轨 ${a || '?'} 浏览器可能不出声，只影响声音）`;
    return { mode: 'direct', why: `${ext.toUpperCase()} + ${v}${ten ? ' 10bit' : ''} + ${a}，原文件直出${silent}` };
  }
  if (okV && (!st.fails || st.fails < 2)) {
    // 画面能直通：只重封装 + 音轨转 AAC（10bit 也先试，失败再转码）
    return { mode: 'remux', why: `${ext.toUpperCase()} 容器或音轨(${a || '?'})浏览器不认 → 转封装，画面不重编码` };
  }
  return { mode: 'transcode', why: `${ten ? '10bit ' : ''}${v || '未知编码'} + ${a || '?'} 无法直通 → 硬件转码 720p` };
}
async function openPreview(id) {
  const r = S.rows.find((x) => x.id === id);
  if (!r) return;
  const plan = planFor(r);
  S.pv = { path: r.path, mode: plan.mode, directUrl: '', dur: r.duration || 0, offset: 0, r };
  $('#pvTitle').textContent = `${r.name}　${r.width}×${r.height}　${((r.vbitrate || r.bitrate) / 1e6).toFixed(1)}Mbps　${r.vcodec}`
    + (r.q_score != null ? `　画面分 ${r.q_score}` : '');
  $('#pvShots').innerHTML = '';
  if (r.width && r.height) {
    $('#pvBox').style.aspectRatio = `${r.width} / ${r.height}`;
  } else {
    $('#pvBox').style.aspectRatio = '16 / 9';
  }
  $('#pvSeek').max = Math.max(1, Math.round(S.pv.dur));
  $('#pvSeek').value = 0;
  $('#pvLoad').hidden = true;
  $('#pvTime').innerHTML = pvTimeHtml(0, S.pv.dur);
  $('#pvHint').textContent = `已自动选择「${PV_NAME[plan.mode]}」：${plan.why}`;
  openM('#mPreview');
  try {
    const j = await api('/api/direct?' + qs({ path: r.path }));
    S.pv.directUrl = URL_(j.url);
    $('#pvDownload').href = URL_(j.url);
  } catch (e) {
    S.pv.directUrl = '';
    $('#pvDownload').removeAttribute('href');
  }
  setPvMode(plan.mode, true);
}
let pvWatch = null;
function clearWatch() { if (pvWatch) { clearTimeout(pvWatch); pvWatch = null; } }
function startWatch() {
  clearWatch();
  if (S.pv.mode !== 'direct') return;
  pvWatch = setTimeout(() => {
    const v = $('#pvVideo');
    if ((v.currentTime || 0) < 0.05 && v.readyState < 1) pvFail('直出 8 秒内没有画面');
  }, 8000);
}
function bumpWatch() {          // 有任何进展就重新计时，避免慢盘被误判
  if (S.pv.mode === 'direct' && pvWatch) {
    clearWatch();
    pvWatch = setTimeout(() => {
      const v = $('#pvVideo');
      if ((v.currentTime || 0) < 0.05 && v.readyState < 1) pvFail('直出 8 秒内没有画面');
    }, 8000);
  }
}
/* 只看画面，不看声音：直出即使无声（TrueHD/DTS/AC3 浏览器不支持）也不做任何切换，
   想看声音时手动点「转封装」即可 */
function watchAudio() {
  /* 故意留空：不再因为"没声音"自动切转封装 */
}
function pvFail(reason) {
  clearWatch();
  const m = S.pv.mode;
  if (m === 'direct') {
    if (S.pv.r) markDirectFail(S.pv.r);
    switchMode('remux', `${reason}，已切「转封装」…`);
  } else if (m === 'remux') {
    switchMode('transcode', `${reason}，已切「硬件转码」…`);
  } else {
    $('#pvLoad').hidden = true;
    $('#pvHint').textContent = '播放失败：编码/音轨不受支持，可点「下载原文件」用本地播放器。';
  }
}
function setPvMode(mode, keepHint, manual) {
  clearWatch();
  S.pv.mode = mode;
  S.pv.manual = !!manual;
  S.pv.offset = 0;
  PV_MODES.forEach((m) => { const b = pvBtn(m); if (b) b.classList.toggle('on', m === mode); });
  const v = $('#pvVideo');
  if (mode === 'direct') {
    v.setAttribute('controls', 'controls');
    $('#pvCtl').hidden = true;
  } else {
    v.removeAttribute('controls');
    $('#pvCtl').hidden = false;
  }
  if (!keepHint) $('#pvHint').textContent = `当前：${PV_NAME[mode]}`;
  playVideo();
}
function switchMode(mode, msg) {
  if (msg) $('#pvHint').textContent = msg;
  setPvMode(mode, true);
}
function playVideo() {
  const v = $('#pvVideo');
  const start = Math.max(0, Math.round(S.pv.offset || 0));
  if (S.pv.mode === 'direct') {
    if (!S.pv.directUrl) { switchMode('remux', '取不到直出地址，已切换「转封装」…'); return; }
    v.src = S.pv.directUrl + (start > 0 ? '#t=' + start : '');
  } else {
    v.src = URL_(`/api/preview?${qs({ path: S.pv.path, mode: S.pv.mode, start })}&t=${Date.now()}`);
  }
  if (S.pv.mode !== 'direct') $('#pvLoad').hidden = false;
  v.load();
  v.play().catch(() => {});
  $('#pvPlay').innerHTML = ICON_PAUSE;
  updateTime();
  startWatch();
  watchAudio();
}
function pvNow() {
  const v = $('#pvVideo');
  if (S.pv.mode === 'direct') return v.currentTime || 0;
  return (S.pv.offset || 0) + (v.currentTime || 0);
}
function updateTime() {
  const cur = Math.min(S.pv.dur || 1e9, pvNow());
  $('#pvTime').innerHTML = pvTimeHtml(cur, S.pv.dur);
  if (document.activeElement !== $('#pvSeek')) $('#pvSeek').value = Math.min(cur, Number($('#pvSeek').max));
}
function pvSeekTo(t) {
  t = Math.max(0, Math.min(S.pv.dur || 1e9, t));
  const v = $('#pvVideo');
  if (S.pv.mode === 'direct') {
    v.currentTime = t;
    $('#pvSeek').value = t;
    updateTime();
  } else {
    S.pv.offset = t;          // 转封装/转码无法内部跳转，从该时间点重新起流（约 1 秒）
    playVideo();
  }
}
function stopVideo() {
  const v = $('#pvVideo');
  v.pause();
  v.removeAttribute('src');
  v.load();
}

/* ---------------- 操作 ---------------- */
async function doAct(action, extra) {
  const ids = Array.from(S.sel.keys());
  if (!ids.length) { toast('请先勾选文件'); return; }
  const payload = Object.assign({ action, ids }, extra || {});
  const dry = await api('/api/act?' + qs(), { method: 'POST', body: JSON.stringify(Object.assign({ dry: true }, payload)) });
  if (!dry.plan.length) { toast('没有可操作的文件'); return; }
  const preview = dry.plan.slice(0, 25).map((p) => `${p.name}  ${fmtSize(p.size || 0)}`).join('\n');
  const more = dry.plan.length > 25 ? `\n… 共 ${dry.plan.length} 个` : '';
  const titles = { delete: '确认删除（不可恢复）', move: '确认移动', keep: '确认保留' };
  $('#cfTitle').textContent = titles[action] || '确认';
  $('#cfBody').textContent = `${dry.plan.length} 个文件，合计 ${fmtSize(dry.bytes)}`
    + (action === 'move' ? `\n目标目录：${extra.target}` : '') + `\n\n${preview}${more}`;
  openM('#mConfirm');
  $('#cfOk').onclick = async () => {
    closeM('#mConfirm');
    try {
      const res = await api('/api/act?' + qs(), { method: 'POST', body: JSON.stringify(Object.assign({ dry: false }, payload)) });
      toast(`完成 ${res.done} 个${res.errors.length ? `，失败 ${res.errors.length}：${res.errors[0]}` : ''}`, 4200);
      S.sel.clear();
      updateSel();
      await refreshAll();
    } catch (e) { toast('操作失败：' + e.message, 4000); }
  };
}

/* ---------------- 设置 ---------------- */
/* 只保留运行参数；评分口径（权重/阈值/封顶）已写死在代码里，不再暴露 */
const CFG_FIELDS = [
  ['cfgInspect', 'inspect_on_scan', 'bool'],
  ['cfgHideSys', 'hide_sys_dirs', 'bool'],
  ['cfgFrames', 'q_frames', 'int'], ['cfgQConc', 'q_concurrency', 'int'],
  ['cfgGpu', 'use_gpu', 'bool'],
  ['cfgBlockPen', 'block_penalty_on', 'bool'],
  ['cfgBppAdj', 'bpp_adjust_on', 'bool'],
  ['cfgConc', 'concurrency', 'int'],
  ['cfgPin', 'pin', 'str'],
];
function fillCfg(cfg) {
  CFG_FIELDS.forEach(([id, key, type]) => {
    const el = $('#' + id);
    if (!el) return;
    if (type === 'bool') el.checked = !!cfg[key];
    else el.value = cfg[key] == null ? '' : cfg[key];
  });
  const btnScan = $('#btnScan');
  if (btnScan) btnScan.textContent = cfg.inspect_on_scan ? '扫描+体检' : '扫描';
}
async function loadCfg() {
  const j = await api('/api/config?' + qs());
  S.cfg = j.config;
  S.roots = j.config.roots || [];
  fillCfg(j.config);
  renderRoots();
}
function readCfg() {
  const out = {};
  CFG_FIELDS.forEach(([id, key, type]) => {
    const el = $('#' + id);
    if (!el) return;
    if (type === 'bool') out[key] = el.checked;
    else if (type === 'str') out[key] = el.value.trim();
    else out[key] = el.value === '' ? null : Number(el.value);
  });
  Object.keys(out).forEach((k) => { if (out[k] === null) delete out[k]; });
  return out;
}
async function saveCfg() {
  const body = readCfg();
  if (body.w_res != null && body.w_br != null && body.w_clarity != null
      && body.w_res + body.w_br + body.w_clarity <= 0) {
    toast('三个权重不能同时为 0'); return;
  }
  const j = await api('/api/config?' + qs(), { method: 'POST', body: JSON.stringify(body) });
  S.cfg = j.config;
  if ((body.pin || '') !== PIN()) localStorage.setItem('mqc_pin', body.pin || '');
  closeM('#mCfg');
  fillCfg(j.config);
  const r = await api('/api/rescore?' + qs(), { method: 'POST', body: '{}' });
  toast(`已保存并重算 ${r.rescored} 条${r.quality_rescored ? `（含画面分 ${r.quality_rescored} 条）` : ''}`);
  await refreshAll();
}
async function resetCfg() {
  const j = await api('/api/config?' + qs({ defaults: 1 }));
  fillCfg(j.config);
  toast('已填入默认值，点「保存并重算」生效');
}
async function refreshAll() {
  await Promise.all([loadStats(), loadList()]);
}

/* ---------------- 事件 ---------------- */
let qTimer = null;
$$('.chip[data-flag]').forEach((c) => c.addEventListener('click', () => {
  $$('.chip[data-flag]').forEach((x) => x.classList.remove('on'));
  c.classList.add('on');
  S.flag = c.dataset.flag;
  S.page = 1;
  loadList();
}));
$('#q').addEventListener('input', (e) => {
  const v = e.target.value.trim();
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { S.q = v; S.page = 1; loadList(); }, 300);
});
$('#sort').addEventListener('change', (e) => { S.sort = e.target.value; loadList(); });
$('#verdict').addEventListener('change', (e) => { S.verdict = e.target.value; S.page = 1; loadList(); });
$('#qfilter').addEventListener('change', (e) => { S.qfilter = e.target.value; S.page = 1; loadList(); });
$('#prev').addEventListener('click', () => { if (S.page > 1) { S.page--; loadList(); } });
$('#next').addEventListener('click', () => { S.page++; loadList(); });
$('#chkAll').addEventListener('change', (e) => {
  S.rows.forEach((r) => { e.target.checked ? S.sel.set(r.id, r.size) : S.sel.delete(r.id); });
  renderTable();
  updateSel();
});
$('#btnScan').addEventListener('click', startScan);
$('#btnInspect').addEventListener('click', startInspect);
$('#btnStop').addEventListener('click', async () => {
  await api('/api/job/stop?' + qs(), { method: 'POST', body: '{}' });
  toast('正在停止…');
});
$('#btnCfg').addEventListener('click', () => openM('#mCfg'));
$('#cfgSave').addEventListener('click', () => saveCfg().catch((e) => toast('保存失败：' + e.message, 5000)));
$('#cfgReset').addEventListener('click', () => resetCfg().catch((e) => toast('读取默认值失败：' + e.message)));
$('#cfgResetPlan').addEventListener('click', () => { resetPlanHints(); toast('已清除记住的播放方式，下次预览重新自动判断'); });
$('#cfgInspect').addEventListener('change', (e) => { $('#btnScan').textContent = e.target.checked ? '扫描+体检' : '扫描'; });
$('#pClose').addEventListener('click', () => { $('#progress').hidden = true; });
$('#btnExport').addEventListener('click', () => window.open(URL_('/api/export.csv?' + qs()), '_blank'));
$('#btnKeep').addEventListener('click', () => doAct('keep'));
$('#btnDelete').addEventListener('click', () => doAct('delete'));
$('#btnMove').addEventListener('click', () => {
  if (!S.sel.size) { toast('请先勾选文件'); return; }
  openPick('move');
});
$('#pickOk').addEventListener('click', () => pickOk());
$('#pvDirect').addEventListener('click', () => setPvMode('direct', false, true));
$('#pvRemux').addEventListener('click', () => setPvMode('remux', false, true));
$('#pvTrans').addEventListener('click', () => setPvMode('transcode', false, true));
$('#pvReload').addEventListener('click', () => {
  S.pv.offset = Number($('#pvStart').value || 0);
  playVideo();
});
$('#pvPlay').addEventListener('click', () => {
  const v = $('#pvVideo');
  if (v.paused) v.play().catch(() => {}); else v.pause();
});
$('#pvVideo').addEventListener('playing', () => {
  $('#pvLoad').hidden = true;
  clearWatch();
  // 只有「直出成功」或「用户手动选的方式成功」才记为该格式的偏好，避免降级结果污染同类文件
  if (S.pv.r && (S.pv.mode === 'direct' || S.pv.manual)) {
    setPlanState(S.pv.r, { ok: S.pv.mode, fails: 0 });
  }
});
$('#pvVideo').addEventListener('canplay', () => { $('#pvLoad').hidden = true; });
['progress', 'loadedmetadata', 'loadeddata', 'suspend', 'canplaythrough'].forEach((ev) => {
  $('#pvVideo').addEventListener(ev, bumpWatch);
});
$('#pvVideo').addEventListener('waiting', () => { if (S.pv.mode !== 'direct') $('#pvLoad').hidden = false; });
$('#pvVideo').addEventListener('play', () => { $('#pvPlay').innerHTML = ICON_PAUSE; });
$('#pvVideo').addEventListener('pause', () => { $('#pvPlay').innerHTML = ICON_PLAY; });
$('#pvVideo').addEventListener('timeupdate', updateTime);
$('#pvVideo').addEventListener('durationchange', updateTime);
$('#pvSeek').addEventListener('change', (e) => pvSeekTo(Number(e.target.value || 0)));
$('#pvSeek').addEventListener('input', (e) => {
  $('#pvTime').innerHTML = pvTimeHtml(Number(e.target.value || 0), S.pv.dur);
});
$('#pvBack').addEventListener('click', () => pvSeekTo(pvNow() - 10));
$('#pvFwd').addEventListener('click', () => pvSeekTo(pvNow() + 10));
$('#pvFull').addEventListener('click', () => {
  const v = $('#pvVideo');
  if (v.requestFullscreen) v.requestFullscreen();
  else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();
});
$('#pvShot').addEventListener('click', () => {
  const box = $('#pvShots');
  box.innerHTML = '';
  [5, 30, 60].forEach((t) => {
    const img = document.createElement('img');
    img.src = URL_(`/api/thumb?${qs({ path: S.pv.path, t })}`);
    box.appendChild(img);
  });
});
$('#pvVideo').addEventListener('error', () => pvFail('播放器报错'));
document.addEventListener('visibilitychange', () => { if (document.hidden) $('#pvVideo').pause(); });

(async function init() {
  $$('.modal').forEach((m) => { m.hidden = true; });
  window.addEventListener('error', (e) => toast('页面脚本异常：' + (e.message || e), 6000));
  try {
    await loadCfg();
    await refreshAll();
    const j = await api('/api/job?' + qs());
    renderJob(j.job, j.last_scan);
    if (j.job.state === 'scanning') {
      $('#progress').hidden = false;
      poll = setInterval(pollJob, 700);
    }
  } catch (e) {
    toast('加载失败：' + e.message, 5000);
  }
})();
