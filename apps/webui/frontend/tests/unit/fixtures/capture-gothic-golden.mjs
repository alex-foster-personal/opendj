/** Capture pre-B5 gothic block heights for fixture regeneration (issue #11396). */
import { readFileSync } from 'node:fs';

import { loadTypeScriptModule } from '../load-typescript.mjs';

const ROW_W = 1200;
const ROW_H = 410;
const HALF_H = (ROW_H - 10) / 2 - 1;

export async function captureGothicGolden(fixtureUrl) {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	const render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	await render.__test_loadBlockSnap();
	const fx = JSON.parse(readFileSync(fixtureUrl, 'utf8'));
	const d = fx.detail;
	const detail = { low: d.low, mid: d.mid, high: d.high, length: d.low.length };
	const mono = '#cfcfcf';
	const palette = {
		bg: '#000000',
		low: '#111111',
		mid: '#222222',
		high: '#333333',
		mono,
		tick: '#444444',
		cueHotCue: '#35c04f',
		cueLoop: '#e8a13a',
		cueMemory: '#ff3b30',
		cueOutline: '#c8cdd2',
		phrase: '#888888'
	};
	const selfDecoded = { status: 'decoded', reason: null, preview_b64: null, preview_max: null };

	const rowHeights = (beats, snap) => {
		const anlz = {
			stable_id: 'a'.repeat(40),
			points: detail.length,
			waveform: { kind: 'mono', preview: detail, detail },
			beatgrid: { beat_count: beats.length, beats },
			cues: [],
			phrases: [],
			vocals: { status: 'not_analyzed' },
			beatgrid_source: 'rekordbox',
			beatgrid_own_unavailable_reason: null,
			local_waveform: selfDecoded
		};
		const rects = [];
		const base = {
			fillStyle: '',
			fillRect(x, y, w, h) {
				if (this.fillStyle === mono) rects.push({ x, y, w, h });
			},
			fill(path) {
				if (this.fillStyle === mono) rects.push(...path.rects);
			}
		};
		const ctx = new Proxy(base, {
			get: (t, k) => (k in t ? t[k] : () => {}),
			set: (t, k, v) => ((t[k] = v), true)
		});
		render.drawWaveRow(ctx, {
			windowS: 24,
			playheadFrac: 0.5,
			widthCss: ROW_W,
			heightCss: ROW_H,
			positionMs: 12_000,
			durationMs: 24_000,
			anlz,
			palette,
			pitch: 1,
			loop: null,
			waveformDesign: 'blocks',
			blocksSnap: snap
		});
		if (snap) return rects.sort((a, b) => a.x - b.x).map((r) => r.h / 2 / HALF_H);
		const out = new Array(Math.ceil(ROW_W / render.BLOCK_PITCH_PX)).fill(0);
		for (const r of rects) out[r.x / render.BLOCK_PITCH_PX] = r.h / 2 / HALF_H;
		return out.map((v) => Math.round(v * HALF_H) / HALF_H);
	};

	return {
		grid: rowHeights(fx.beats, false),
		noGrid: rowHeights([], false)
	};
}
