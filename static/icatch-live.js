class ICatchLivePlayer {
  constructor({ canvas, canvases, status }) {
    this.singleCanvas = canvas || null;
    this.canvases = canvases || (canvas ? [canvas] : []);
    this.status = status;
    this.contexts = this.canvases.map(c => {
      const ctx = c.getContext('2d', { alpha: false, desynchronized: true });
      ctx.imageSmoothingEnabled = true;
      return ctx;
    });
    this.ws = null;
    this.decoders = [];
    this.running = false;
    this.timestamps = [];
    this.frames = [];
    this.gotKeyFrame = [];
    this.lastStatusAt = 0;
    this.channelCount = this.canvases.length || 1;
  }

  setStatus(text, force = false) {
    const now = performance.now();
    if (!force && now - this.lastStatusAt < 1000) return;
    this.lastStatusAt = now;
    if (this.status) this.status.textContent = text;
  }

  stop() {
    this.running = false;
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
    for (const decoder of this.decoders) {
      try { decoder.close(); } catch (_) {}
    }
    this.decoders = [];
    this.setStatus('已停止原生即時播放', true);
  }

  async start({ host, user, password, channel = 1, channels = [channel], quality = 'sub' }) {
    if (!('VideoDecoder' in window)) {
      throw new Error('這個瀏覽器不支援 WebCodecs，請用新版 Chrome / Edge');
    }
    this.stop();

    const wanted = channels.map(Number).filter(ch => ch >= 1 && ch <= 16);
    if (wanted.length === 0) throw new Error('沒有可播放的 channel');
    if (wanted.length > this.canvases.length) throw new Error('播放器畫面數不足');

    this.running = true;
    this.channelCount = wanted.length;
    this.frames = Array(16).fill(0);
    this.timestamps = Array(16).fill(0);
    this.gotKeyFrame = Array(16).fill(false);
    this.decoders = Array(16).fill(null);

    const cleanHost = host.replace(/^https?:\/\//, '').replace(/\/+$/, '');
    const high = ['main', 'high', 'hq', '1'].includes(String(quality).toLowerCase());
    const bits = wanted.reduce((sum, ch) => sum | (1 << (ch - 1)), 0);
    const cmd = `vobits=${bits.toString(16)},pbits=${bits.toString(16)},aobits=0,hq=${high ? 1 : 0}`;
    const auth = 'Basic ' + btoa(`${user}:${password}`);

    wanted.forEach((ch, index) => {
      const canvas = this.canvases[index];
      const ctx = this.contexts[index];
      const channelIndex = ch - 1;
      this.decoders[channelIndex] = this.createDecoder({ canvas, ctx, channel: ch });
    });

    this.setStatus(`連線 DVR WebSocket 中…（${wanted.join(', ')}）`, true);
    this.ws = new WebSocket(`wss://${cleanHost}/streaming`);
    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => this.setStatus('已連線，等待 DVR 初始化…', true);
    this.ws.onerror = () => this.setStatus('❌ WebSocket 連線失敗。若是憑證問題，請先開 DVR HTTPS 頁面允許憑證。', true);
    this.ws.onclose = () => {
      if (this.running) this.setStatus('連線已中斷', true);
      this.running = false;
    };
    this.ws.onmessage = event => {
      if (!this.running) return;
      const buf = event.data;
      if (typeof buf === 'string') return;
      const bytes = new Uint8Array(buf);
      if (bytes.length >= 4 && text4(bytes, 0) === 'wsli') {
        this.ws.send(auth);
        this.ws.send(cmd);
        this.setStatus('已送出登入與 4CH 播放指令，等待關鍵影格…', true);
        return;
      }
      this.processPacket(bytes);
    };
  }

  createDecoder({ canvas, ctx, channel }) {
    const decoder = new VideoDecoder({
      output: frame => {
        // Canvas resizing is expensive on phones. Only resize when resolution changes.
        if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas.width = frame.displayWidth;
          canvas.height = frame.displayHeight;
        }
        ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
        frame.close();
        const idx = channel - 1;
        this.frames[idx] += 1;
        this.updatePlaybackStatus();
      },
      error: err => {
        this.setStatus(`❌ CH${channel} 解碼失敗：${err.message || err}`, true);
      }
    });

    decoder.configure({
      codec: 'avc1.42E01E',
      optimizeForLatency: true,
      avc: { format: 'annexb' }
    });
    return decoder;
  }

  updatePlaybackStatus() {
    const active = this.frames
      .map((count, idx) => count > 0 ? `CH${idx + 1}:${count}` : null)
      .filter(Boolean)
      .join('  ');
    this.setStatus(`▶ 原生 4CH 即時播放中：${active || '等待影格'}`);
  }

  processPacket(bytes) {
    let done = 0;
    while (done + 40 <= bytes.length) {
      const fourcc = text4(bytes, done);
      const dataSize = u32(bytes, done + 24);
      const ch = u32(bytes, done + 28);
      const exSize = u32(bytes, done + 36);
      const frameSize = 40 + exSize + dataSize;
      if (frameSize <= 0 || done + frameSize > bytes.length) break;

      if (fourcc === 'H264' && ch >= 0 && ch < this.decoders.length && this.decoders[ch] && dataSize > 0) {
        const key = done + 56 < bytes.length ? u32(bytes, done + 56) : 0;
        const payload = bytes.slice(done + 40 + exSize, done + frameSize);
        this.decode(ch, payload, key === 1);
      }
      done += frameSize;
    }
  }

  decode(channelIndex, payload, isKey) {
    const decoder = this.decoders[channelIndex];
    if (!decoder || decoder.state !== 'configured') return;

    // A browser decoder must start from a key frame. Dropping early delta frames
    // also prevents a large startup backlog that feels like lag.
    if (isKey) this.gotKeyFrame[channelIndex] = true;
    if (!this.gotKeyFrame[channelIndex]) return;

    // Keep latency low on phones: prefer dropping frames over delayed playback.
    if (decoder.decodeQueueSize > 1 && !isKey) return;
    if (decoder.decodeQueueSize > 4) {
      this.setStatus(`⚠️ CH${channelIndex + 1} 手機解碼跟不上，已丟幀降延遲`);
      return;
    }

    this.timestamps[channelIndex] += 33333; // ~30fps timestamp.
    try {
      decoder.decode(new EncodedVideoChunk({
        type: isKey ? 'key' : 'delta',
        timestamp: this.timestamps[channelIndex],
        data: payload
      }));
    } catch (err) {
      this.setStatus(`❌ CH${channelIndex + 1} 送入解碼器失敗：${err.message || err}`, true);
    }
  }
}

function text4(bytes, offset) {
  return String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3]);
}

function u32(bytes, offset) {
  return (bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16) | (bytes[offset + 3] << 24)) >>> 0;
}

window.ICatchLivePlayer = ICatchLivePlayer;
