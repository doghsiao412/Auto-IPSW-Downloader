const $ = id => document.getElementById(id);
let token, loaded = false, catalog = [], picked = new Set(), dirty = false, connectionError = false;
const bytes = n => Number.isFinite(n) ? (n / 1e9).toFixed(2) + ' GB' : '—';
const date = n => n ? new Date(n * 1000).toLocaleString('zh-TW', {timeZone:'Asia/Taipei'}) : '—';
const phases = {idle:'待命',checking:'查詢中',downloading:'下載中',verifying:'驗證中',exporting:'複製至外接硬碟',syncing:'同步中',waiting:'等待重試'};
const pages = {
  overview: {title:'總覽', eyebrow:'工作空間', description:'一眼掌握下載服務與儲存空間。'},
  downloads: {title:'下載作業', eyebrow:'同步作業', description:'查看每個檔案的下載、驗證與外接硬碟進度。'},
  settings: {title:'設定', eyebrow:'下載偏好', description:'管理下載範圍、檢查排程與舊版清理。'},
  activity: {title:'通知與紀錄', eyebrow:'活動中心', description:'確認郵件提交狀態，回顧每個檔案的同步紀錄。'}
};
function showPage(focus = false) {
  if (location.hash === '#main-content') return;
  const requested = location.hash.slice(1);
  const key = Object.hasOwn(pages, requested) ? requested : 'overview';
  const page = pages[key];
  document.querySelectorAll('[data-page]').forEach(panel => panel.hidden = panel.dataset.page !== key);
  document.querySelectorAll('[data-nav]').forEach(link => {
    if (link.dataset.nav === key) link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  });
  $('page-title').textContent = page.title;
  $('page-eyebrow').textContent = page.eyebrow;
  $('page-description').textContent = page.description;
  document.title = `${page.title} · Auto IPSW Downloader`;
  if (focus) { $('page-title').focus({preventScroll:true}); window.scrollTo(0,0); }
}
function renderDirty() {
  $('unsaved-nav').hidden = !dirty;
  $('unsaved-status').hidden = !dirty;
}
function markDirty() { dirty = true; $('saved').textContent = ''; renderDirty(); }
function renderOverview(d) {
  const types = {iPhone:'iOS',iPad:'iPadOS',Mac:'macOS'};
  const versions = d.config.types.filter(t=>d.state.latest[types[t]]).map(t=>{
    const div=document.createElement('div'); div.className='version-row';
    const name=document.createElement('span'), version=document.createElement('strong');
    name.textContent=types[t]; version.textContent=d.state.latest[types[t]]; div.append(name,version); return div;
  });
  fillList('overview-versions',versions,'等待版本資訊');
  $('overview-files').textContent = `${d.state.plan.length} 個`;
  $('overview-size').textContent = bytes(d.state.plan.reduce((n,f)=>n+f.size,0));
  $('overview-scope').textContent = `${d.config.types.join(' ＋ ')} · ${d.config.models.length ? `指定 ${d.config.models.length} 個機型` : '所選類別全部適用機型'}`;
  $('overview-interval').textContent = d.config.interval_hours < 1 ? `每 ${d.config.interval_hours*60} 分鐘` : `每 ${d.config.interval_hours} 小時`;
  $('overview-last').textContent = date(d.state.last_check);
  $('overview-next').textContent = d.config.enabled ? date(d.state.next_check) : '自動下載已暫停';
  $('overview-cleanup').textContent = !d.config.cleanup ? '未啟用' : d.config.cleanup_early ? '偵測到新版時先刪除' : '新版同步後刪除';
  $('overview-speed').textContent = d.config.min_speed_mbps ? `每個檔案 ${d.config.min_speed_mbps} MB/s，過慢自動重連` : '未啟用';
  $('service-mode').textContent = d.config.enabled ? '自動下載' : '已暫停';
  $('nav-count').textContent = (d.transfers || []).length;
  $('nav-count').hidden = !(d.transfers || []).length;
  $('record-count').textContent = `${d.records.length} 筆紀錄`;
}
window.addEventListener('hashchange',()=>showPage(true));
showPage();
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function row(title, note, right) {
  const div = document.createElement('div'); div.className = 'row';
  const text = document.createElement('div'), name = document.createElement('div'), sub = document.createElement('div'), size = document.createElement('div');
  name.className = 'row-title'; name.textContent = title; sub.className = 'row-note'; sub.textContent = note;
  size.className = 'row-size'; size.textContent = right; text.append(name, sub); div.append(text, size); return div;
}
function fillList(id, items, empty) {
  $(id).replaceChildren(...items);
  if (!items.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = empty; $(id).append(p); }
}
function models() {
  const filter = $('filter').value.toLowerCase();
  const types = [...document.querySelectorAll('[name=type]:checked')].map(e=>e.value);
  $('selection').textContent = picked.size ? `· 已選 ${picked.size} 個` : '· 所選類別全部機型';
  $('models').replaceChildren();
  for (const d of catalog) {
    const kind = d.identifier.startsWith('iPhone') ? 'iPhone' : d.identifier.startsWith('iPad') ? 'iPad' : 'Mac';
    if (!types.includes(kind) || !(d.name + d.identifier).toLowerCase().includes(filter)) continue;
    const label = document.createElement('label'), input = document.createElement('input'), span = document.createElement('span'), small = document.createElement('small');
    input.type = 'checkbox'; input.checked = picked.has(d.identifier);
    input.onchange = () => { input.checked ? picked.add(d.identifier) : picked.delete(d.identifier); markDirty(); $('selection').textContent = picked.size ? `· 已選 ${picked.size} 個` : '· 所選類別全部機型'; };
    span.textContent = d.name; small.textContent = d.identifier; span.append(small); label.append(input, span); $('models').append(label);
  }
}
const rate = n => n >= 1e6 ? `${(n / 1e6).toFixed(2)} MB/s` : `${(n / 1e3).toFixed(1)} KB/s`;
const transferPhases = {downloading:'下載中',waiting_verify:'等待驗證',verifying:'內部驗證中',waiting_export:'等待複製',exporting:'外接硬碟處理中',reconnecting:'速度過慢或連線中斷，重新連線續傳'};
const exportPhases = {waiting:'準備複製',copying:'複製至外接硬碟',flushing:'等待磁碟完成寫入',readback:'讀回驗證',done:'複製與驗證完成',error:'處理中斷，保留內部暫存'};
const duration = seconds => !Number.isFinite(seconds) ? '估算中' : seconds < 60 ? `約 ${Math.ceil(seconds)} 秒` : seconds < 3600 ? `約 ${Math.ceil(seconds / 60)} 分鐘` : `約 ${(seconds / 3600).toFixed(1)} 小時`;
function transferDetail(t) {
  const percent = t.total ? Math.min(100, 100 * t.progress / t.total).toFixed(1) : '0.0';
  return `${bytes(t.progress)} / ${bytes(t.total)} · ${percent}%${t.speed_bps > 0 ? ` · ${rate(t.speed_bps)} · 剩餘 ${duration(t.eta_seconds)}` : ' · 速度與剩餘時間估算中'}`;
}
function renderTransfers(d) {
  const tasks = d.transfers || [];
  $('transfer-summary').textContent = `${tasks.filter(t=>t.phase==='downloading').length} 個下載中 · 最多同時下載 ${d.max_transfers || 5} 個，有空位立即遞補`;
  fillList('transfers', tasks.map(t=>{
    const card = document.createElement('div'); card.className = 'transfer';
    const exporting = t.phase === 'exporting';
    const note = (transferPhases[t.phase] || t.phase) + (t.reconnects ? ` · 已自動重新連線 ${t.reconnects} 次` : '');
    card.append(row(t.name, note, exporting ? '見下方進度' : ''));
    if (['downloading','verifying','reconnecting'].includes(t.phase)) {
      const bar = document.createElement('progress'); bar.max = t.total || 1; bar.value = t.progress; bar.setAttribute('aria-label', `${t.name} ${note}`);
      const detail = document.createElement('p'); detail.className = 'muted hint'; detail.textContent = transferDetail(t); card.append(bar,detail);
    }
    return card;
  }), '目前沒有進行中的檔案');
  const p = d.export_progress;
  $('export-progress').hidden = !p;
  if (!p) { $('export-stage').textContent='等待複製'; $('export-name').textContent=''; $('export-detail').textContent='複製完成後會繼續讀回驗證，驗證成功才清除內部暫存。'; return; }
  $('export-stage').textContent = exportPhases[p.stage] || p.stage;
  $('export-name').textContent = p.name;
  $('export-progress').max = p.total || 1; $('export-progress').value = p.progress;
  if (p.stage === 'flushing') $('export-progress').removeAttribute('value');
  const stale = p.active && Date.now()/1000-p.updated_at>15;
  $('export-detail').textContent = p.stage === 'done' ? `已同步 ${bytes(p.total)} · ${date(p.updated_at)}` : p.stage === 'flushing' ? `${bytes(p.total)} 已送出，等待磁碟完成寫入；接著讀回驗證。` : p.stage === 'error' ? '複製或驗證未完成，內部暫存保留供重試。' : transferDetail(p);
  if (stale) $('export-detail').textContent += ' · 等待磁碟回應，進度尚未更新';
}
async function refresh() {
  try {
    const response = await fetch('/api/status'); if (!response.ok) throw Error('NAS 狀態讀取失敗');
    const d = await response.json(); token = d.token;
    if (!loaded) {
      document.querySelectorAll('[name=type]').forEach(e=>e.checked = d.config.types.includes(e.value));
      $('enabled').checked = d.config.enabled; $('cleanup').checked = d.config.cleanup; $('cleanup-early').checked = !!d.config.cleanup_early; $('cleanup-early').disabled = !d.config.cleanup;
      $('interval').value = d.config.interval_hours; $('min-speed').value = String(d.config.min_speed_mbps ?? 0); picked = new Set(d.config.models); loaded = true; models();
    }
    renderTransfers(d);
    renderOverview(d);
    $('connection').textContent = '已連線'; $('connection').classList.remove('offline');
    if (connectionError) { notice(''); connectionError = false; }
    $('phase').textContent = phases[d.state.phase] || d.state.phase;
    $('message').textContent = d.state.message;
    const active = d.state.phase === 'downloading'; $('progress').hidden = !active;
    $('progress').max = d.state.total || 1; $('progress').value = d.state.progress;
    $('bytes').textContent = active ? `${bytes(d.state.progress)} / ${bytes(d.state.total)}` : '';
    $('disk').textContent = d.disk.connected ? '已連接' : d.disk.ejected ? '已退出' : '等待連接';
    const mac = d.platform === 'mac';
    $('eject').hidden = mac || !d.disk.connected;
    $('mount').hidden = mac || d.disk.connected;
    document.querySelectorAll('.nas-only').forEach(e => e.hidden = mac);
    $('version').textContent = `版本 ${d.version || '—'}`;
    $('disk-info').textContent = d.disk.connected ? `可用 ${bytes(d.disk.free)} · ${d.disk.folder}` : d.disk.error;
    $('staging').textContent = bytes(d.staging_bytes); $('free').textContent = `內部可用 ${bytes(d.internal_free)}`;
    $('schedule').textContent = `上次檢查 ${date(d.state.last_check)} · 下次 ${date(d.state.next_check)}${dirty ? ' · 有尚未儲存的設定' : ''}${d.state.schedule_error ? ' · ' + d.state.schedule_error : ''}`;
    if (d.notifications) $('notify-status').textContent = `待提交 ${d.notifications.pending} 則 · 最近提交 DSM：${date(d.notifications.last_submitted)}${d.notifications.last_error ? ' · ' + d.notifications.last_error : ''}`;
    $('latest').textContent = Object.entries(d.state.latest).map(([k,v])=>`${k} ${v}`).join(' ／ ');
    const added = new Set(d.state.added || []);
    $('plan-summary').textContent = `${d.state.plan.length} 個檔案 · ${bytes(d.state.plan.reduce((a,b)=>a+b.size,0))}${added.size ? ` · 本次新增 ${added.size} 個` : ''}`;
    const synced = new Map(d.records.filter(r=>r.status==='exported').map(r=>[r.name, r]));
    fillList('plan', d.state.plan.map(f=>{
      const r = synced.get(f.name);
      const where = added.has(f.name) ? '本次新增' : r ? `已在外接硬碟 · ${date(r.synced_at || r.time)} 同步` : '尚未同步';
      return row(f.name, `${f.label || f.version} · Build ${f.build} · ${f.devices.length} 個適用機型 · ${where}`, bytes(f.size));
    }), '儲存範圍後按「立即檢查」');
    const status = {downloading:'下載中／等待續傳',exported:'已同步至外接硬碟',waiting_export:'內部已完成，等待複製',removed_old:'已清除舊版'};
    fillList('records', d.records.sort((a,b)=>(b.synced_at || b.time)-(a.synced_at || a.time)).map(f=>row(f.name, `${status[f.status] || f.status} · ${date(f.synced_at || f.time)}`, bytes(f.size))), '尚無下載紀錄');
  } catch(e) { connectionError = true; $('connection').textContent = '暫時離線'; $('connection').classList.add('offline'); notice(e.message + '；將自動重試'); }
}
async function post(path, data) {
  const r = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(data)});
  const d = await r.json(); if (!r.ok) throw Error(d.error); return d;
}
$('settings').addEventListener('change', ()=>{markDirty(); models(); $('cleanup-early').disabled = !$('cleanup').checked;});
$('settings').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    await post('/api/config', {enabled:$('enabled').checked,cleanup:$('cleanup').checked,cleanup_early:$('cleanup').checked && $('cleanup-early').checked,interval_hours:Number($('interval').value),min_speed_mbps:Number($('min-speed').value),types:[...document.querySelectorAll('[name=type]:checked')].map(e=>e.value),models:[...picked]});
    dirty = false; renderDirty(); $('saved').textContent = '已儲存'; notice(''); await refresh();
  } catch(e) { notice(e.message); } finally { button.disabled = false; }
};
$('check').onclick = async () => {
  if (dirty) return notice('請先儲存設定，再檢查所選範圍。');
  try { await post('/api/check', {}); notice('已排入檢查；若正在下載，會在本輪完成後處理。'); } catch(e) { notice(e.message); }
};
$('filter').oninput = models;
$('clear-models').onclick = ()=>{picked.clear();markDirty();models();};
$('notify-test').onclick = async () => {
  const button = $('notify-test'); button.disabled = true;
  try { await post('/api/notifications/test', {}); notice('已排入 DSM 測試通知，請稍後查看收件信箱。'); }
  catch(e) { notice(e.message); } finally { button.disabled = false; }
};
$('eject').onclick = async () => {
  if (!confirm('要安全退出外接硬碟嗎？\n\n退出後不會開始新的下載；內部暫存會保留，重新插上後自動繼續。')) return;
  const button = $('eject'); button.disabled = true; notice('正在退出外接硬碟…');
  try { await post('/api/disk/eject', {}); notice('外接硬碟已安全退出，可以拔除。'); await refresh(); }
  catch(e) { notice(e.message); } finally { button.disabled = false; }
};
$('mount').onclick = async () => {
  const button = $('mount'); button.disabled = true; notice('正在重新掛載外接硬碟…');
  try { await post('/api/disk/mount', {}); notice('外接硬碟已重新掛載，服務會自動繼續。'); await refresh(); }
  catch(e) { notice(e.message); } finally { button.disabled = false; }
};
async function start() {
  await refresh();
  try { const r = await fetch('/api/devices'); const d = await r.json(); if (!r.ok) throw Error(d.error); catalog = d.devices.sort((a,b)=>a.name.localeCompare(b.name,'en',{numeric:true})); models(); }
  catch(e) { notice(e.message); }
  setInterval(refresh, 3000);
}
start();
