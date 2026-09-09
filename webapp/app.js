const tg = window.Telegram?.WebApp;
const state = {
  month: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  selectedDate: new Date().toISOString().slice(0, 10),
  services: [],
  slots: [],
  selectedTimes: [],
};

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor('#f7f8f3');
  tg.setBackgroundColor('#f7f8f3');
}

const initData = tg?.initData || '';
const headers = { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json' };
const $ = (id) => document.getElementById(id);

function showToast(text) {
  const toast = $('toast');
  toast.textContent = text;
  toast.classList.add('visible');
  setTimeout(() => toast.classList.remove('visible'), 3000);
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить действие');
  return data;
}

function monthKey() { return state.month.toISOString().slice(0, 7); }
function formatDate(date) { return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }); }
function formatTime(value) { return value.slice(11, 16); }

function renderCalendar() {
  const grid = $('calendar-grid');
  grid.innerHTML = '';
  $('month-label').textContent = state.month.toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' });
  const firstDay = new Date(state.month.getFullYear(), state.month.getMonth(), 1);
  const offset = (firstDay.getDay() + 6) % 7;
  const days = new Date(state.month.getFullYear(), state.month.getMonth() + 1, 0).getDate();
  for (let i = 0; i < offset; i += 1) grid.insertAdjacentHTML('beforeend', '<div class="calendar-day empty"></div>');
  for (let day = 1; day <= days; day += 1) {
    const date = new Date(state.month.getFullYear(), state.month.getMonth(), day);
    const key = date.toISOString().slice(0, 10);
    const daySlots = state.slots.filter((slot) => slot.starts_at.startsWith(key));
    const button = document.createElement('button');
    button.className = `calendar-day ${key === state.selectedDate ? 'selected' : ''} ${key === new Date().toISOString().slice(0, 10) ? 'today' : ''}`;
    button.innerHTML = `<span class="number">${day}</span>${daySlots.length ? '<span class="dot"></span>' : ''}`;
    button.addEventListener('click', () => { state.selectedDate = key; renderCalendar(); renderDay(); });
    grid.appendChild(button);
  }
}

function renderDay() {
  $('selected-date-label').textContent = formatDate(new Date(`${state.selectedDate}T12:00:00`));
  const list = $('day-slots');
  const slots = state.slots.filter((slot) => slot.starts_at.startsWith(state.selectedDate));
  list.innerHTML = '';
  if (!slots.length) {
    list.innerHTML = '<div class="empty-state">Свободных окон нет. Добавьте первое окно для этого дня.</div>';
    return;
  }
  slots.forEach((slot) => {
    const card = document.createElement('article');
    card.className = `slot-card ${slot.client_id ? 'slot-booked' : ''}`;
    card.innerHTML = `<div><div class="slot-time">${formatTime(slot.starts_at)}</div><div class="slot-meta">${slot.service_name} · ${slot.client_id ? `Клиент: ${slot.client_name}` : 'Свободно'}</div></div>${slot.client_id ? '<span class="slot-meta">Занято</span>' : `<button class="delete-button" data-id="${slot.id}">Удалить</button>`}`;
    card.querySelector('.delete-button')?.addEventListener('click', () => deleteSlot(slot.id));
    list.appendChild(card);
  });
}

function renderServices() {
  $('service-list').innerHTML = state.services.map((service) => `<div class="service-row"><div><strong>${service.name}</strong><div class="service-detail">${service.duration_minutes} минут</div></div><strong>${service.price_rubles ? `${service.price_rubles} ₽` : 'Цена не указана'}</strong></div>`).join('');
}

async function load() {
  try {
    const bootstrap = await api('/api/bootstrap');
    state.services = bootstrap.services;
    $('master-name').textContent = bootstrap.master.name;
    const slots = await api(`/api/slots?month=${monthKey()}`);
    state.slots = slots.slots;
    renderServices(); renderCalendar(); renderDay();
  } catch (error) { showToast(error.message); }
}

function openSlotModal() {
  $('slot-modal').classList.remove('hidden');
  $('slot-date').value = state.selectedDate;
  $('slot-service').innerHTML = state.services.map((service) => `<option value="${service.id}">${service.name} · ${service.duration_minutes} мин</option>`).join('');
  state.selectedTimes = []; renderSelectedTimes();
}
function renderSelectedTimes() { $('selected-times').innerHTML = state.selectedTimes.map((time) => `<button class="chip" data-time="${time}">${time} ×</button>`).join(''); $('selected-times').querySelectorAll('button').forEach((button) => button.addEventListener('click', () => { state.selectedTimes = state.selectedTimes.filter((time) => time !== button.dataset.time); renderSelectedTimes(); })); }

$('slot-time').addEventListener('change', (event) => { if (event.target.value && !state.selectedTimes.includes(event.target.value)) state.selectedTimes.push(event.target.value); event.target.value = ''; renderSelectedTimes(); });
$('add-slot-button').addEventListener('click', openSlotModal);
$('save-slot-button').addEventListener('click', async () => {
  if (!state.selectedTimes.length) return showToast('Добавьте хотя бы одно время');
  try {
    const result = await api('/api/slots', { method: 'POST', body: JSON.stringify({ date: $('slot-date').value, service_id: Number($('slot-service').value), times: state.selectedTimes }) });
    $('slot-modal').classList.add('hidden');
    showToast(result.conflicts.length ? `Добавлено: ${result.created.length}. Конфликтов: ${result.conflicts.join(', ')}` : `Добавлено окон: ${result.created.length}`);
    await load();
  } catch (error) { showToast(error.message); }
});

async function deleteSlot(id) { if (!confirm('Удалить свободное окно?')) return; try { await api(`/api/slots/${id}`, { method: 'DELETE' }); showToast('Окно удалено'); await load(); } catch (error) { showToast(error.message); } }
$('add-service-button').addEventListener('click', () => $('service-modal').classList.remove('hidden'));
$('save-service-button').addEventListener('click', async () => { try { await api('/api/services', { method: 'POST', body: JSON.stringify({ name: $('service-name').value, duration_minutes: Number($('service-duration').value), price_rubles: Number($('service-price').value) }) }); $('service-modal').classList.add('hidden'); showToast('Услуга добавлена'); await load(); } catch (error) { showToast(error.message); } });
$('previous-month').addEventListener('click', async () => { state.month = new Date(state.month.getFullYear(), state.month.getMonth() - 1, 1); await load(); });
$('next-month').addEventListener('click', async () => { state.month = new Date(state.month.getFullYear(), state.month.getMonth() + 1, 1); await load(); });
$('refresh-button').addEventListener('click', load);
document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => $(button.dataset.close).classList.add('hidden')));
load();
