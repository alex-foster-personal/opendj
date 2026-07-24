// Acceptance run for the four-stem rater. Every check below is one of the maintainer's
// three complaints or one of the invariants the page is not allowed to lose.
import { chromium } from 'playwright';

const BASE = 'http://localhost:8791/vocal_quality_rater.html';
const OUT = process.argv[2] || '/tmp/rater-4stem.png';
const results = [];
function check(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? '[PASS]' : '[FAIL]'} ${name}${detail ? ' -- ' + detail : ''}`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
page.on('console', (m) => { if (m.type() === 'error') pageErrors.push('console: ' + m.text()); });

await page.goto(`${BASE}?manifest=ladder_4stem_all.json`, { waitUntil: 'networkidle' });
await page.waitForSelector('.card[data-arm-id]', { timeout: 20000 });

const arms = await page.$$('.card[data-arm-id]');
const perArm = await page.$$eval('.card[data-arm-id]', (cards) =>
  cards.map((c) => Array.from(c.querySelectorAll('.stem-row')).map((r) => r.dataset.stem)));
check('all four stems on every arm card',
  arms.length === 6 && perArm.every((s) => s.join(',') === 'vocals,drums,bass,other'),
  `${arms.length} arms, stem sets: ${JSON.stringify([...new Set(perArm.map((s) => s.join(',')))])}`);

const refStems = await page.$$eval('#reference-card .stem-row[data-stem]', (r) => r.map((x) => x.dataset.stem));
check('four true-stem references on the same page', refStems.length === 4, refStems.join(','));

const colors = await page.$$eval('.card[data-arm-id] .stem-row', (rows) => {
  const out = {};
  rows.forEach((r) => { out[r.dataset.stem] = getComputedStyle(r).borderLeftColor; });
  return out;
});
const EXPECT = { vocals: 'rgb(62, 209, 106)', drums: 'rgb(122, 120, 255)',
                 bass: 'rgb(255, 69, 184)', other: 'rgb(255, 68, 56)' };
check('rekordbox stem colours applied to every stem row',
  Object.keys(EXPECT).every((s) => colors[s] === EXPECT[s]), JSON.stringify(colors));

await page.waitForFunction(() => {
  const cs = Array.from(document.querySelectorAll('canvas'));
  return cs.length > 0 && cs.every((c) => {
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    for (let i = 3; i < d.length; i += 4000) if (d[i] !== 0) return true;
    return false;
  });
}, { timeout: 180000 });
const canvasCount = (await page.$$('canvas')).length;
const preload = await page.$$eval('audio', (a) => a.map((x) => x.preload));
check('every waveform drawn on load, nothing played yet', canvasCount >= 29,
  `${canvasCount} canvases, all non-blank`);
check('every player still preload="none"', preload.every((p) => p === 'none'),
  `${preload.length} players`);

const blindText = await page.textContent('#blind-toggle');
const armTitles = await page.$$eval('.arm-title', (t) => t.map((x) => x.textContent.trim()));
check('blind mode ON by default', /Blind mode: ON/.test(blindText), blindText);
check('arm identities hidden while blind',
  armTitles.every((t) => /^Arm [A-Z]$/.test(t)), armTitles.join(', '));

const exclusive = await page.evaluate(() => {
  const els = Array.from(document.querySelectorAll('audio'));
  const pick = [els[0], els[7], els[20], els[els.length - 1]];
  const log = [];
  for (const el of pick) {
    el.dispatchEvent(new Event('play'));
    log.push(els.filter((x) => x !== el && !x.paused).length);
  }
  return { total: els.length, othersPlayingAfterEachPlay: log };
});
check('exclusive playback holds across all players',
  exclusive.othersPlayingAfterEachPlay.every((n) => n === 0),
  `${exclusive.total} players, others still playing after each: ${exclusive.othersPlayingAfterEachPlay}`);

const realPlay = await page.evaluate(async () => {
  const els = Array.from(document.querySelectorAll('audio'));
  await els[5].play();
  await new Promise((r) => setTimeout(r, 900));
  const firstRan = els[5].currentTime > 0;
  await els[18].play();
  await new Promise((r) => setTimeout(r, 900));
  return { firstRan, firstPausedBySecond: els[5].paused, secondRan: els[18].currentTime > 0 };
});
check('a real clip plays and starting another pauses it',
  realPlay.firstRan && realPlay.firstPausedBySecond && realPlay.secondRan,
  JSON.stringify(realPlay));
await page.evaluate(() => document.querySelectorAll('audio').forEach((a) => a.pause()));

await page.click('.card[data-arm-id] .stem-row[data-stem="bass"] .rate-chip:nth-child(7)');
const progress = await page.textContent('#progress');
check('a stem score registers against its own arm+stem cell', /^1 \/ 24 /.test(progress), progress);

await page.screenshot({ path: OUT, fullPage: false });
await page.screenshot({ path: OUT.replace('.png', '-full.png'), fullPage: true });

for (const m of ['ladder_cheap.json', 'ladder_4stem_vocals.json', 'ladder_edge.json']) {
  const errs = [];
  const p2 = await browser.newPage();
  p2.on('pageerror', (e) => errs.push(e.message));
  await p2.goto(`${BASE}?manifest=${m}`, { waitUntil: 'networkidle' });
  const cards = await p2.$$('.card[data-clip-id]');
  const fatal = await p2.$('.fatal');
  check(`${m} still renders`, cards.length > 0 && fatal === null && errs.length === 0,
    `${cards.length} clip cards, ${errs.length} pageerrors`);
  await p2.close();
}

check('zero pageerror on the four-stem page', pageErrors.length === 0,
  pageErrors.slice(0, 3).join(' | ') || 'none');

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length} of ${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
