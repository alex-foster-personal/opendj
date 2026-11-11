// requirement: CHROME-19, CHROME-20, UICONTRAST-6037
// [if] the Light skin or light theme is on [then] the jog readout, CUE/Play, the sunken wells (search, voice command, loop readout, strip waveform, explainer popovers), the posture button and the Camelot key text all clear AA on the surface they are drawn on, [else stop].
// [if] Default or Gothic is on [then] every one of those hooks resolves to the value it rendered before (the use-site fallback), so those skins do not change, [else stop].
// [if] the light theme or the Light skin is on [then] the stem tag letters, stem chips and stems status take --rb-stem-ink and clear AA on every row and deck surface, [else stop].
// [if] the Light skin is on [then] the app shell (footer build stamp, sync chip, JOBS, Settings) uses the light shell tokens, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) => readFileSync(`src/${rel}`, 'utf8');
const THEME = read('lib/rb/theme.css');
const APP_CSS = read('app.css');

const LIGHT_SKIN = "html[data-skin='light'] .perf-root";
const LIGHT_THEME = "html[data-theme='light'] .perf-root";
const GOTHIC = "html[data-skin='mono-dev'] .perf-root";

/** Every hook, the files that read it, and the value those reads fall back to
 * (what Default and Gothic rendered before the hook existed). */
