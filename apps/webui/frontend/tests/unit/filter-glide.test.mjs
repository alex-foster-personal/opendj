// requirement: FXFILT-10611
// [if] FILTER_GLIDE_DOMAIN or FILTER_GLIDE_S differ between constants.ts and mixer.rs [then] the parity test fails
// [if] the glide domain is 'hz' [then] a corner move schedules exactly cancelScheduledValues then setTargetAtTime, as before
// [if] the glide domain is 'octave' [then] the corner follows a per-frame log2 one-pole curve that ends exactly on the target
// [if] an octave glide is retargeted mid-glide [then] the active curve is cancelled and the new curve starts from the recorded curve, not param.value
// [if] LP and HP make a full-travel step in octaves [then] each covers half its octave span within 1 ms of the other
// [if] the web glide replays the Rust flick fixture [then] every stored frame agrees within 1e-6 in both domains
// [if] the stand-in AudioParam keeps a curve that is active at its cancel time [then] the stand-in test fails
// [if] the flick is evaluated after all retargets instead of rendered between them [then] it is not the parity evidence (see the Chromium spec control)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { SpecAudioParam } from './fixtures/spec-audio-param.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const SR = 48000;

let glide;
let constants;
let guards;

before(async () => {
	glide = await loadTypeScriptModule('src/lib/rb/filter-glide.ts');
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
	guards = await loadTypeScriptModule('src/lib/rb/audio-engine-guards.ts');
});

/** First frame at which a step glide has covered half its octave span. */
function halfSpanFrame(from, to) {
	const p = new SpecAudioParam(from, SR);
	glide.glideCorner(p, to, 0, SR, 'octave', constants.FILTER_GLIDE_S);
	p.renderTo(SR);
	const mid = (Math.log2(from) + Math.log2(to)) / 2;
	const dir = Math.sign(Math.log2(to) - mid);
	for (let n = 0; n < SR; n++) if ((Math.log2(p.rendered[n]) - mid) * dir >= 0) return n;
	throw new Error('never reached half span');
}

