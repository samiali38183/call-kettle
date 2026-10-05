/* Real-browser regression for the website call player (Edge/Chromium via playwright-core).
 * Serves marketing/site locally with Range support, clicks every trade tab, plays real audio, pauses, replays,
 * jumps by clicking a message, and checks bubbles never overlap and no separate voice-clip control exists.
 * Usage: node check_call_demo_browser.cjs <path-to-playwright-core> [screenshot-dir]
 */
const http = require('http'), fs = require('fs'), path = require('path'), assert = require('assert');
const { chromium } = require(process.argv[2]);
const shots = process.argv[3] || null;
const ROOT = path.resolve(__dirname, '..', 'site');
const TYPES = { '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.mp3': 'audio/mpeg', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.xml': 'application/xml', '.ico': 'image/x-icon' };
function serve(req, res) {
  let p = decodeURIComponent(req.url.split('?')[0]); if (p.endsWith('/')) p += 'index.html';
  let f = path.join(ROOT, p); if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = fs.existsSync(f + '.html') ? f + '.html' : path.join(ROOT, '404.html');
  const size = fs.statSync(f).size, type = TYPES[path.extname(f)] || 'application/octet-stream', range = req.headers.range;
  if (range) { const m = /bytes=(\d*)-(\d*)/.exec(range), a = m[1] ? +m[1] : 0, b = m[2] ? +m[2] : size - 1; res.writeHead(206, { 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Content-Range': `bytes ${a}-${b}/${size}`, 'Content-Length': b - a + 1 }); fs.createReadStream(f, { start: a, end: b }).pipe(res); }
  else { res.writeHead(200, { 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Content-Length': size }); fs.createReadStream(f).pipe(res); }
}
(async () => {
  const server = http.createServer(serve).listen(0); const port = server.address().port;
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--autoplay-policy=no-user-gesture-required'] });
  const results = [];
  for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 }); const page = await ctx.newPage();
    const errors = []; page.on('pageerror', e => errors.push(String(e))); page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'load' });
    const ui = page.locator('#callui'); await ui.scrollIntoViewIfNeeded();
    assert.equal(await page.locator('.voice-clip').count(), 0, 'no separate voice-clip control');
    assert(!(await page.locator('body').innerText()).includes('AI voice sample'), 'no AI voice sample card');
    const tabs = page.locator('#callui .cu-tab'); const n = await tabs.count(); assert(n >= 8, 'at least 8 trade tabs, got ' + n);
    for (let i = 0; i < n; i++) {
      const label = (await tabs.nth(i).innerText()).trim();
      await tabs.nth(i).click(); await page.waitForTimeout(300);
      assert.equal(await page.locator('#callui .cu-status').innerText(), 'Ready to play', label + ' ready');
      assert((await page.locator('#callui [data-cu-audio]').getAttribute('src')).includes('/assets/call-example-'), label + ' has its own audio');
      await page.locator('#callui [data-cu-play]').click(); await page.waitForTimeout(2600);
      const t1 = await page.evaluate(() => document.querySelector('[data-cu-audio]').currentTime);
      assert(t1 > 1.2, `${label}: audio advanced (${t1})`);
      assert.equal(await page.locator('#callui .cu-status').innerText(), 'Playing example', label + ' playing');
      const msgs = await page.evaluate(() => [...document.querySelectorAll('#callui .msg')].map(e => { const r = e.getBoundingClientRect(); return [r.top, r.bottom, e.scrollWidth <= e.clientWidth + 1]; }));
      assert(msgs.length >= 1, label + ' shows first message');
      for (let k = 1; k < msgs.length; k++) assert(msgs[k][0] >= msgs[k - 1][1] - 1, label + ' bubbles overlap');
      if (i === 0 && shots) await page.locator('#callui').screenshot({ path: path.join(shots, `player-${name}-playing.png`) });
      await page.locator('#callui [data-cu-play]').click(); await page.waitForTimeout(250);
      assert.equal(await page.locator('#callui .cu-status').innerText(), 'Paused', label + ' paused');
      const paused = await page.evaluate(() => document.querySelector('[data-cu-audio]').paused); assert(paused, label + ' audio really paused');
      const tp = await page.evaluate(() => document.querySelector('[data-cu-audio]').currentTime); await page.waitForTimeout(600);
      assert.equal(await page.evaluate(() => document.querySelector('[data-cu-audio]').currentTime), tp, label + ' stays paused');
      await page.locator('#callui [data-cu-replay]').click(); await page.waitForTimeout(700);
      const tr = await page.evaluate(() => document.querySelector('[data-cu-audio]').currentTime); assert(tr < tp && tr < 1.5, `${label}: replay restarts (${tr} vs ${tp})`);
      await page.locator('#callui [data-cu-play]').click(); await page.waitForTimeout(200);
      await page.locator('#callui [data-cu-transcript]').click(); await page.waitForTimeout(200);
      const full = await page.evaluate(() => [document.querySelectorAll('#callui .msg').length, document.querySelector('#callui .cu-result').classList.contains('show')]);
      assert(full[0] >= 9 && full[1], `${label}: full transcript + booking card (${full})`);
      results.push(`${name}:${label}: ok (${msgs.length} bubbles shown at 2.6s)`);
    }
    // jump by clicking a message
    await tabs.nth(0).click(); await page.locator('#callui [data-cu-transcript]').click(); await page.waitForTimeout(200);
    await page.locator('#callui .msg').nth(4).click(); await page.waitForTimeout(900);
    const cue = await page.evaluate(() => { const c = JSON.parse(document.getElementById('calls-data').textContent)[0].audio.cues[4]; return [c, document.querySelector('[data-cu-audio]').currentTime]; });
    assert(cue[1] >= cue[0] && cue[1] < cue[0] + 3, 'clicking a bubble jumps audio ' + cue);
    if (shots) { await page.locator('#callui [data-cu-transcript]').click(); await page.waitForTimeout(200); await page.locator('#callui').screenshot({ path: path.join(shots, `player-${name}-full.png`) }); await page.screenshot({ path: path.join(shots, `home-${name}-top.png`) }); }
    assert.equal(errors.length, 0, 'console/page errors: ' + errors.join(' | '));
    await ctx.close();
  }
  await browser.close(); server.close();
  console.log(results.join('\n')); console.log('PASS real browser: tabs, audio, pause, replay, jump, transcript, no overlap, no separate voice clip');
})().catch(e => { console.error(e); process.exit(1); });
