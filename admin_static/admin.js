let state = null;
let activeFilter = 'all';
let busy = false;
const $ = id => document.getElementById(id);
const number = value => new Intl.NumberFormat().format(value || 0);
const dateKey = offset => new Date(Date.now() - offset * 86400000).toISOString().slice(0, 10);
const metric = (day, name) => state.metrics.find(row => row.day === day && row.name === name)?.value || 0;
const ago = stamp => {
  if (!stamp) return 'No activity yet';
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - stamp));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
};
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function renderChats() {
  const query = $('search').value.toLowerCase();
  const rows = state.chats.filter(chat => {
    const matches = `${chat.title} ${chat.chat_id}`.toLowerCase().includes(query);
    return matches && (activeFilter === 'all' || (activeFilter === 'watching' && chat.watching) ||
      (activeFilter === 'group' && ['group', 'supergroup'].includes(chat.kind)) ||
      (activeFilter === 'private' && chat.kind === 'private'));
  });
  $('chat-rows').replaceChildren();
  for (const chat of rows) {
    const tr = el('tr');
    const name = el('td');
    const identity = el('div', 'chat-name');
    identity.append(el('span', 'chat-icon', chat.kind === 'private' ? '↗' : '◎'));
    const text = el('div');
    text.append(el('span', 'chat-title', chat.title), el('span', 'chat-id', `${chat.kind} · ${chat.chat_id}`));
    identity.append(text); name.append(identity);
    const status = el('td');
    status.append(el('span', `badge${chat.watching ? '' : ' paused'}`, chat.watching ? '● Watching' : chat.watch_paused ? 'Watch paused' : 'Inactive'));
    const accessLabel = {own: 'Own API credentials', promo: 'Owner API · promo', locked: 'Activation required'};
    status.append(el('span', 'cell-sub', accessLabel[chat.access_mode] || 'Activation required'));
    if (chat.access_expires_at) status.append(el('span', 'cell-sub', `Until ${new Date(chat.access_expires_at * 1000).toLocaleDateString()}`));
    const location = el('td', '', chat.watching || chat.watch_paused ? `${chat.lat.toFixed(3)}, ${chat.lon.toFixed(3)}` : '—');
    if (chat.watching || chat.watch_paused) location.append(el('span', 'cell-sub', `${chat.radius} km radius`));
    const usage = el('td', '', `${number(chat.alerts)} alerts`);
    usage.append(el('span', 'cell-sub', `${number(chat.updates)} updates`));
    const activity = el('td', '', ago(chat.last_seen));
    const actions = el('td');
    if (chat.watching || chat.watch_paused) {
      const button = el('button', 'stop', 'Stop watch');
      button.addEventListener('click', () => stopWatch(chat));
      actions.append(button);
    }
    if (chat.access_mode && chat.access_mode !== 'locked') {
      const revoke = el('button', 'stop revoke-access', 'Revoke access');
      revoke.addEventListener('click', async () => {
        if (!confirm(`Remove API access, saved credentials, and the watch for ${chat.title}?`)) return;
        try { await mutate('/api/revoke-access', {chat_id: chat.chat_id}); await refresh(); }
        catch (error) { showError(error); }
      });
      actions.append(revoke);
    }
    tr.append(name, status, location, usage, activity, actions);
    $('chat-rows').append(tr);
  }
  $('empty').hidden = rows.length > 0;
  $('empty').querySelector('h3').textContent = state.chats.length ? 'No matching chats' : 'No chats here yet';
  $('results').textContent = `Showing ${rows.length} of ${state.chats.length} chats`;
}
function render() {
  const today = dateKey(0);
  $('total-chats').textContent = number(state.chats.length);
  $('chat-count').textContent = number(state.chats.length);
  $('active-watches').textContent = number(state.active_watches);
  $('alerts-today').textContent = number(metric(today, 'alerts'));
  $('checks-today').textContent = number(metric(today, 'flight_lookups'));
  $('aircraft-today').textContent = `${number(metric(today, 'aircraft_alerted'))} aircraft in arrival alerts`;
  $('errors-today').textContent = `${number(metric(today, 'flight_errors'))} failed flight checks`;
  $('flight-errors').textContent = number(metric(today, 'flight_errors'));
  $('delivery-errors').textContent = number(metric(today, 'delivery_errors'));
  $('heartbeat').textContent = ago(state.heartbeat);
  const online = state.heartbeat && Date.now() / 1000 - state.heartbeat < 180;
  $('bot-status').textContent = online ? '● Bot is running' : '● Heartbeat delayed';
  $('bot-status').classList.toggle('offline', !online);
  $('updated').textContent = `Updated ${new Date(state.generated_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
  const days = Array.from({length: 7}, (_, index) => dateKey(6 - index));
  const max = Math.max(1, ...days.map(day => metric(day, 'alerts')));
  $('chart').replaceChildren();
  let weeklyUpdates = 0;
  days.forEach(day => {
    const count = metric(day, 'alerts');
    weeklyUpdates += metric(day, 'updates');
    const column = el('div', 'chart-day');
    const track = el('div', 'bar-track');
    // A local SVG keeps proportional chart bars compatible with the strict CSP.
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 40 102'); svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
    svg.setAttribute('preserveAspectRatio', 'none');
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    const height = count ? Math.max(3, count / max * 102) : 1;
    rect.setAttribute('x', '0'); rect.setAttribute('y', String(102 - height));
    rect.setAttribute('width', '40'); rect.setAttribute('height', String(height)); rect.setAttribute('rx', '3');
    rect.setAttribute('fill', day === today ? '#456c4c' : '#c8d9bb');
    svg.append(rect); track.append(svg);
    column.append(el('span', 'chart-value', number(count)), track,
      el('label', '', new Date(`${day}T12:00:00Z`).toLocaleDateString([], {weekday: 'short', timeZone: 'UTC'})));
    $('chart').append(column);
  });
  $('week-updates').textContent = number(weeklyUpdates);
  renderChats();
  renderPromos();
}
function renderPromos() {
  const promos = state.promos || [];
  $('promo-rows').replaceChildren();
  $('promos-empty').hidden = promos.length > 0;
  for (const promo of promos) {
    const row = el('tr');
    const status = !promo.enabled ? 'Revoked' : promo.redeem_until < Date.now() / 1000 ? 'Expired' : promo.uses >= promo.max_uses ? 'Fully used' : 'Available';
    const action = el('td');
    if (promo.enabled) {
      const button = el('button', 'stop', 'Revoke');
      button.addEventListener('click', async () => {
        if (!confirm(`Revoke ${promo.label}? Chats using this invitation will lose access to your API account.`)) return;
        try { await mutate('/api/revoke-promo', {id: promo.id}); await refresh(); }
        catch (error) { showError(error); }
      });
      action.append(button);
    }
    row.append(el('td', '', promo.label), el('td', '', `${promo.uses} / ${promo.max_uses}`),
      el('td', '', new Date(promo.redeem_until * 1000).toLocaleDateString()),
      el('td', '', `${promo.grant_days} days`), el('td', '', status), action);
    $('promo-rows').append(row);
  }
}
async function refresh() {
  if (busy) return;
  busy = true; $('refresh').disabled = true;
  try {
    const response = await fetch('/api/overview', {cache: 'no-store'});
    if (response.status === 401) { location.reload(); return; }
    if (!response.ok) throw new Error('Could not load dashboard. Try refreshing.');
    state = await response.json(); render(); $('error').hidden = true;
  } catch (error) { $('error').textContent = error.message; $('error').hidden = false; }
  finally { busy = false; $('refresh').disabled = false; }
}
async function mutate(path, body) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
  if (response.status === 401) { location.reload(); throw new Error('Please sign in again.'); }
  if (!response.ok) throw new Error(await response.text() || 'The action failed. Refresh and try again.');
  return response.headers.get('content-type')?.includes('application/json') ? response.json() : null;
}
function showError(error) { $('error').textContent = error.message; $('error').hidden = false; }
function stopWatch(chat) {
  $('stop-description').textContent = `Alerts for ${chat.title} will stop.`;
  const dialog = $('stop-dialog');
  dialog.returnValue = 'cancel';
  dialog.onclose = async () => {
    if (dialog.returnValue !== 'confirm') return;
    try { await mutate('/api/stop', {chat_id: chat.chat_id}); await refresh(); }
    catch (error) { showError(error); }
  };
  dialog.showModal();
}
$('search').addEventListener('input', () => { if (state) renderChats(); });
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
  activeFilter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('active', item === button));
  if (state) renderChats();
}));
$('refresh').addEventListener('click', refresh);
$('promo-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!state) return;
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    const result = await mutate('/api/promos', Object.fromEntries(new FormData(event.target)));
    $('promo-code').value = result.code; $('new-promo').hidden = false;
    $('copy-promo').textContent = 'Copy'; await refresh();
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
});
$('copy-promo').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('promo-code').value); $('copy-promo').textContent = 'Copied'; }
  catch { $('promo-code').select(); }
});
$('logout').addEventListener('click', async () => {
  if (!state) return;
  try { await mutate('/logout', {}); location.reload(); } catch (error) { showError(error); }
});
refresh();
setInterval(refresh, 15000);
