class ICatchLivePlayer {
  constructor({ canvas, status }) {
    this.canvas = canvas;
    this.status = status;
    this.ctx = canvas.getContext('2d');
    this.ws = null;
    this.decoder = null;
    this.running = false;
    this.timestamp = 0;
    this.frames = 0;
  }

  setStatus(text) {
    if (this.status) this.status.textContent = text;
  }

  stop() {
    this.running = false;
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
    if (this.decoder) {
      try { this.decoder.close(); } catch (_) {}
      this.decoder = null;
    }
    this.setStatus('已停止原生即時播放');
  }

  async start({ host, user, password, channel = 1, quality = 'sub' }) {
    if (!('VideoDecoder' in window)) {
      throw new Error('這個瀏覽器不支援 WebCodecs，請用新版 Chrome / Edge');
    }
    this.stop();
    this.running = true;
    this.frames = 0;
    this.timestamp = 0;

    const cleanHost = host.replace(/^https?:\/\//, '').replace(/\/+$/, '');
    const high = ['main', 'high', 'hq', '1'].includes(String(quality).toLowerCase());
    const bit = 1 << (Number(channel) - 1);
    const cmd = `vobits=${bit.toString(16)},pbits=${bit.toString(16)},aobits=0,hq=${high ? 1 : 0}`;
    const auth = 'Basic ' + btoa(`${user}:${password}`);

    this.decoder = new VideoDecoder({
      output: frame => {
        this.canvas.width = frame.displayWidth;
        this.canvas.height = frame.displayHeight;
        this.ctx.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
        frame.close();
        this.frames += 1;
        if (this.frames % 15 === 0) this.setStatus(`▶ 原生即時播放中：${this.frames} frames`);
      },
      error: err => {
        this.setStatus(`❌ 解碼失敗：${err.message || err}`);
        this.stop();
      }
    });

    this.decoder.configure({
      codec: 'avc1.42E01E',
      optimizeForLatency: true,
      avc: { format: 'annexb' }
    });

    this.setStatus('連線 DVR WebSocket 中…');
    this.ws = new WebSocket(`wss://${cleanHost}/streaming`);
    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => this.setStatus('已連線，等待 DVR 初始化…');
    this.ws.onerror = () => this.setStatus('❌ WebSocket 連線失敗。若是憑證問題，請先開 DVR HTTPS 頁面允許憑證。');
    this.ws.onclose = () => {
      if (this.running) this.setStatus('連線已中斷');
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
        this.setStatus('已送出登入與播放指令…');
        return;
      }
      this.processPacket(bytes, Number(channel) - 1);
    };
  }

  processPacket(bytes, wantedChannel) {
    let done = 0;
    while (done + 40 <= bytes.length) {
      const fourcc = text4(bytes, done);
      const dataSize = u32(bytes, done + 24);
      const ch = u32(bytes, done + 28);
      const exSize = u32(bytes, done + 36);
      const frameSize = 40 + exSize + dataSize;
      if (frameSize <= 0 || done + frameSize > bytes.length) break;

      if (fourcc === 'H264' && ch === wantedChannel && dataSize > 0) {
        const key = done + 56 < bytes.length ? u32(bytes, done + 56) : 0;
        const payload = bytes.slice(done + 40 + exSize, done + frameSize);
        this.decode(payload, key === 1);
      }
      done += frameSize;
    }
  }

  decode(payload, isKey) {
    if (!this.decoder || this.decoder.state !== 'configured') return;

    // Keep latency low on phones: if decoding falls behind, drop delta frames
    // and wait for the next key frame instead of building a huge queue.
    if (this.decoder.decodeQueueSize > 2 && !isKey) return;
    if (this.decoder.decodeQueueSize > 8) {
      this.setStatus('⚠️ 手機解碼跟不上，已自動降延遲丟幀');
      return;
    }

    this.timestamp += 33333; // ~30fps timestamp; display is still driven by decoded output.
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: isKey ? 'key' : 'delta',
        timestamp: this.timestamp,
        data: payload
      }));
    } catch (err) {
      this.setStatus(`❌ 送入解碼器失敗：${err.message || err}`);
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
