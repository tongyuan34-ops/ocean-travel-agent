/* 用无头 Edge 打开前端、执行一段页面脚本、截图。
 *
 * 为什么要自己起 Edge：直接 `msedge --headless --screenshot` 在本机会立刻返回，
 * 子进程随父进程退出被回收，文件写不出来（`--headless=old` 同样失败）。
 * 改成 Node 起子进程并**等它活到截图完成**，生命周期就归 Node 管了。
 *
 * 用法（Node 22+，用到内置 WebSocket）：
 *   node scripts/shot.js <url> <out.png> [页面脚本] [宽] [高]
 *
 * 例：
 *   node scripts/shot.js http://127.0.0.1:3001/index.html /tmp/chat.png \
 *     "document.getElementById('startBtn').click()"
 *
 * 第三个参数会在页面里 await 执行，返回值打到 stdout——
 * 截图之外还能顺便断言「元素到底有没有渲染出来」，比肉眼看图可靠。
 */

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9444;

const [, , url, out, action, width, height] = process.argv;
const W = Number(width || 1440);
const H = Number(height || 900);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let seq = 0;
function cdp(ws, method, params) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    const onMessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== id) return;
      ws.removeEventListener('message', onMessage);
      if (msg.error) reject(new Error(method + ': ' + JSON.stringify(msg.error)));
      else resolve(msg.result);
    };
    ws.addEventListener('message', onMessage);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
}

async function waitForCdp(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await res.json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page;
    } catch (e) {
      /* 还没起来 */
    }
    await sleep(300);
  }
  throw new Error('CDP 端口一直没起来');
}

(async () => {
  const profile = path.join(require('os').tmpdir(), 'edge-shot-' + Date.now());
  const child = spawn(
    EDGE,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      `--window-size=${W},${H}`,
      'about:blank',
    ],
    { stdio: 'ignore' }
  );

  try {
    const page = await waitForCdp(20000);
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((r) => ws.addEventListener('open', r, { once: true }));

    await cdp(ws, 'Page.enable');
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Emulation.setDeviceMetricsOverride', {
      width: W,
      height: H,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await cdp(ws, 'Page.navigate', { url });
    await sleep(3000);

    if (action && action !== '1') {
      const res = await cdp(ws, 'Runtime.evaluate', {
        expression: action,
        awaitPromise: true,
        returnByValue: true,
      });
      console.log('action:', JSON.stringify(res.result && res.result.value));
      await sleep(1500);
    }

    const shot = await cdp(ws, 'Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
    console.log('saved', out);
    ws.close();
  } finally {
    child.kill();
  }
})().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
