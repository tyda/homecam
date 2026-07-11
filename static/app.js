const stamp = () => `t=${Date.now()}`;
const grid = document.querySelector('#grid');
const statusEl = document.querySelector('#status');
const resultEl = document.querySelector('#icatchResult');

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

function renderICatchResult(data) {
  const lines = [];
  lines.push(data.ok ? '✅ 有收到串流' : '⚠️ 沒收到串流');
  lines.push(`Host：${data.host}`);
  lines.push(`畫質：${data.quality}`);
  for (const ch of data.channels || []) {
    const size = ch.width && ch.height ? `${ch.width}x${ch.height}` : '-';
    lines.push(`${ch.ok ? '✅' : '❌'} ${ch.id}：${ch.codec || '-'} ${size}，frames=${ch.frames}`);
  }
  resultEl.textContent = lines.join('\n');
}

async function icatchBody(forceSub = false) {
  const host = document.querySelector('#icatchHost').value.trim();
  const user = document.querySelector('#icatchUser').value.trim() || 'admin';
  const password = document.querySelector('#icatchPassword').value;
  const quality = forceSub ? 'sub' : document.querySelector('#icatchQuality').value;
  if (!host || !password) throw new Error('請輸入 IP / Host 和密碼');
  return { host, user, password, quality };
}

async function testICatch() {
  let body;
  try {
    body = await icatchBody(false);
  } catch (err) {
    resultEl.textContent = err.message;
    return;
  }

  resultEl.textContent = '測試中，約 5～10 秒…';
  try {
    const res = await fetch('/api/icatch/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '測試失敗');
    renderICatchResult(data);
  } catch (err) {
    resultEl.textContent = `❌ ${err.message}`;
  }
}

let liveRunning = false;
let liveObjectUrl = null;
let nativePlayer = null;

async function fetchICatchFrame(body) {
  const res = await fetch('/api/icatch/snapshot/1', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || '抓圖失敗');
  }
  return await res.blob();
}

async function startICatchLive() {
  const img = document.querySelector('#icatchPreview');
  let body;
  try {
    body = await icatchBody(true); // force substream: H264, easier to decode on Vercel.
  } catch (err) {
    resultEl.textContent = err.message;
    return;
  }

  liveRunning = true;
  resultEl.textContent = '播放中… Vercel 會逐張解碼成近即時畫面';
  while (liveRunning) {
    try {
      const blob = await fetchICatchFrame(body);
      if (liveObjectUrl) URL.revokeObjectURL(liveObjectUrl);
      liveObjectUrl = URL.createObjectURL(blob);
      img.src = liveObjectUrl;
      resultEl.textContent = `▶ CH1 播放中：${new Date().toLocaleTimeString()}`;
    } catch (err) {
      resultEl.textContent = `❌ ${err.message}`;
      liveRunning = false;
    }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
}

async function startNativeLive() {
  let body;
  try {
    body = await icatchBody(true);
  } catch (err) {
    resultEl.textContent = err.message;
    return;
  }
  document.querySelector('#icatchPreview').removeAttribute('src');
  nativePlayer = nativePlayer || new window.ICatchLivePlayer({
    canvas: document.querySelector('#nativeCanvas'),
    status: resultEl
  });
  try {
    await nativePlayer.start({ ...body, channel: 1 });
  } catch (err) {
    resultEl.textContent = `❌ ${err.message}`;
  }
}

function stopICatchLive() {
  liveRunning = false;
  if (nativePlayer) nativePlayer.stop();
  resultEl.textContent = '已停止播放';
}

function openDvrLivePage() {
  const host = document.querySelector('#icatchHost').value.trim();
  if (!host) {
    resultEl.textContent = '請先輸入 IP / Host';
    return;
  }
  const cleanHost = host.replace(/^https?:\/\//, '').replace(/\/+$/, '');
  window.open(`https://${cleanHost}/login.html`, '_blank', 'noopener,noreferrer');
  resultEl.textContent = '已開啟 DVR 原廠即時頁；這才是真正連續影像。';
}

document.querySelector('#refreshBtn').addEventListener('click', refresh);
document.querySelector('#icatchTest').addEventListener('click', testICatch);
document.querySelector('#nativeLive').addEventListener('click', startNativeLive);
document.querySelector('#icatchStop').addEventListener('click', stopICatchLive);
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