const HOOKS = {
	'rb-jog-text': { fallback: 'var(--rb-text)', files: ['lib/components/rb/deck/JogDial.svelte'] },
	'rb-jog-text-dim': { fallback: 'var(--rb-text-dim)', files: ['lib/components/rb/deck/JogDial.svelte'] },
	'rb-round-face': {
		fallback: 'radial-gradient(circle at 50% 35%, #262b33, #101318 75%)',
		files: ['lib/components/rb/deck/TransportCluster.svelte']
	},
	'rb-well': {
		fallback: /^#(0a0c0f|080a0d|11151a)$/,
		files: [
			'lib/components/rb/browser/SearchBox.svelte',
			'lib/components/rb/CommandEntry.svelte',
			'lib/components/rb/deck/LoopCluster.svelte',
			'lib/components/rb/deck/StripWaveform.svelte',
			'lib/components/rb/deck/ControlExplainer.svelte',
			'lib/components/rb/browser/AutoPlayExplainer.svelte',
			'lib/components/rb/browser/AddTrackSearch.svelte',
			'lib/components/rb/AnalysisSourceMenu.svelte',
			'lib/components/rb/FeedbackPanel.svelte'
			// FeedbackPinCard and FeedbackPinDraftBubble are mounted by the root
			// layout, outside .perf-root, where no --rb-* hook resolves. They
			// read the app shell tokens instead (PGMIX-7692d,
			// pgmix-04-feedback-pin-shell-tokens.test.mjs).
		]
	},
	'rb-strip-pos': { fallback: '#ff2d2d', files: ['lib/components/rb/deck/StripWaveform.svelte'] },
	'rb-strip-pos-glow': { fallback: 'rgba(255, 45, 45, 0.95)', files: ['lib/components/rb/deck/StripWaveform.svelte'] },
	'rb-text-muted': { fallback: '#aaa', files: ['lib/components/rb/AppPostureChip.svelte'] },
	'rb-key-lightness-major': { fallback: '62%', files: ['lib/rb/camelot-color.ts'] },
	'rb-key-lightness-minor': { fallback: '55%', files: ['lib/rb/camelot-color.ts'] },
	'rb-knob-ring': { fallback: '#0b0d10', files: ['lib/components/rb/mixer/Knob.svelte'] },
	'rb-knob-cap': { fallback: '#23272f', files: ['lib/components/rb/mixer/Knob.svelte'] },
	'rb-knob-cap-edge': { fallback: '#101318', files: ['lib/components/rb/mixer/Knob.svelte'] },
	'rb-knob-indicator-white': { fallback: '#f0f2f5', files: ['lib/components/rb/mixer/Knob.svelte'] },
	'rb-knob-takeover': { fallback: '#f2b84b', files: ['lib/components/rb/mixer/Knob.svelte'] },
	'rb-knob-stem-tint': { fallback: '42%', files: ['lib/components/rb/mixer/Knob.svelte'] },
	// UICONTRAST-6037: ink on the stem tags, stem chips and the stems status.
	'rb-stem-ink': {
		fallback: /^(#cfefff|#e4f5d8|#ffe7c8|var\(--chip-color\)|var\(--rb-blue, #4aa3ff\)|var\(--rb-text\))$/,
		files: ['lib/components/rb/browser/StemTags.svelte', 'lib/components/rb/deck/StemRow.svelte']
	}
};

/** sRGB color-mix(in srgb, fg p, bg) as the browser computes it, for a pair measured on its real fill. */
function mixHex(fg, bg, p) {
	const ch = (h, i) => parseInt(h.slice(1 + 2 * i, 3 + 2 * i), 16);
	return '#' + [0, 1, 2].map((i) => Math.round(ch(fg, i) * p + ch(bg, i) * (1 - p)).toString(16).padStart(2, '0')).join('');
}

/** All `--name: value;` declarations (any value) in the block whose selector line ends with `selector {`. */
function block(css, selector) {
	const start = css.indexOf(`${selector} {`);
	assert.ok(start !== -1, `no \`${selector} {}\` block`);
	const body = css.slice(start, css.indexOf('\n}', start));
	return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

let cc;
before(async () => {
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
});

test('if a hook use site lost its fallback then Default and Gothic would change with it', () => {
	for (const [name, { fallback, files }] of Object.entries(HOOKS)) {
		for (const file of files) {
			const src = read(file);
			const uses = [...src.matchAll(new RegExp(`var\\(--${name},\\s*((?:[^()]|\\([^()]*(?:\\([^()]*\\))?[^()]*\\))+)\\)`, 'g'))];
			assert.ok(uses.length > 0, `${file} no longer reads --${name}`);
			for (const [, fb] of uses) {
				if (fallback instanceof RegExp) assert.match(fb.trim(), fallback, `${file} --${name} fallback`);
				else assert.equal(fb.trim(), fallback, `${file} --${name} fallback`);
			}
		}
	}
	// Default (base .perf-root) defines none of them, so every read takes its fallback.
	const base = block(THEME, '\n.perf-root');
	assert.deepEqual(Object.keys(HOOKS).filter((h) => h in base), []);
});

test('if a stem tag, chip or status ignores --rb-stem-ink then the light faces paint the dark-skin ink', () => {
	const rule = (src, sel) => src.slice(src.indexOf(`${sel} {`), src.indexOf('}', src.indexOf(`${sel} {`)));
	const tags = read('lib/components/rb/browser/StemTags.svelte');
	for (const [g, ink] of [['V', '#cfefff'], ['I', '#e4f5d8'], ['D', '#ffe7c8']]) {
		assert.match(rule(tags, `.tag.on[data-g='${g}']`), new RegExp(`color: var\\(--rb-stem-ink, ${ink}\\);`), `tag ${g}`);
	}
	assert.match(rule(tags, '.meta'), /color: var\(--rb-text, #c8cdd2\);/);
	const row = read('lib/components/rb/deck/StemRow.svelte');
	assert.match(rule(row, '.chip'), /color: var\(--rb-stem-ink, var\(--chip-color\)\);/);
	assert.match(rule(row, '.status.busy'), /color: var\(--rb-stem-ink, var\(--rb-blue, #4aa3ff\)\);/);
	// The action label's dark-skin ink is the skin text color (its blue border
	// carries the action cue), so that is the fallback Default and Gothic keep.
	assert.match(rule(row, '.status.action'), /color: var\(--rb-stem-ink, var\(--rb-text\)\);/);
});

test('if Gothic does not reset the hooks then Gothic on the light theme gets light wells under light ink', () => {
	const gothic = block(THEME, GOTHIC);
	for (const name of Object.keys(HOOKS)) assert.equal(gothic[name], 'initial', `mono-dev --${name}`);
	// Gothic must come after the light-theme block so its reset wins at equal specificity.
	assert.ok(THEME.indexOf(`${GOTHIC} {`) > THEME.indexOf(`${LIGHT_THEME} {`));
});

for (const selector of [LIGHT_SKIN, LIGHT_THEME]) {
	test(`if ${selector} misses a hook or a hooked pair falls under AA then that surface is unreadable`, () => {
		const decl = block(THEME, selector);
		for (const name of Object.keys(HOOKS)) assert.ok(name in decl, `${selector} does not set --${name}`);
		const t = cc.parseColorTokens(THEME, selector);
		const pair = (fg, bg, floor, label) => {
			const r = cc.contrastRatio(fg, bg);
			assert.ok(r >= floor, `${label}: ${fg} on ${bg} is ${r.toFixed(2)}:1, under ${floor}:1`);
		};
		// The stopped jog face is the #14171d literal in JogDial.svelte in every skin.
		pair(t['rb-jog-text'], '#14171d', 4.5, 'jog BPM/pitch');
		pair(t['rb-jog-text-dim'], '#14171d', 4.5, 'jog range');
		// CUE/Play face: both gradient stops under the button's ink and its lit accent.
		// The Light skin paints it flat --rb-panel (Gothic's section 2, shared).
		const stops =
			decl['rb-round-face'] === 'var(--rb-panel)'
				? [t['rb-panel']]
				: decl['rb-round-face'].match(/#[0-9a-f]{6}/gi);
		assert.ok(stops?.length >= 1, `unreadable round face ${decl['rb-round-face']}`);
		for (const stop of stops) {
			pair(t['rb-text'], stop, 4.5, 'CUE label / Play glyph');
			pair(t['rb-accent'], stop, 4.5, 'playing Play glyph');
		}
		pair(t['rb-text'], t['rb-well'], 4.5, 'typed search / popover text');
		pair(t['rb-text-dim'], t['rb-well'], 4.5, 'LIST mode / strip label');
		pair(t['rb-strip-pos'], t['rb-well'], 3, 'strip playhead');
		pair(t['rb-text-muted'], t['rb-panel'], 4.5, 'idle posture button');
		pair(t['rb-accent'], t['rb-knob-cap'], 3, 'knob indicator');
		pair(t['rb-knob-indicator-white'], t['rb-knob-cap'], 3, 'knob centre indicator');
		pair(t['rb-knob-cap-edge'], t['rb-knob-ring'], 3, 'knob cap edge');
		pair(t['rb-knob-takeover'], t['rb-knob-cap'], 3, 'MIDI pickup ghost on the cap');
		pair(t['rb-knob-takeover'], t['rb-knob-ring'], 3, 'MIDI takeover ring');
		// The warn and alarm caps blend 18% / 22% of their color into the cap (Knob.svelte).
		const warn = t['rb-knob-warn'] ?? t['rb-orange'];
		const alarm = t['rb-knob-alarm'] ?? t['rb-red'];
		pair(warn, mixHex(warn, t['rb-knob-cap'], 0.18), 3, 'warn indicator on its tinted cap');
		pair(alarm, mixHex(alarm, t['rb-knob-cap'], 0.22), 3, 'alarm indicator on its tinted cap');
		// Stem-accented EQ knobs: the indicator on the cap as Knob.svelte blends it
		// (accent at --rb-knob-stem-tint into the cap), the 8px label on the panel.
		const tint = parseFloat(decl['rb-knob-stem-tint']) / 100;
		for (const stem of ['vocal', 'instrumental', 'drums', 'bass', 'other']) {
			const accent = t[`rb-stem-${stem}`];
			pair(accent, mixHex(accent, t['rb-knob-cap'], tint), 3, `${stem} stem knob indicator on its tinted cap`);
			pair(t[`rb-stem-${stem}`], t['rb-panel'], 4.5, `${stem} stem knob label`);
		}
		// UICONTRAST-6037: stem tag letters on their 25% fill (StemTags.svelte), the
		// stem chips on their 15% fill (StemRow.svelte) and the stems status, on
		// every row and deck surface they sit on.
		const ink = decl['rb-stem-ink'] === 'var(--rb-text)' ? t['rb-text'] : t['rb-stem-ink'];
		assert.ok(ink, `${selector} --rb-stem-ink is ${decl['rb-stem-ink']}`);
		for (const surface of ['rb-bg', 'rb-panel', 'rb-panel-raised', 'rb-select']) {
			pair(ink, t[surface], 4.5, `stems status on --${surface}`);
			for (const stem of ['vocal', 'instrumental', 'drums', 'bass', 'other']) {
				const accent = t[`rb-stem-${stem}`];
				pair(ink, mixHex(accent, t[surface], 0.25), 4.5, `${stem} stem tag on --${surface}`);
				pair(ink, mixHex(accent, t[surface], 0.15), 4.5, `${stem} stem chip on --${surface}`);
			}
		}
	});
}

test('if the light key lightness is too high then some Camelot keys fall under AA on light rows', async () => {
	const { camelotKeyColor } = await loadTypeScriptModule('src/lib/rb/camelot-color.ts');
	const light = block(THEME, LIGHT_SKIN);
	const t = cc.parseColorTokens(THEME, LIGHT_SKIN);
	const rows = [t['rb-panel'], t['rb-bg'], t['rb-panel-raised'], t['rb-select']];
	const hslToHex = (h, s, l) => {
		const k = (n) => (n + h / 30) % 12;
		const a = s * Math.min(l, 1 - l);
		const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
		return '#' + [f(0), f(8), f(4)].map((v) => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
	};
	let checked = 0;
	for (let n = 1; n <= 12; n += 1) {
		for (const mode of ['A', 'B']) {
			const css = camelotKeyColor(`${n}${mode}`);
			const m = css.match(/^hsl\((\d+) (\d+)% var\(--rb-key-lightness-(major|minor), (\d+)%\)\)$/);
			assert.ok(m, `unexpected key color ${css}`);
			assert.equal(m[3], mode === 'B' ? 'major' : 'minor');
			const l = parseFloat(light[`rb-key-lightness-${m[3]}`]) / 100;
			const hex = hslToHex(Number(m[1]), Number(m[2]) / 100, l);
			for (const row of rows) {
				const r = cc.contrastRatio(hex, row);
				assert.ok(r >= 4.5, `${n}${mode} ${hex} on ${row} is ${r.toFixed(2)}:1`);
				checked += 1;
			}
		}
	}
	assert.equal(checked, 24 * rows.length);
});

test('if the Light skin does not select the light shell then the footer, sync chip and JOBS draw dark-theme ink on light panels', () => {
	const at = APP_CSS.indexOf("html[data-theme='light'] {");
	assert.ok(at !== -1, 'light shell block moved');
	const head = APP_CSS.slice(APP_CSS.lastIndexOf('*/', at) + 2, at);
	assert.match(head, /html\[data-skin='light'\],\s*$/, "app.css light shell block must also select html[data-skin='light']");
	const shell = cc.parseColorTokens(APP_CSS, "html[data-theme='light']");
	const panel = cc.parseColorTokens(THEME, LIGHT_SKIN)['rb-panel'];
	// The footer and top bar sit on the Light skin's panel and read these shell tokens.
	for (const token of ['fg', 'muted', 'accent']) {
		const r = cc.contrastRatio(shell[token], panel);
		assert.ok(r >= 4.5, `--${token} on the Light panel is ${r.toFixed(2)}:1`);
	}
});

test('if the loop restart overlay keeps its dark literal then its glyph vanishes on the Light well', () => {
	const src = read('lib/components/rb/deck/LoopCluster.svelte');
	const rule = src.slice(src.indexOf('.restart-btn {'), src.indexOf('}', src.indexOf('.restart-btn {')));
	// 75% of the old #080a0d literal is the old rgba(8, 10, 13, 0.75), so Default and Gothic do not move.
	assert.match(rule, /background: color-mix\(in srgb, var\(--rb-well, #080a0d\) 75%, transparent\);/);
});

// CHROME-21: the Light skin is Gothic flipped.
test('if the Light skin does not share Gothic\'s shape overrides and mono look then it is not Gothic flipped', async () => {
	for (const rule of ['.rb-knob, .rb-fader-thumb, .slot, .round, .pad)', '.round {', '.rb-lit-button.lit {', '.rb-fader-track {', '.rb-fader-thumb {']) {
		assert.ok(
			THEME.includes(`html:is([data-skin='mono-dev'], [data-skin='light']) .perf-root ${rule.startsWith('.rb-knob') ? ':is(button, ' : ''}${rule}`),
			`section 2 rule ${rule} is not shared with the Light skin`
		);
	}
	const gothic = block(THEME, GOTHIC);
	const light = block(THEME, LIGHT_SKIN);
	for (const name of ['rb-font', 'rb-fs-browser', 'rb-fs-label', 'rb-waverow-h', 'rb-status-dot-filter', 'rb-quality-filter', 'rb-artwork-filter', 'rb-accent-glow']) {
		assert.equal(light[name], gothic[name], `--${name} should match Gothic`);
	}
	// Flipped: Gothic's ground is darker than its text, Light's is lighter.
	const t = cc.parseColorTokens(THEME, LIGHT_SKIN);
	const g = cc.parseColorTokens(THEME, GOTHIC);
	assert.ok(cc.relativeLuminance(g['rb-bg']) < cc.relativeLuminance(g['rb-text']));
	assert.ok(cc.relativeLuminance(t['rb-bg']) > cc.relativeLuminance(t['rb-text']));
	// Grayscale chrome: every chrome token is a neutral gray, as in Gothic.
	for (const name of ['rb-bg', 'rb-panel', 'rb-panel-raised', 'rb-border', 'rb-text', 'rb-text-dim', 'rb-accent', 'rb-select']) {
		const hex = t[name].slice(1);
		assert.ok(hex.slice(0, 2) === hex.slice(2, 4) && hex.slice(2, 4) === hex.slice(4, 6), `--${name} ${t[name]} is not a gray`);
	}
	const { SKIN_WAVE_LOOK } = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	assert.deepEqual({ ...SKIN_WAVE_LOOK.light }, { ...SKIN_WAVE_LOOK['mono-dev'] });
});

// CHROME-21: Auto gives Light Gothic's mono look, but an explicit Waveform
// colors choice still shows, in its light hues (Codex on #5812).
test('if the Light skin pins the waveform hues then an explicit Rekordbox or Legacy choice has no effect', async () => {
	const { resolveWaveBandColors } = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
	const skinBlock = block(THEME, LIGHT_SKIN);
	const effective = {
		rekordbox: skinBlock,
		mono: { ...skinBlock, ...block(THEME, "html[data-skin='light'][data-wave-palette='mono'] .perf-root") },
		legacy: { ...skinBlock, ...block(THEME, "html[data-skin='light'][data-wave-palette='legacy'] .perf-root") },
		blue: { ...skinBlock, ...block(THEME, "html[data-skin='light'][data-wave-palette='blue'] .perf-root") }
	};
	for (const [choice, decl] of Object.entries(effective)) {
		const want = resolveWaveBandColors('light', choice);
		for (const band of ['low', 'mid', 'high', 'mono', 'vocal']) {
			assert.equal(decl[`rb-wave-${band}`], want[band], `Light ${choice} ${band}`);
		}
	}
	// Equal specificity with the theme's light palette blocks, so the skin's must come later.
	for (const choice of ['mono', 'legacy', 'blue']) {
		assert.ok(
			THEME.indexOf(`html[data-skin='light'][data-wave-palette='${choice}'] .perf-root {`) >
				THEME.indexOf(`html[data-theme='light'][data-wave-palette='${choice}'] .perf-root {`),
			`${choice}: the Light skin palette block must follow the light theme's`
		);
	}
});

test('if a knob lacks the 2px --rb-focus-ring outline then keyboard focus on a knob is invisible (A11Y-11403)', () => {
	const knob = read('lib/components/rb/mixer/Knob.svelte');
	const rule = knob.slice(knob.indexOf('.knob:focus-visible {'), knob.indexOf('}', knob.indexOf('.knob:focus-visible {')));
	assert.match(rule, /filter: drop-shadow\(0 0 3px var\(--rb-accent-glow\)\);/);
	assert.match(rule, /outline: 2px solid var\(--rb-focus-ring\);/);
	for (const selector of [GOTHIC, LIGHT_SKIN]) {
		const decl = block(THEME, selector);
		assert.equal(decl['rb-accent-glow'], 'transparent', `${selector} glow`);
		assert.ok(decl['rb-focus-ring'], `${selector} declares no --rb-focus-ring`);
	}
	// Every takeover stroke reads the hook (a bare amber is 1.4:1 on the Light cap).
	const style = knob.slice(knob.indexOf('<style'));
	assert.deepEqual(style.replace(/var\(--rb-knob-takeover, #f2b84b\)/g, '').match(/#f2b84b/gi) ?? [], []);
	// Default declares the ring too.
	assert.ok(block(THEME, '\n.perf-root')['rb-focus-ring']);
});

// CHROME-21: the strip and preview canvases pick light or dark band hues from
// the skin, not the raw theme (Light is reached from Gothic with the theme still dark).
test('if the strip canvases take the raw theme then Light on the dark theme paints dark-scheme bands on a white well', async () => {
	const { effectiveWaveScheme } = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	assert.equal(effectiveWaveScheme('dark', 'light'), 'light');
	assert.equal(effectiveWaveScheme('light', 'light'), 'light');
	assert.equal(effectiveWaveScheme('light', 'mono-dev'), 'dark', 'Gothic is dark chrome on either theme');
	assert.equal(effectiveWaveScheme('dark', 'mono-dev'), 'dark');
	// Control: the Default skin still follows the theme.
	assert.equal(effectiveWaveScheme('light', 'default'), 'light');
	assert.equal(effectiveWaveScheme('dark', 'default'), 'dark');
	for (const file of ['lib/components/rb/deck/StripWaveform.svelte', 'lib/components/rb/browser/PreviewStrip.svelte']) {
		const src = read(file);
		assert.match(src, /resolveStripBandColors\(effectiveWaveScheme\(uiPrefs\.theme, uiPrefs\.ui_skin\),/, file);
		assert.doesNotMatch(src, /resolveStripBandColors\(uiPrefs\.theme/, file);
	}
});
