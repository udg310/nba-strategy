'use strict';

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const route = location.pathname.replace(/\/$/, '') || '/';
const home = route === '/';
const gamesPage = route === '/games';
const historyPage = route === '/activity';
const positionsPage = route === '/positions';
const detailId = route.startsWith('/positions/') ? decodeURIComponent(route.slice(11)) : null;
const apiRoot = '/api/dashboard/v1';
const dayMs = 86400000;
const zone = 'Asia/Hong_Kong';
const dayKey = t => new Date(t + dayMs / 3).toISOString().slice(0, 10);
const dayStart = d => Date.parse(`${d}T00:00:00+08:00`);
const dateBefore = d => dayKey(dayStart(d) - dayMs);
const timeFormat = new Intl.DateTimeFormat('en-GB', {timeZone:zone, hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
const epoch = s => typeof s === 'string' ? Date.parse(s) : NaN;
function stamp(value, date = true) {
  const time = epoch(value);
  if (!Number.isFinite(time)) return '—';
  return (date ? dayKey(time) + ' ' : '') + timeFormat.format(time);
}
const escapeMap = {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'};
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => escapeMap[c]);
const labels = {
  buy:'买入', sell:'卖出', settlement:'结算', confirmed:'已核实', aggregate:'运行汇总', inferred:'推断',
  verified:'已核实', estimated:'含估算手续费', marked:'含持仓估值', partial:'部分可算', unavailable:'暂无法计算',
  matched:'撮合', chain_confirmed:'链上确认', recorded:'记录于', settlement_booked:'结算入账', simulated:'模拟',
  order:'订单', submitted:'已提交', fill:'成交', signal:'信号', revision:'修订', rejected:'已拒绝', cancelled:'已撤销', filled:'已成交',
  open:'持有中', partially_closed:'部分卖出', pending_settlement:'待结算', closed:'已平仓', review:'待核对',
  missing_fee:'缺手续费依据', missing_cost:'缺买入成本', stale_quote:'报价过期', no_bid:'暂无买盘', no_quote:'缺买一报价', no_day_baseline:'缺零点估值', unattributed:'成交归属待确认', source_gap:'记录不完整',
  shared_exit:'跨通道卖出待分摊', balance_adjustment:'余额增加待归属', balance_disappeared:'缺退出成交凭据', resolved:'市场已定兑付价',
  fresh:'有效报价', stale:'报价过期', missing:'缺报价', ok:'正常', delayed:'延迟', down:'异常', unknown:'状态未知', unconfigured:'未接入',
  runner:'运行记录', executor:'执行账本', quotes:'持仓行情', attempts:'订单尝试', scoreboard:'NBA 赛程'
};
const label = key => Object.hasOwn(labels, key) ? labels[key] : key || '—';
const matchTitle = row => row.game_label && row.game_label !== row.game_id && !/^\d+$/.test(row.game_label) ? row.game_label : '对阵信息暂缺';
const marketSide = row => [row.market, row.outcome].filter(v => v && !/^\d+$/.test(v)).join(' · ') || '方向暂缺';
function positionState(p) {
  if (p.state === 'pending_settlement' && p.quote?.status === 'resolved') return '兑付待确认';
  if (p.state !== 'review') return label(p.state);
  const cause = p.issues?.find(i => ['shared_exit','balance_adjustment','balance_disappeared','missing_cost','unattributed','source_gap','missing_fee'].includes(i.code));
  return cause ? label(cause.code) : '缺成交记录';
}
const metrics = {today:'今日盈亏', total:'累计总盈亏', realized:'已实现盈亏', unrealized:'未实现盈亏'};
const symbols = {today:'◷', total:'↗', realized:'▣', unrealized:'∿'};
let env = params.get('env') === 'paper' ? 'paper' : 'live';
let snap, version, asOf, generation = 0, controller, source, retryTimer, pollTimer, snapshotTimer;
let recoveringSince, retryStep, lastContact, connection, clockOffset = 0, gapTask, nextPoll;
let range = 'today', chartData, chartError, chartToken = 0, chartBusy = false, wantedChart;
let pageData, pageVersion, pageBusy, pageError, pageToken = 0, wantedPage, historyDirty = false;
let activeDialog, todayMap = new Map(), positionMap = new Map(), lastExpiry = '', dayPending = false;
let daysReady = false, message = '', chartSelection = null, appliedDates;
const now = () => Date.now() + clockOffset;
const businessDay = () => snap?.business_date || dayKey(now());
const currency = () => snap?.currency || '';
const urlFor = (path, values = {}) => {
  const url = new URL(path, location.origin);
  if (env === 'paper') url.searchParams.set('env', env);
  for (const [key, value] of Object.entries(values)) if (value != null && value !== '') url.searchParams.set(key, value);
  return url.pathname + url.search;
};
const detailURL = id => urlFor('/positions/' + encodeURIComponent(id));
const validNumber = value => typeof value === 'string' && /^[-+]?\d+(?:\.\d+)?$/.test(value);
function formatAmount(value, {cash = true, signed = false, precise = false} = {}) {
  if (!validNumber(value)) return '—';
  const negative = value.startsWith('-'), [integer, fraction = ''] = value.replace(/^[-+]/, '').split('.');
  // Round presentation using decimal integer arithmetic. The API retains the source precision.
  const tinyValue = !/[1-9]/.test(integer) && fraction.startsWith('00') && /[1-9]/.test(fraction);
  const digits = precise || tinyValue ? Math.min(fraction.length, 6) : 2;
  let units = BigInt(integer + fraction.padEnd(digits, '0').slice(0, digits));
  if (fraction.length > digits && fraction[digits] >= '5') units += 1n;
  const aligned = units.toString().padStart(digits + 1, '0');
  const whole = (digits ? aligned.slice(0, -digits) : aligned).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const tail = digits ? aligned.slice(-digits) : '';
  const displayTail = precise || tinyValue ? tail.replace(/0+$/, '') : tail;
  const decimals = displayTail ? '.' + displayTail : '';
  const prefix = cash ? (currency() === 'USD' ? '$' : currency() ? currency() + ' ' : '') : '';
  const sign = negative && units ? '−' : signed && units ? '+' : '';
  if (digits && !units && /[1-9]/.test(fraction)) return (negative ? '−' : '') + '<' + prefix + '0.' + '0'.repeat(digits - 1) + '1';
  return sign + prefix + whole + decimals;
}
const qty = value => formatAmount(value, {cash:false, precise:true});
const price = value => formatAmount(value, {precise:true});
const profit = value => formatAmount(value, {signed:true});
const color = value => !validNumber(value) || Number(value) === 0 ? '' : value.startsWith('-') ? 'negative' : 'positive';
const sideText = value => color(value) === 'positive' ? '盈利' : color(value) === 'negative' ? '亏损' : validNumber(value) ? '持平' : '';
const metricValue = m => ['verified','estimated','marked'].includes(m?.status) && validNumber(m.value) ? m.value : null;
const reasonList = issues => (issues || []).map(issue => label(issue.code)).filter((v, i, a) => a.indexOf(v) === i).join('、');
function quoteExpired(p) {
  const quantity = p?.shared_exit?.quantity ?? p?.quantity;
  if (p?.quote?.status !== 'fresh' || !validNumber(quantity) || Number(quantity) <= 0) return false;
  const expires = epoch(p.quote.valid_until);
  const observed = epoch(p.quote.observed_at);
  const sourceTime = epoch(p.quote.source_at);
  const deadline = Math.min(Number.isFinite(expires) ? expires : Infinity, Number.isFinite(observed) ? observed + 15000 : Infinity, Number.isFinite(sourceTime) ? sourceTime + 15000 : Infinity);
  return now() >= deadline;
}
function adjusted(m, stale) {
  if (!stale || !m) return m;
  return {...m, value:null, known_subtotal:null, covered_count:null, status:'partial', issues:[...(m.issues || []), {code:'stale_quote'}]};
}
function stalePositions() { return [...positionMap.values()].filter(quoteExpired); }
function quotePrice(position, side) {
  const q = position.quote;
  if (quoteExpired(position) || q?.status === 'stale') return '报价过期';
  if (side === 'bid' && q?.status === 'no_bid') return '无买盘';
  return q?.status === 'fresh' || (side === 'ask' && q?.status === 'no_bid') ? price(q[side]) : '—';
}
function gamesStatus() {
  const src = snap?.games_source;
  if (src?.status === 'ok' && (!navigator.onLine || lastContact && Date.now() - lastContact > 30000 || now() - epoch(src.last_success_at) > 30000)) return 'delayed';
  return src?.status || (snap ? 'unconfigured' : null);
}
function summaryMetric(key) {
  if (dayPending && key === 'today') return null;
  return adjusted(snap?.summary?.[key], key !== 'realized' && stalePositions().length > 0);
}
function sourceState() {
  const trade = (snap?.sources || []).filter(s => s.name !== 'quotes' && s.name !== 'scoreboard');
  if (!snap) return '正在读取数据';
  if (!trade.length || trade.every(s => s.status === 'unconfigured')) return '交易源未接入';
  if (trade.some(s => s.status === 'down')) return '交易源异常';
  if (trade.some(s => s.status === 'delayed')) return '交易源延迟';
  if (trade.some(s => s.status === 'unknown')) return '交易源状态未知';
  return '';
}
function syncMessage() {
  $('message').textContent = message;
  $('message').hidden = !message;
}
function showMessage(text) { message = text; syncMessage(); }
function renderStatus() {
  const disconnected = lastContact && Date.now() - lastContact > 30000;
  const state = disconnected || !navigator.onLine ? '连接中断' : connection;
  $('feedStatus').textContent = state;
  $('feedStatus').className = 'chip' + (state === '实时连接' ? ' good' : '');
  $('headerMeta').textContent = [snap?.account_label, env === 'paper' ? '模拟盘' : '实盘', businessDay(), '香港时间', asOf && '更新于 ' + stamp(asOf)].filter(Boolean).join(' · ');
  const status = sourceState();
  $('sourceBanner').hidden = !snap || !status;
  const reasons = (snap?.sources || []).filter(s => !['quotes','scoreboard'].includes(s.name) && !['unconfigured','ok'].includes(s.status)).map(s => s.reason).filter(Boolean);
  $('sourceBanner').textContent = status ? [status,...new Set(reasons),asOf && '数据截至 ' + stamp(asOf)].filter(Boolean).join(' · ') : '';
  $('footerMeta').textContent = ['覆盖起点 ' + stamp(snap?.coverage_from), currency(), '香港时间 UTC+8'].filter(Boolean).join(' · ');
}
function setLinks() {
  for (const [id, path] of [['navHome','/'],['navGames','/games'],['navPositions','/positions'],['navHistory','/activity'],['allPositions','/positions'],['historyLink','/activity']]) $(id).href = urlFor(path);
  document.querySelector('.brand').href = urlFor('/');
  $(home ? 'navHome' : gamesPage ? 'navGames' : historyPage ? 'navHistory' : 'navPositions').setAttribute('aria-current','page');
  const heading = home ? '交易总览' : gamesPage ? '正在打的比赛' : positionsPage ? '仓位明细' : historyPage ? '历史交易' : '仓位详情';
  $('heading').textContent = heading;
  document.title = heading + ' · 稻香';
  $('dashboard').hidden = !home;
  $('gamesPanel').hidden = !gamesPage;
  $('activityPanel').hidden = !home && !historyPage;
  $('historyFilters').hidden = !historyPage;
  $('ledgerTitle').textContent = home ? '今日交易' : '交易记录';
  $('localKindLabel').hidden = !home;
  $('historyLink').hidden = !home;
  $('positionPanel').hidden = !positionsPage;
  $('detailPanel').hidden = !detailId;
}
function versionParts(v) {
  const hit = typeof v === 'string' && /^(.+)\.(\d+)$/.exec(v);
  if (!hit) throw Error('版本无效');
  return [hit[1], BigInt(hit[2])];
}
async function read(path, args = {}) {
  const mark = generation;
  const url = new URL(apiRoot + path, location.origin);
  url.searchParams.set('env',env);
  for (const [key, value] of Object.entries(args)) if (value != null && value !== '') url.searchParams.set(key,value);
  const response = await fetch(url, {cache:'no-store',signal:controller.signal});
  const result = await response.json();
  if (mark !== generation) throw new DOMException('环境已切换','AbortError');
  if (!response.ok) throw Object.assign(Error(result.message || '数据暂不可读'), {status:response.status});
  if (result.env !== env) throw Error('数据环境不一致');
  return result;
}
function closeStream() {
  if (source) source.close();
  source = null;
}
function clearNetwork() {
  closeStream();
  clearTimeout(retryTimer); clearTimeout(snapshotTimer); clearInterval(pollTimer);
  retryTimer = snapshotTimer = pollTimer = null;
  if (controller) controller.abort();
}
function storeSnapshot(result) {
  versionParts(result.version);
  const d = result.data;
  if (!d?.business_date || !Array.isArray(d.open_positions) || !Array.isArray(d.today_activity)) throw Error('快照字段不完整');
  positionMap = new Map(d.open_positions.map(p => [p.id, p]));
  todayMap = new Map(d.today_activity.filter(a => dayKey(epoch(a.time)) === d.business_date).map(a => [a.id,a]));
  snap = d;
  version = result.version;
  asOf = result.as_of;
  lastContact = Date.now();
  dayPending = businessDay() < dayKey(now());
  lastExpiry = expirySignature();
  renderMain();
  if (!daysReady) { initForms(); daysReady = true; }
  if (home) queueChart();
  else refreshPage();
  markPaint(version, 'snapshot', asOf);
}
async function start(reset = false) {
  const mark = ++generation;
  clearNetwork();
  controller = new AbortController();
  gapTask = null; recoveringSince = null; retryStep = 0; nextPoll = 0;
  chartToken++; pageToken++; chartBusy = false; pageBusy = false; wantedPage = wantedChart = null;
  if (!reset) {
    snap = version = asOf = pageData = chartData = null;
    delete document.documentElement.dataset.version;
    chartError = pageError = lastExpiry = ''; dayPending = false; historyDirty = false;
    positionMap.clear(); todayMap.clear(); lastContact = 0; daysReady = false; clockOffset = 0;
    connection = '连接中'; showMessage('');
    renderMain(); renderPage();
  } else {
    connection = '重新同步'; renderStatus();
  }
  async function fetchSnapshot() {
    if (mark !== generation) return;
    try {
      const result = await read('/snapshot');
      if (mark !== generation) return;
      if (reset) {
        pageData = chartData = pageVersion = null;
        historyDirty = false;
        pageError = chartError = '';
      }
      storeSnapshot(result);
      showMessage('');
      connectStream();
    } catch (e) {
      if (mark !== generation) return;
      connection = '连接中断'; renderStatus(); showMessage('快照暂不可读，正在重新连接。' + e.message);
      snapshotTimer = setTimeout(fetchSnapshot, 3000);
    }
  }
  await fetchSnapshot();
}
function applyChange(change) {
  if (change.env !== env) throw Error('更新环境不符');
  const [localGen, localNo] = versionParts(version), [newGen, newNo] = versionParts(change.version);
  if (newGen !== localGen) throw Object.assign(Error('数据版本已重建'), {status:410});
  if (newNo <= localNo) return;
  if (change.prev_version !== version || newNo !== localNo + 1n) throw Error('更新序号存在缺口');
  const p = change.patch;
  if (!p || typeof p !== 'object') throw Error('更新缺少内容');
  const date = p.business_date || snap.business_date;
  const rolled = date !== snap.business_date;
  if (rolled && !p.summary) throw Error('等待完整的跨日更新');
  const positions = new Map(positionMap), activity = rolled ? new Map() : new Map(todayMap);
  for (const item of p.positions_upsert || []) positions.set(item.id,item);
  for (const id of p.positions_remove || []) positions.delete(id);
  for (const a of p.activity_upsert || []) {
    if (dayKey(epoch(a.time)) === date) activity.set(a.id,a);
    else activity.delete(a.id);
  }
  for (const id of p.activity_remove || []) activity.delete(id);
  const next = {...snap, business_date:date};
  for (const key of ['summary','sources','live_games','games_source','coverage_from','account_label','currency']) if (key in p) next[key] = p[key];
  positionMap = positions; todayMap = activity; snap = next;
  asOf = change.published_at; version = change.version;
  dayPending = date < dayKey(now());
  lastExpiry = expirySignature();
  renderMain({activity:rolled || !!(p.activity_upsert || p.activity_remove || p.sources)});
  if (home && (p.history_dirty_from || rolled || p.summary)) queueChart();
  if (detailId && (p.detail_dirty?.includes(detailId) || p.positions_upsert?.some(pos => pos.id === detailId) || p.positions_remove?.includes(detailId))) refreshPage();
  if (positionsPage && (rolled || p.positions_upsert || p.positions_remove || p.history_dirty_from || p.detail_dirty?.length)) refreshPage();
  if (historyPage) {
    const start = dayStart(appliedDates.from), end = dayStart(appliedDates.to) + dayMs;
    const inRange = stamp => start <= epoch(stamp) && epoch(stamp) < end;
    const loadedIds = new Set((pageData?.items || []).map(a => a.id));
    if (inRange(p.history_dirty_from) || (p.activity_upsert || []).some(a => inRange(a.time) || loadedIds.has(a.id)) || (p.activity_remove || []).some(id => loadedIds.has(id))) {
      historyDirty = true; $('refreshHistory').hidden = false;
    }
    if (rolled) updateDateBounds();
  }
  markPaint(version,change.cause,asOf);
}
function markPaint(value, cause, published) {
  document.documentElement.dataset.version = value;
  if (document.hidden) return;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (document.documentElement.dataset.version !== value) return;
    window.dispatchEvent(new CustomEvent('dashboard:paint',{detail:{env,version:value,cause,published_at:published,painted_at:new Date().toISOString()}}));
  }));
}
function catchUp() {
  if (gapTask) return gapTask;
  const mark = generation;
  const task = (async () => {
    do {
      const after = version;
      const result = await read('/changes', {after,limit:500});
      if (generation !== mark) return;
      for (const item of result.data.items) applyChange(item);
      lastContact = Date.now();
      if (!result.data.has_more) break;
      if (after === version) throw Error('补读未能推进版本');
    } while (generation === mark);
  })();
  gapTask = task;
  task.finally(() => { if (gapTask === task) gapTask = null; }).catch(() => {});
  return task;
}
function recover() {
  const mark = generation;
  closeStream();
  if (!recoveringSince) {
    recoveringSince = Date.now(); nextPoll = recoveringSince + 10000;
    catchUp().catch(e => { if (generation === mark && e.status === 410) start(true); });
  }
  connection = Date.now() - recoveringSince >= 10000 ? '轮询更新' : '重连中';
  renderStatus();
  if (!pollTimer) pollTimer = setInterval(() => {
    if (generation !== mark || !recoveringSince || gapTask || Date.now() < nextPoll) return;
    nextPoll = Date.now() + 3000;
    connection = '轮询更新'; renderStatus();
    catchUp().catch(e => { if (generation === mark && e.status === 410) start(true); });
  }, 500);
  if (!retryTimer) {
    const delay = Math.min(15000, 1000 * 2 ** Math.min(4,retryStep++)) * (0.9 + Math.random() * 0.2);
    retryTimer = setTimeout(() => { retryTimer = null; connectStream(); },delay);
  }
}
function connectStream() {
  closeStream();
  if (!window.EventSource) { recover(); return; }
  const es = new EventSource(apiRoot + '/stream?' + new URLSearchParams({env,after:version}));
  const mark = generation;
  source = es;
  const current = () => source === es && mark === generation;
  function connected() {
    connection = '实时连接'; recoveringSince = null; retryStep = 0;
    clearInterval(pollTimer); pollTimer = null; renderStatus();
  }
  es.onopen = () => { if (current() && !recoveringSince) connected(); };
  for (const type of ['change','heartbeat','reset']) es.addEventListener(type, event => {
    if (!current()) return;
    try {
      const msg = JSON.parse(event.data);
      if (msg.env !== env) return;
      lastContact = Date.now();
      if (recoveringSince) connected();
      if (type === 'reset') { start(true); return; }
      if (type === 'change') { applyChange(msg); return; }
      if (Number.isFinite(epoch(msg.server_time))) clockOffset = epoch(msg.server_time) - Date.now();
      if (msg.sources) { snap = {...snap,sources:msg.sources}; renderMain({activity:false}); }
      if (msg.head_version && msg.head_version !== version) {
        const [a,n] = versionParts(version), [b,m] = versionParts(msg.head_version);
        if (a !== b) { start(true); return; }
        if (m > n) recover();
      }
    } catch (e) { if (e.status === 410) start(true); else recover(); }
  });
  es.onerror = () => { if (current()) recover(); };
}
function expirySignature() {
  const ids = stalePositions().map(p => p.id);
  if (pageData?.position && quoteExpired(pageData.position)) ids.push('detail');
  for (const p of positionsPage ? pageData?.items || [] : []) if (quoteExpired(p)) ids.push('list-' + p.id);
  return JSON.stringify([ids,gamesStatus()]);
}
function checkTime() {
  renderStatus();
  if (!snap) return;
  if (dayKey(now()) > businessDay() && !dayPending) {
    dayPending = true; renderMain();
    if (connection !== '实时连接') { start(true); return; }
  }
  if (dayPending && connection === '实时连接' && !snapshotTimer && now() > dayStart(dayKey(now())) + 3000) {
    start(true); return;
  }
  if (connection === '实时连接' && Date.now() - lastContact > 30000) recover();
  const expires = expirySignature();
  if (lastExpiry !== expires) { lastExpiry = expires; renderMain({activity:false}); renderPage(); }
}
function metricText(m) {
  const value = metricValue(m);
  if (value !== null) return (m.includes_estimated_fee ? '≈ ' : '') + profit(value);
  if (!m) return '—';
  if (m.status === 'partial' && validNumber(m.known_subtotal)) return '部分可算';
  const code = ['shared_exit','balance_disappeared','balance_adjustment','no_day_baseline','missing_cost','unattributed','stale_quote','no_bid','no_quote','missing_fee','source_gap'].find(k => m.issues?.some(i => i.code === k));
  return code ? label(code) : '暂无法计算';
}
const coverage = m => m?.covered_count != null && m?.total_count != null ? `覆盖 ${m.covered_count}/${m.total_count} 仓` : '';
function metricNote(m) {
  if (!m) return snap ? '等待数据' : '正在读取…';
  const sub = validNumber(m.known_subtotal) ? '可计算部分 ' + profit(m.known_subtotal) : '';
  const reasons = reasonList(m.issues);
  return [sub, m.includes_estimated_fee && m.status !== 'estimated' && '含估算手续费', coverage(m), reasons].filter(Boolean).join(' · ') || '按账户交易范围统计';
}
function renderCards() {
  $('metrics').innerHTML = Object.entries(metrics).map(([k,name]) => {
    const m = summaryMetric(k), value = metricValue(m);
    const tag = value === null ? '' : sideText(value);
    return `<button class="metric ${k === 'today' ? 'primary' : ''}" data-metric="${k}">
      <span class="cardtop"><span class="metricIcon" aria-hidden="true">${symbols[k]}</span>${name}${tag ? '<span class="quality">' + esc(tag) + '</span>' : ''}</span>
      <b class="metricValue ${value === null ? 'word' : 'num'}">${esc(dayPending && k === 'today' ? '正在同步' : metricText(m))}</b>
      <span class="cardbottom"><span class="cardnote">${value !== null ? esc(label(m.status)) + '<br>' : ''}${esc(metricNote(m))}</span><span aria-hidden="true">↗</span></span></button>`;
  }).join('');
}
function renderGames() {
  const games = snap?.live_games || [];
  const status = gamesStatus();
  $('gamesStatus').textContent = status && status !== 'ok' ? '赛程' + label(status) : '';
  $('gameCount').textContent = games.length || status === 'ok' ? games.length : '—';
  if (!games.length) {
    $('games').innerHTML = `<p class="empty">${esc(!snap ? '正在读取赛程…' : status === 'ok' ? '目前没有进行中的比赛' : '暂无法确认进行中的比赛')}</p>`;
    return;
  }
  const staleGames = new Set(stalePositions().map(p => p.game_id));
  $('games').innerHTML = '<div class="gameGrid">' + games.map(g => {
      const m = k => adjusted(g[k], k !== 'realized' && staleGames.has(g.game_id));
      const total = m('total'), value = metricValue(total);
      const matchup = matchTitle(g);
      const subtotal = value === null && validNumber(total?.known_subtotal);
      const shown = subtotal ? (total.includes_estimated_fee ? '≈ ' : '') + profit(total.known_subtotal) : metricText(total);
      const detail = [!g.trade_count && '暂无交易记录', value === null && reasonList(total?.issues)].filter(Boolean).join(' · ');
      const component = (name, metric) => `<div><span>${name}</span><b class="num ${color(metricValue(metric))}">${esc(metricText(metric))}</b></div>`;
      return `<article class="gameCard"><h3>${esc(matchup)}</h3>
        <div class="gameProfit"><span>${subtotal ? '本场盈亏 · 可计算部分' : '本场盈亏'}</span><b class="${value === null && !subtotal ? 'unknownProfit' : 'num ' + color(value ?? total.known_subtotal)}">${esc(shown)}</b></div>
        <div class="gameComponents">${component('已实现',m('realized'))}${component('未实现',m('unrealized'))}</div>
        ${detail ? '<p class="gameIssue">' + esc(detail) + '</p>' : ''}</article>`;
    }).join('') + '</div>';
}
function renderOpen() {
  const all = [...positionMap.values()].sort((a,b) => {
    const va = metricValue(adjusted(a.unrealized,quoteExpired(a))), vb = metricValue(adjusted(b.unrealized,quoteExpired(b)));
    return (va === null) - (vb === null) || Math.abs(Number(vb)) - Math.abs(Number(va)) || a.id.localeCompare(b.id);
  });
  $('posCount').textContent = !snap || (!all.length && sourceState()) ? '—' : all.length;
  $('positions').innerHTML = all.map(p => {
    const m = adjusted(p.unrealized,quoteExpired(p)), val = metricValue(m);
    return `<a href="${esc(detailURL(p.id))}" class="position"><span class="identity"><span class="team">${esc(p.channel ?? '—')}</span>
    <span class="teamtext"><strong>${esc(matchTitle(p))}</strong><span class="sub">${esc(marketSide(p))} · ${esc(positionState(p))}</span></span></span>
    <span class="positionVal"><b class="num ${color(val)}">${esc(metricText(m))}</b><span class="sub">${p.shared_exit ? esc('两通道合计 ' + metricText(adjusted(p.shared_exit.total,quoteExpired(p)))) : esc(qty(p.quantity) + ' 份 · ' + valuationPrice(p))}</span></span></a>`;
  }).join('') || `<p class="empty">${esc(!snap ? '正在读取持仓…' : sourceState() || '当前暂无持仓')}</p>`;
  $('posFoot').textContent = '未实现盈亏 · 按买一或市场已定兑付价估值' + (currency() ? ' / ' + currency() : '');
}
function fee(a) {
  if (a.fee_unit === 'shares') return qty(a.fee) + ' 份';
  if (a.fee_unit === 'cash') return (a.fee_basis === 'estimated_taker' ? '≈ ' : '') + price(a.fee);
  return validNumber(a.fee) ? qty(a.fee) + '（单位待核）' : '—';
}
function tradeRows(items) {
  return items.map(a => `<tr class="trade" data-activity="${esc(a.id)}">
    <td><span class="num time">${esc(stamp(a.time,historyPage))}</span><span class="sub">${esc(label(a.time_kind))}</span></td>
    <td><span class="kind ${a.kind === 'buy' ? 'buy' : a.kind === 'sell' ? 'sell' : 'settle'}">${esc(label(a.kind))}</span></td>
    <td><button type="button" class="gameBtn" aria-label="${esc(matchTitle(a))}，查看交易详情">${esc(matchTitle(a))}</button><span class="sub">${esc(marketSide(a))} <span class="channel">${esc(a.channel)}</span></span></td>
    <td class="amount num">${esc(price(a.price))}</td><td class="amount num">${esc(qty(a.quantity))}</td>
    <td class="amount num deskcol">${esc(price(a.gross))}</td><td class="amount num deskcol">${esc(fee(a))}</td><td class="amount num mobileHide ${color(a.net_cash)}">${esc(netCash(a))}</td>
    <td class="deskcol">${esc(label(a.evidence))}</td></tr>`).join('');
}
function renderTrades() {
  const filter = $('localKind').value;
  const items = home ? (dayPending ? [] : [...todayMap.values()].filter(a => !filter || a.kind === filter).sort((a,b) => epoch(b.time) - epoch(a.time) || b.id.localeCompare(a.id))) : pageData?.items || [];
  $('trades').innerHTML = tradeRows(items);
  const unknown = home ? !snap || dayPending || (sourceState() && !items.length) : !pageData || (sourceState() && !items.length);
  $('tradeCount').textContent = unknown ? '—' : items.length;
  $('empty').hidden = items.length > 0;
  $('empty').textContent = home && dayPending ? '正在同步今日交易' : !snap ? '正在读取交易…' : historyPage && !pageData ? pageError || '正在查询历史…' :
    sourceState() || (home ? filter ? '今天没有此类交易' : '今日暂无交易' : '该范围暂无历史交易');
  $('activityMore').hidden = !historyPage || !pageData?.next_cursor;
  $('activityMore').disabled = !!pageBusy;
  $('refreshHistory').hidden = !historyPage || !historyDirty;
}
function renderMain({activity = true} = {}) {
  renderStatus();
  if (gamesPage) renderGames();
  if (home) {
    renderCards(); renderOpen();
    if (activity) renderTrades();
    $('chartLegend').textContent = '累计盈亏' + (currency() ? ' · ' + currency() : '');
    if (!chartData) drawChart();
  }
  if (activeDialog && $('dialog').open) renderDialog();
}
function grid(items) {
  return '<div class="detailGrid">' + items.map(([key,value]) => `<div><small>${esc(key)}</small><span>${esc(value)}</span></div>`).join('') + '</div>';
}
function issues(list) {
  return (list || []).map(issue => `<p class="issue">${esc(label(issue.code))}${issue.position_id ? ` · <a href="${esc(detailURL(issue.position_id))}">${esc(issue.position_id)}</a>` : ''}${issue.activity_id ? ' · ' + esc(issue.activity_id) : ''}</p>`).join('');
}
function netCash(a) {
  return (validNumber(a.net_cash) && a.fee_basis === 'estimated_taker' ? '≈ ' : '') + formatAmount(a.net_cash,{precise:true,signed:true});
}
function activityDetails(a) {
  const basis = a.fee_basis === 'estimated_taker' ? '按吃单估算 · 成交份额 × 0.05 × 价格 × (1 − 价格)' : a.fee_basis === 'calculated_maker' ? '挂单成交，手续费为零' : validNumber(a.fee) ? '成交记录' : '缺少计算依据';
  return grid([['业务时间（香港）',stamp(a.time)],['时间类型',label(a.time_kind)],['原始业务时间',a.time],['比赛',matchTitle(a)],['比赛编号',a.game_id],
    ['市场 / 方向',marketSide(a)],['通道',a.channel],['动作',label(a.kind)],['份额',qty(a.quantity)],
    [a.evidence === 'aggregate' ? '来源汇总价 / 份' : '成交价 / 份',price(a.price)],['成交 / 兑付额',price(a.gross)],['手续费',fee(a)],['费用依据',basis],['净收付',netCash(a)],
    ['证据',label(a.evidence)],['数据源',label(a.source)],['订单号',a.order_id],['成交号',a.fill_id],['流水号',a.id],['仓位号',a.position_id],
    ['记录于',stamp(a.recorded_at)],['修订于',stamp(a.revised_at)]]) + issues(a.issues);
}
function openDialog(type,id) {
  activeDialog = {type,id}; renderDialog();
  if (!$('dialog').open) $('dialog').showModal();
}
function renderDialog() {
  const {type,id} = activeDialog;
  let title, body;
  if (type === 'sources') {
    title = '数据来源';
    const srcs = [...(snap?.sources || [])];
    if (snap?.games_source) srcs.push(snap.games_source);
    body = grid([['环境',env === 'live' ? '实盘' : '模拟盘'],['账户',snap?.account_label],['覆盖起点',stamp(snap?.coverage_from)],['可用现金',price(snap?.summary?.available_cash)],['数据版本',version]]) +
      srcs.map(s => `<section class="event"><h3>${esc(label(s.name))} · ${esc(label(s.status))}</h3>${grid([['最近成功读取',stamp(s.last_success_at)],['最近业务更新',stamp(s.last_event_at)]])}<p class="subtle">${esc(s.reason || '')}</p></section>`).join('');
  } else if (type === 'metric') {
    title = metrics[id]; const m = summaryMetric(id);
    body = grid([['盈亏',metricText(m)],['数据质量',label(m?.status)],['可计算小计',profit(m?.known_subtotal)],['覆盖仓位',coverage(m) || '—'],
      ['已实现盈亏',metricText(summaryMetric('realized'))],['未实现盈亏',metricText(summaryMetric('unrealized'))],['估值时间',stamp(snap?.summary?.valued_at)],['覆盖起点',stamp(snap?.coverage_from)]]) +
      issues(m?.issues) + `<a class="linkBtn" href="${esc(urlFor('/positions',{view:'history'}))}">逐仓核对 →</a>`;
  } else {
    const a = home ? todayMap.get(id) : pageData?.items?.find(row => row.id === id);
    title = a ? label(a.kind) + '详情' : '交易详情';
    body = a ? activityDetails(a) + (a.position_id ? `<a class="linkBtn" href="${esc(detailURL(a.position_id))}">查看仓位与完整时间线 →</a>` : '<span class="subtle">仓位归属待核</span>') : '<p>该记录已修订或不在当前查询范围。</p>';
  }
  $('dialogTitle').textContent = title; $('dialogBody').innerHTML = body;
}
function queueChart() {
  wantedChart = version;
  if (chartBusy || !snap || !home) return;
  const token = ++chartToken, selected = range, target = wantedChart;
  wantedChart = null; chartBusy = true; chartError = '';
  read('/pnl/history',{range:selected,at_version:target}).then(r => {
    if (token !== chartToken || r.version !== target || wantedChart && wantedChart !== target) return;
    chartData = {...r.data, points:[...r.data.points].sort((a,b) => epoch(a.at) - epoch(b.at))};
    chartSelection = null; drawChart();
  }).catch(e => {
    if (token !== chartToken) return;
    if (e.status === 410) { start(true); return; }
    chartError = '走势暂不可读'; chartData = null; drawChart();
  }).finally(() => {
    if (token !== chartToken) return;
    chartBusy = false; if (wantedChart) queueChart();
  });
}
let plotted = [];
function drawChart() {
  const c = $('chart'), rect = c.getBoundingClientRect(), ctx = c.getContext('2d');
  if (!home || !rect.width || !rect.height || !ctx) return;
  const w = rect.width, h = rect.height, dpr = Math.min(devicePixelRatio || 1,3);
  if (c.width !== Math.round(w*dpr) || c.height !== Math.round(h*dpr)) { c.width = Math.round(w*dpr); c.height = Math.round(h*dpr); }
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
  const items = chartData?.points || [];
  const first = items.find(p => metricValue(p.total) !== null);
  $('chartFoot').textContent = first ? '首个可用点 ' + stamp(first.at) + ' · 香港时间' : '';
  $('chartEmpty').textContent = first ? '' : chartError || (chartData ? '暂无可核实的历史盈亏' : '正在读取走势…');
  $('tip').hidden = true; plotted = [];
  const left = 60, right = w-12, top = 16, bottom = h-24;
  ctx.font = '10px Consolas, monospace'; ctx.lineWidth = 1;
  for (let i=0; i<4; i++) {
    const y = top + (bottom-top)*i/3;
    ctx.beginPath(); ctx.setLineDash([3,5]); ctx.strokeStyle = '#3b493e'; ctx.moveTo(left,y); ctx.lineTo(right,y); ctx.stroke();
  }
  ctx.setLineDash([]);
  if (!first) return;
  let lo = Infinity, hi = -Infinity;
  for (const p of items) if (metricValue(p.total) !== null) {
    const value = Number(p.total.value); lo = Math.min(lo,value); hi = Math.max(hi,value);
  }
  const pad = Math.max((hi-lo)*0.1,Math.abs(hi)*.001,.01);
  lo -= pad; hi += pad;
  const from = Math.min(epoch(chartData.from) || epoch(items[0].at),epoch(items[0].at));
  const to = Math.max(from+1,epoch(chartData.to) || 0,epoch(items.at(-1).at));
  const x = at => left + (epoch(at)-from)/(to-from)*(right-left);
  const y = val => bottom - (val-lo)/(hi-lo)*(bottom-top);
  ctx.fillStyle = '#bac6bb'; ctx.textAlign = 'right';
  for (let i=0; i<4; i++) ctx.fillText((hi-(hi-lo)*i/3).toLocaleString('en-US',{maximumFractionDigits:2}),left-6,top+(bottom-top)*i/3+3);
  const axis = t => range === 'today' ? timeFormat.format(t).slice(0,5) : dayKey(t).slice(5);
  ctx.textAlign = 'left'; ctx.fillText(axis(from),left,h-4);
  ctx.textAlign = 'right'; ctx.fillText(axis(to),right,h-4);
  const gradient = ctx.createLinearGradient(0,top,0,bottom);
  gradient.addColorStop(0,'rgba(249,154,92,.25)'); gradient.addColorStop(1,'rgba(249,154,92,0)');
  let segment = [];
  function drawSegment() {
    if (!segment.length) return;
    ctx.beginPath(); ctx.moveTo(segment[0].x,segment[0].y);
    for (const p of segment.slice(1)) ctx.lineTo(p.x,p.y);
    ctx.strokeStyle = '#ffa367'; ctx.lineWidth = 2; ctx.stroke();
    if (segment.length === 1) { ctx.beginPath(); ctx.arc(segment[0].x,segment[0].y,2.5,0,2*Math.PI); ctx.fillStyle = '#ffa367'; ctx.fill(); }
    else { ctx.lineTo(segment.at(-1).x,bottom); ctx.lineTo(segment[0].x,bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill(); }
    segment = [];
  }
  let previous;
  for (const p of items) {
    const when = epoch(p.at), value = metricValue(p.total);
    if (value === null || (chartData.bucket_seconds && when - previous > chartData.bucket_seconds * 1000 + 1000)) drawSegment();
    const pt = {p,x:x(p.at),y:value === null ? null : y(Number(value))}; plotted.push(pt);
    if (value !== null) segment.push(pt);
    previous = when;
  }
  drawSegment();
  if (chartSelection != null && plotted[chartSelection]) {
    const {p,x:cx,y:cy} = plotted[chartSelection];
    ctx.beginPath(); ctx.strokeStyle = '#829387'; ctx.lineWidth = 1; ctx.moveTo(cx,top); ctx.lineTo(cx,bottom); ctx.stroke();
    if (cy != null) { ctx.beginPath(); ctx.fillStyle = '#ffbe96'; ctx.arc(cx,cy,4,0,2*Math.PI); ctx.fill(); }
    const tip = $('tip');
    tip.textContent = [stamp(p.at),'累计 ' + metricText(p.total),'已实现 ' + metricText(p.realized),'未实现 ' + metricText(p.unrealized)].join('\n');
    tip.hidden = false;
    tip.style.left = Math.max(0,Math.min(cx+9,w-tip.offsetWidth-2))+'px'; tip.style.top = '4px';
  }
}
function updateDateBounds() {
  const max = dateBefore(businessDay()), form = $('historyFilters');
  form.elements.from.max = max; form.elements.to.max = max;
}
function initForms() {
  updateDateBounds();
  const form = $('historyFilters'), yesterday = form.elements.from.max;
  for (const input of form.elements) if (input.name) input.value = params.get(input.name) || (['from','to'].includes(input.name) ? yesterday : '');
  appliedDates = {from:form.elements.from.value,to:form.elements.to.value};
  const positions = $('positionFilters');
  const view = params.get('view') || 'open';
  const sort = {open:'abs_unrealized',today:'abs_today',history:'last_activity'}[view] || 'abs_unrealized';
  for (const input of positions.elements) if (input.name) input.value = params.get(input.name) || (input.name === 'view' ? view : input.name === 'sort' ? sort : '');
}
function queryPageParams() {
  if (detailId) return {};
  const form = historyPage ? $('historyFilters') : $('positionFilters');
  const value = key => params.get(key) || '';
  if (historyPage) {
    const {from,to} = appliedDates;
    return {from:new Date(dayStart(from)).toISOString(),to:new Date(dayStart(to)+dayMs).toISOString(),
      kind:value('kind'),position_id:value('position_id'),channel:value('channel'),game_id:value('game_id')};
  }
  const view = value('view') || form.elements.view.value || 'open';
  return {view,sort:value('sort') || ({open:'abs_unrealized',today:'abs_today',history:'last_activity'}[view]),game_id:value('game_id'),channel:value('channel')};
}
async function refreshPage(append = false) {
  if (home || gamesPage || !snap) return;
  if (pageBusy) {
    if (!append) wantedPage = version;
    return;
  }
  const target = append ? pageVersion : version, token = ++pageToken;
  const path = historyPage ? '/activity' : positionsPage ? '/positions' : '/positions/' + encodeURIComponent(detailId);
  const rows = detailId ? 'timeline' : 'items';
  // Rebuild already expanded position/timeline pages together when a newer version invalidates the cursor.
  const keepCount = append || historyPage ? 0 : pageData?.[rows]?.length || 0;
  pageBusy = true; pageError = ''; wantedPage = null;
  if (!pageData) renderPage();
  try {
    let cursor = append && pageData ? pageData.next_cursor : null, merged;
    do {
      const r = await read(path,{...queryPageParams(),at_version:target,cursor});
      if (token !== pageToken || r.version !== target) return;
      merged = {...r.data,[rows]:[...(merged?.[rows] || (append ? pageData?.[rows] || [] : [])),...r.data[rows]]};
      cursor = merged.next_cursor;
    } while (cursor && merged[rows].length < keepCount);
    if (!wantedPage || wantedPage === target) {
      pageData = merged; pageVersion = target;
    }
  } catch (e) {
    if (token !== pageToken) return;
    if (e.status === 410) {
      pageData = null; pageVersion = null; historyDirty = false; showMessage('列表已更新，正在重新载入第一页。');
      start(true); return;
    }
    pageError = e.status === 404 ? '未找到该仓位' : '读取失败 · ' + e.message;
    if (pageData) showMessage(pageError);
  } finally {
    if (token === pageToken) {
      pageBusy = false; renderPage();
      if (wantedPage && wantedPage !== target) refreshPage();
    }
  }
}
function valuationPrice(p) {
  return p.quote?.status === 'resolved' ? '兑付价 ' + price(p.quote.payout) : '买一 ' + quotePrice(p,'bid');
}
function positionTable(items) {
  return '<div class="tablebox"><table class="ledger"><thead><tr>' +
    ['比赛 / 市场 / 方向','通道','状态','今日贡献','未实现盈亏','已实现盈亏','余仓 / 份','预留 / 份','平均买价 / 份','含费成本 / 份','估值依据 / 份','市值'].map(s=>`<th>${s}</th>`).join('') +
    '</tr></thead><tbody>' + items.map(p => {
      const stale = quoteExpired(p);
      const values = [metricText(adjusted(p.today,stale)),metricText(adjusted(p.unrealized,stale)),metricText(p.realized),qty(p.quantity),qty(p.reserved_quantity),
        price(p.avg_entry_price),price(p.unit_cost),valuationPrice(p),stale ? '—' : price(p.market_value)];
      const group = p.shared_exit ? '<span class="sub">两通道合计 ' + esc(metricText(adjusted(p.shared_exit.total,stale))) + '</span>' : '';
      return `<tr><td><a class="listLink" href="${esc(detailURL(p.id))}">${esc(matchTitle(p))}</a><span class="sub">${esc(marketSide(p))}</span></td><td>${esc(p.channel)}</td><td>${esc(positionState(p))}${group}</td>` +
        values.map(v=>`<td class="num">${esc(v)}</td>`).join('') + '</tr>';
    }).join('') + '</tbody></table></div>';
}
function timelineItem(t, expanded) {
  const a = t.activity;
  return `<details class="event" data-event="${esc(t.id)}" ${expanded.has(t.id) ? 'open' : ''}><summary>${esc(stamp(t.time))} · ${esc(label(t.time_kind))} · <b>${esc(a ? label(a.kind) : label(t.type))}</b> ${esc(t.state ? label(t.state) : '')}</summary>` +
    grid([['原始业务时间',t.time],['订单号',t.order_id],['尝试号',t.attempt_id],['订单限价 / 份',price(t.limit_price)],['原因',t.reason],['核对凭据',t.evidence_ref]]) +
    (a ? activityDetails(a) + grid([['成交后余仓',qty(t.remaining_quantity)],['成交后剩余成本',price(t.remaining_cost)],['本次已实现盈亏',profit(t.realized_change)]]) : '') + '</details>';
}
function renderPage() {
  if (home || gamesPage) return;
  if (historyPage) { renderTrades(); return; }
  const target = positionsPage ? $('positionList') : $('detailContent');
  const more = $(positionsPage ? 'positionMore' : 'timelineMore');
  more.hidden = !pageData?.next_cursor; more.disabled = !!pageBusy;
  if (!pageData) { target.innerHTML = `<div class="empty">${esc(pageError || '正在读取仓位…')}</div>`; return; }
  if (positionsPage) {
    target.innerHTML = pageData.items.length ? positionTable(pageData.items) : `<div class="empty">${esc(sourceState() || '该范围暂无仓位')}</div>`;
    return;
  }
  const p = pageData.position, stale = quoteExpired(p), q = p.quote || {};
  const expanded = new Set([...target.querySelectorAll('details[open]')].map(el => el.dataset.event));
  target.innerHTML = `<section class="detailSection"><h2>${esc(matchTitle(p))}</h2><p class="subtle">${esc([marketSide(p),p.channel && p.channel + ' 通道', positionState(p)].filter(Boolean).join(' · '))}</p>` +
    grid([['比赛编号',p.game_id],['仓位号',p.id],['标的',p.asset_id],['开仓',stamp(p.opened_at)],['最近业务时间',stamp(p.last_activity_at)],['剩余份额',qty(p.quantity)],['待卖预留份额',qty(p.reserved_quantity)],
      ['平均买入价 / 份',price(p.avg_entry_price)],['含费单位成本',price(p.unit_cost)],['剩余成本',price(p.remaining_cost)],['市值',stale ? '—' : price(p.market_value)],
      ['估值依据 / 份',valuationPrice(p)],['卖一 / 份',quotePrice(p,'ask')],['市场状态',label(stale ? 'stale' : q.status)],['估值依据时间',stamp(q.observed_at)],
      ['来源报价时间',stamp(q.source_at)],['报价有效至',stamp(q.valid_until)],['已实现盈亏',metricText(p.realized)],['未实现盈亏',metricText(adjusted(p.unrealized,stale))],['今日贡献',metricText(adjusted(p.today,stale))]]) +
    (p.shared_exit ? '<h3>同方向两个通道合计</h3>' + grid([['总盈亏',metricText(adjusted(p.shared_exit.total,stale))],['剩余份额',qty(p.shared_exit.quantity)],['成交净收付',profit(p.shared_exit.net_cash)]]) : '') +
    issues(p.issues) + (p.before_fees ? '<h3>费前估算</h3>' + grid([['已实现（费前）',profit(p.before_fees.realized)],['未实现（费前）',stale ? '—' : profit(p.before_fees.unrealized)],['总盈亏（费前）',stale ? '—' : profit(p.before_fees.total)]]) : '') + '</section><section class="detailSection"><h2>订单与成交时间线</h2>' +
    (pageData.timeline.map(t => timelineItem(t,expanded)).join('') || '<p class="empty">暂无可核对事件</p>') + '</section>';
}
$('environment').value = env;
$('environment').addEventListener('change', e => {
  env = e.target.value;
  // A position identifier belongs to its environment. Switch at the corresponding list.
  if (detailId) { location.assign(urlFor('/positions')); return; }
  if (env === 'live') params.delete('env'); else params.set('env',env);
  history.replaceState(null,'',route + (params.size ? '?' + params : ''));
  if ($('dialog').open) $('dialog').close();
  setLinks(); start();
});
$('sourceButton').addEventListener('click', () => openDialog('sources'));
$('closeDialog').addEventListener('click', () => $('dialog').close());
$('dialog').addEventListener('close', () => { activeDialog = null; });
$('dialog').addEventListener('click', e => { if (e.target === $('dialog')) $('dialog').close(); });
$('metrics').addEventListener('click', e => {
  const button = e.target.closest('[data-metric]');
  if (!button) return;
  if (button.dataset.metric === 'today') location.assign(urlFor('/positions',{view:'today'}));
  else openDialog('metric',button.dataset.metric);
});
$('trades').addEventListener('click', e => {
  const row = e.target.closest('[data-activity]');
  if (row) openDialog('activity',row.dataset.activity);
});
$('localKind').addEventListener('change',renderTrades);
$('ranges').addEventListener('click', e => {
  const button = e.target.closest('[data-range]');
  if (!button || range === button.dataset.range) return;
  range = button.dataset.range; chartToken++; wantedChart = chartData = chartSelection = null; chartError = ''; chartBusy = false;
  for (const b of $('ranges').children) b.setAttribute('aria-pressed', String(b === button));
  drawChart(); queueChart();
});
$('chart').addEventListener('pointermove', e => {
  if (!plotted.length) return;
  const x = e.clientX - $('chart').getBoundingClientRect().left;
  let lo = 0, hi = plotted.length - 1;
  while (lo < hi) {
    const mid = Math.floor((lo+hi)/2);
    if (plotted[mid].x < x) lo = mid + 1; else hi = mid;
  }
  chartSelection = lo > 0 && x - plotted[lo-1].x < plotted[lo].x - x ? lo-1 : lo;
  drawChart();
});
$('chart').addEventListener('pointerleave', () => { chartSelection = null; drawChart(); });
$('chart').addEventListener('keydown', e => {
  if (!plotted.length || !['ArrowLeft','ArrowRight'].includes(e.key)) return;
  e.preventDefault(); chartSelection = Math.max(0,Math.min(plotted.length-1,(chartSelection ?? -1) + (e.key === 'ArrowLeft' ? -1 : 1)));
  drawChart();
});
$('positionFilters').elements.view.addEventListener('change', e => {
  $('positionFilters').elements.sort.value = {open:'abs_unrealized',today:'abs_today',history:'last_activity'}[e.target.value];
});
for (const form of [$('historyFilters'),$('positionFilters')]) form.addEventListener('submit',e => {
  e.preventDefault();
  if (form === $('historyFilters') && form.elements.from.value > form.elements.to.value) {
    form.elements.to.setCustomValidity('结束日期须在开始日期当天或之后'); form.elements.to.reportValidity(); return;
  }
  location.assign(urlFor(route,Object.fromEntries(new FormData(form))));
});
$('historyFilters').addEventListener('input', () => $('historyFilters').elements.to.setCustomValidity(''));
for (const id of ['activityMore','positionMore','timelineMore']) $(id).addEventListener('click', () => { if (pageData?.next_cursor) refreshPage(true); });
$('refreshHistory').addEventListener('click', () => {
  historyDirty = false; pageData = null; pageVersion = null; pageToken++; pageBusy = false; refreshPage();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && snap) {
    const mark = generation;
    if (dayKey(now()) > businessDay()) { dayPending = true; renderMain(); start(true); return; }
    checkTime();
    catchUp().catch(e => { if (mark === generation) { if (e.status === 410) start(true); else recover(); } });
  }
});
window.addEventListener('online', () => { if (snap) recover(); else start(true); });
window.addEventListener('offline',renderStatus);
window.addEventListener('pagehide', () => clearNetwork());
window.addEventListener('pageshow', e => { if (e.persisted) start(true); });
new ResizeObserver(drawChart).observe($('chart'));
const logo = $('brandBall').getContext('2d');
if (logo) {
  logo.scale(3,3); logo.strokeStyle = '#292a22'; logo.lineWidth = 1.65; logo.lineCap = 'round';
  logo.stroke(new Path2D('M12 2.2a9.8 9.8 0 1 0 .01 0 M3 8.1c5.5 1.6 10.5 7 13.4 12.4 M6.6 19.7C11.5 16.7 16.3 10 17.5 4 M3 14.6C9.5 14.5 13.7 10.2 14.6 2.6 M10.2 21.7c.6-5.1 4.7-9.3 11.3-10'));
}
setInterval(checkTime,1000);
setLinks(); start();
