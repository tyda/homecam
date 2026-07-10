const stamp = () => `t=${Date.now()}`;
const grid = document.querySelector('#grid');
const statusEl = document.querySelector('#status');

async function loadCameras() {
  const res = await fetch('/api/cameras');
  const data = await res.json();
  const root = document.querySelector('#cameras');
  root.innerHTML = '';
  for (const cam of data.cameras) {
    const card = document.createElement('article');
    card.className = 'camera';
    card.innerHTML = `<img alt="${cam.name}" src="/api/camera/${cam.id}/snapshot?${stamp()}"><h2>${cam.id} · ${cam.name}</h2>`;
    card.addEventListener('click', () => window.open(`/api/camera/${cam.id}/snapshot?${stamp()}`, '_blank'));
    root.appendChild(card);
  }
}

function refresh() {
  grid.src = `/api/grid/snapshot?${stamp()}`;
  document.querySelectorAll('.camera img').forEach(img => {
    const base = img.src.split('?')[0];
    img.src = `${base}?${stamp()}`;
  });
}

document.querySelector('#refreshBtn').addEventListener('click', refresh);
document.querySelector('#sendTelegram').addEventListener('click', async () => {
  statusEl.textContent = '傳送中…';
  try {
    const res = await fetch('/api/telegram/send-grid', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '傳送失敗');
    statusEl.textContent = '已送出 Telegram 四格快照';
  } catch (err) {
    statusEl.textContent = `Telegram 未送出：${err.message}`;
  }
});

loadCameras().then(refresh);
setInterval(refresh, 30000);