describe('filter corner glide', () => {
	test('the stand-in AudioParam cancels an active curve as the Web Audio spec does', () => {
		const p = new SpecAudioParam(100, SR);
		p.setValueCurveAtTime([200, 400], 0, 0.01);
		p.renderTo(240);
		const before = p.rendered.slice();
		p.cancelScheduledValues(0.005);
		assert.equal(p.events.length, 0, 'a curve active at the cancel time is removed');
		assert.equal(p.valueAt(0.002), 100, 'the pre-curve value is restored');
		assert.deepEqual(p.rendered, before, 'rendered frames are frozen');
		const q = new SpecAudioParam(100, SR);
		q.setValueCurveAtTime([200, 400], 0, 0.01);
		q.cancelScheduledValues(0.02);
		assert.equal(q.events.length, 1, 'a curve that ended before the cancel time is kept');
		assert.throws(() => q.setValueCurveAtTime([1, 2], 0.005, 0.01), /NotSupportedError/);
	});

	test('the named constants match the Rust engine and ship hz at 10 ms', () => {
		assert.equal(constants.FILTER_GLIDE_DOMAIN, 'hz');
		assert.equal(constants.FILTER_GLIDE_S, 0.01);
		const mixer = readFileSync(`${REPO}apps/audio-engine/src/mixer.rs`, 'utf8');
		const domain = mixer.match(
			/pub const FILTER_GLIDE_DOMAIN: GlideDomain = GlideDomain::(Hz|Octave);/
		);
		const tau = mixer.match(/pub const FILTER_GLIDE_S: f64 = ([0-9.]+);/);
		assert.ok(domain && tau, 'mixer.rs names FILTER_GLIDE_DOMAIN and FILTER_GLIDE_S');
		assert.equal(domain[1].toLowerCase(), constants.FILTER_GLIDE_DOMAIN);
		assert.equal(Number(tau[1]), constants.FILTER_GLIDE_S);
	});

	test('hz mode schedules exactly what the Hz glide always did', () => {
		const p = new SpecAudioParam(20000, SR);
		glide.glideCorner(p, 130, 3.5, SR, 'hz', 0.01);
		assert.deepEqual(p.calls, [
			['cancel', 3.5],
			['target', 130, 3.5, 0.01]
		]);
	});

	test('octave mode is a per-frame log2 one-pole that lands exactly on the target', () => {
		const p = new SpecAudioParam(20000, SR);
		glide.glideCorner(p, 130, 2, SR, 'octave', 0.01);
		const [cancel, curveCall] = p.calls;
		assert.deepEqual(cancel, ['cancel', 2]);
		const [, n, t, duration] = curveCall;
		assert.equal(t, 2);
		assert.equal(duration, (n - 1) / SR, 'one point per frame');
		const curve = p.events[0].curve;
		assert.equal(curve[0], Math.fround(20000));
		assert.equal(curve[n - 1], Math.fround(130));
		const k = 1 - Math.exp(-1 / (0.01 * SR));
		const [l0, lt] = [Math.log2(20000), Math.log2(130)];
		for (let m = 0; m < n; m += 37) {
			const want = lt + (l0 - lt) * (1 - k) ** m;
			assert.ok(Math.abs(Math.log2(curve[m]) - want) < 1e-6, `frame ${m}`);
		}
	});

	test('a retarget mid-glide starts from the recorded curve, not param.value', () => {
		const p = new SpecAudioParam(20000, SR);
		glide.glideCorner(p, 130, 0, SR, 'octave', 0.01);
		p.renderTo(480);
		const mid = glide.octaveGlideCurve(20000, 130, SR, 0.01)[480];
		glide.glideCorner(p, 5000, 480 / SR, SR, 'octave', 0.01);
		p.renderTo(960);
		assert.equal(p.events.length, 1, 'the curve active at the retarget is cancelled');
		assert.equal(p.events[0].curve[0], Math.fround(mid));
		assert.equal(p.rendered[480], Math.fround(mid), 'continuous across the retarget');
		assert.notEqual(Math.fround(mid), Math.fround(p.value));
	});

	test('LP and HP full-travel steps reach half their octave span within 1 ms of each other', () => {
		const lp = halfSpanFrame(20000, 130);
		const hp = halfSpanFrame(30, 6300);
		assert.ok(Math.abs(lp - hp) <= SR / 1000, `LP ${lp} vs HP ${hp}`);
	});

	test('the web glide replays the Rust flick fixture within 1e-6 per stored frame', () => {
		const fx = JSON.parse(
			readFileSync(`${REPO}apps/audio-engine/tests/fixtures/filter-glide-flick.json`, 'utf8')
		);
		assert.equal(fx.glide_s, constants.FILTER_GLIDE_S);
		const center = guards.filterParamsFromKnob(0.5);
		for (const domain of ['hz', 'octave']) {
			for (const g of fx.gestures) {
				const key = g.corner === 'lp' ? 'lpHz' : 'hpHz';
				const p = new SpecAudioParam(center[key], fx.sample_rate);
				for (let i = 0; i < fx.events; i++) {
					p.renderTo(i * fx.event_every);
					const knob = 0.5 + ((g.end - 0.5) * (i + 1)) / fx.events;
					const hz = guards.filterParamsFromKnob(knob)[key];
					glide.glideCorner(
						p,
						hz,
						(i * fx.event_every) / fx.sample_rate,
						fx.sample_rate,
						domain,
						fx.glide_s
					);
				}
				p.renderTo(fx.frames);
				const want = fx.trajectories[domain][g.name];
				let worst = 0;
				want.forEach((w, j) => {
					const got = p.rendered[j * fx.stride];
					worst = Math.max(worst, Math.abs(got - w) / w);
				});
				assert.ok(worst <= 1e-6, `${domain} ${g.name}: worst relative error ${worst}`);
			}
		}
	});

	test('bad inputs throw by name', () => {
		const p = new SpecAudioParam(1000, SR);
		assert.throws(
			() => glide.glideCorner(p, 0, 0, SR, 'octave', 0.01),
			/hz must be finite and > 0/
		);
		assert.throws(() => glide.glideCorner(p, NaN, 0, SR, 'hz', 0.01), /hz must be finite and > 0/);
		assert.throws(
			() => glide.glideCorner(p, 100, 0, SR, 'octave', 0),
			/tauS must be finite and > 0/
		);
		assert.throws(
			() => glide.glideCorner(p, 100, 0, SR, 'cents', 0.01),
			/Unhandled filter glide domain/
		);
	});
});
