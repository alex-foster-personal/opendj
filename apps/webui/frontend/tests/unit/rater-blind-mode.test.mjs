import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createContext, runInContext } from 'node:vm';
import { before, beforeEach, test } from 'node:test';

// H3 - human quality ratings are captured blind.
//
//   "Calibrate by showing me a vocal stem of same track at 1,2,3,...10 quality
//    by your measure and I'll mark them different numbers if I disagree."
//
// The cost of losing this is already on record: scripts/bench/judgement-
// calibration.json's label_provenance_warning says BOTH existing rating rounds
// ran non-blind with the answer visible in the params string, so together they
// are "worth about one round, not two".
//
// The only existing check is scripts/bench/verify_four_stem_rater.mjs, a manual
// Playwright script needing scripts/bench/serve.py live on port 8791. A refactor
// will not run it and none of its check names are in the test inventory.
//
// This runs the rater's REAL script in a vm against a minimal DOM, so the
// assertions are about what the page actually builds, not about its source text.
//
// Regression lines:
// - if a blind arm card carries the arm id, model name or params anywhere in its
//   subtree then the label is on screen and the score measures the label
// - if blind mode stops shuffling then arm order leaks the machine ranking
// - if the saved JSON stops carrying blind_mode then a biased round is
//   indistinguishable from a clean one in scripts/bench/ratings/
// - if the machine-vs-human table renders before every stem is scored then the
//   machine score anchors the remaining ratings
// - if blind_default stops being honoured then a manifest that asked to open
//   blind opens with the answers visible

const RATER = fileURLToPath(
	new URL('../../../../../scripts/bench/vocal_quality_rater.html', import.meta.url)
);

// -------------------------------------------------------------- DOM shim

/** Minimal element: everything the rater's builders touch, and nothing else. */
class El {
	constructor(tagName) {
		this.tagName = tagName.toUpperCase();
		this.children = [];
		this.dataset = {};
		this.style = { setProperty() {}, removeProperty() {} };
		this.attributes = {};
		this.className = '';
		this.textContent = '';
		this.title = '';
		this._listeners = new Map();
	}
	appendChild(child) {
		this.children.push(child);
		return child;
	}
	append(...nodes) {
		for (const node of nodes) this.appendChild(node);
	}
	addEventListener(type, fn) {
		this._listeners.set(type, fn);
	}
	removeEventListener(type) {
		this._listeners.delete(type);
	}
	setAttribute(name, value) {
		this.attributes[name] = String(value);
	}
	getAttribute(name) {
		return this.attributes[name] ?? null;
	}
	getContext() {
		return new Proxy(
			{},
			{ get: (_t, prop) => (prop === 'canvas' ? this : () => undefined) }
		);
	}
	getBoundingClientRect() {
		return { left: 0, top: 0, width: 100, height: 20 };
	}
	querySelector() {
		return null;
	}
	querySelectorAll() {
		return [];
	}
	/**
	 * Every string this subtree puts in front of the rater's eyes: rendered
	 * text, hover titles, placeholders and field values.
	 *
	 * Deliberately NOT dataset / src - see the machine-readable test below.
	 */
	visibleText() {
		const parts = [
			this.textContent ?? '',
			this.title ?? '',
			this.placeholder ?? '',
			this.value === undefined ? '' : String(this.value),
			this.innerHTML ?? ''
		];
		for (const child of this.children) {
			parts.push(typeof child.visibleText === 'function' ? child.visibleText() : String(child.text ?? ''));
		}
		return parts.join('  ');
	}
	/** What a devtools inspector would see, but the eye would not. */
	machineText() {
		const parts = [
			...Object.values(this.dataset).map(String),
			...Object.values(this.attributes),
			this.src === undefined ? '' : String(this.src)
		];
		for (const child of this.children) {
			if (typeof child.machineText === 'function') parts.push(child.machineText());
		}
		return parts.join('  ');
	}
}

class TextNode {
	constructor(text) {
		this.text = String(text);
		this.children = [];
	}
	visibleText() {
		return this.text;
	}
}

function fakeDocument() {
	const byId = new Map();
	return {
		title: '',
		documentElement: new El('html'),
		body: new El('body'),
		createElement: (tag) => new El(tag),
		createTextNode: (text) => new TextNode(text),
		getElementById: (id) => {
			if (!byId.has(id)) byId.set(id, new El('div'));
			return byId.get(id);
		},
		addEventListener: () => {}
	};
}

function fakeStorage() {
	const map = new Map();
	return {
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => map.set(key, String(value)),
		removeItem: (key) => map.delete(key),
		clear: () => map.clear()
	};
}

// ----------------------------------------------------------- load the rater

/** The rater's script, minus its boot() call, plus an epilogue that hands the
 * test the module-scope bindings a script does not put on globalThis. */
function raterSource() {
	const html = readFileSync(RATER, 'utf8');
	const open = html.indexOf('<script>');
	const close = html.indexOf('</script>', open);
	assert.ok(open >= 0 && close > open, 'the rater has no inline script - it was restructured');
	const script = html.slice(open + '<script>'.length, close);
	assert.ok(script.includes('\nboot();'), 'boot() is gone - find the new entry point');
	return (
		script.replace(/\nboot\(\);/, '\n') +
		'\nglobalThis.__rater = { state, LETTERS, resolveBlind, shuffled, armCard, ' +
		'fillFourStemSummary, buildFourStemPayload, buildPayload, rankedByMachine, ' +
		'isFourStem, cellKey, extremesFirst, renderFourStem };\n'
	);
}

let source;
let rater;
let storage;

const STEMS = [
	{ stem: 'vocals', reference: 'true_vocals.wav', mixture_floor_si_sdr: -5 },
	{ stem: 'drums', reference: 'true_drums.wav', mixture_floor_si_sdr: -6 }
];

function arm(id, params, machine) {
	return {
		id,
		params,
		machine_score: machine,
		s_per_stem_minute: 3.5,
		stems: Object.fromEntries(
			STEMS.map((g) => [g.stem, { file: `${id}_${g.stem}.wav`, si_sdr: machine }])
		)
	};
}

const ARMS = [
	arm('htdemucs_ft_shifts2', 'model=htdemucs_ft shifts=2', 9),
	arm('mdx_extra_q', 'model=mdx_extra_q shifts=0', 4),
	arm('roformer2_bs', 'model=roformer2 overlap=0.25', 7)
];

const MANIFEST = { track: 'demo-track', mode: 'four-stem', blind_default: true, stems: STEMS, arms: ARMS };

before(() => {
	source = raterSource();
});

/** A fresh rater instance with the manifest already loaded, as boot() would. */
function bootRater({ blindDefault = true, ratings = {} } = {}) {
	storage = fakeStorage();
	const context = createContext({
		console,
		document: fakeDocument(),
		window: { location: { search: '' }, addEventListener: () => {} },
		localStorage: storage,
		fetch: async () => {
			throw new Error('the rater must not fetch during a unit test');
		},
		performance: { now: () => 0 },
		setTimeout,
		clearTimeout,
		setInterval,
		clearInterval,
		Math,
		JSON,
		Date,
		URLSearchParams
	});
	context.globalThis = context;
	runInContext(source, context);
	const api = context.__rater;
	const manifest = { ...MANIFEST, blind_default: blindDefault };
	api.state.manifest = manifest;
	api.state.ratings = ratings;
	api.state.focus = {};
	api.state.blind = api.resolveBlind(manifest);
	api.state.ranked = api.rankedByMachine(manifest.arms);
	return api;
}

beforeEach(() => {
	rater = bootRater();
});

/** The blind display order, with the letters the page assigns. */
function blindOrder(api) {
	return api
		.shuffled(api.state.ranked)
		.map((a, i) => Object.assign({}, a, { _letter: api.LETTERS[i] }));
}

// ========================================= 1. no identity reaches the DOM

test('a blind arm card puts no arm id, model name or preset anywhere in its subtree', () => {
	assert.equal(rater.state.blind, true, 'a blind_default manifest did not open blind');

	for (const card of blindOrder(rater).map((a) => rater.armCard(a))) {
		const visible = card.visibleText();
		for (const a of ARMS) {
			assert.ok(
				!visible.includes(a.id),
				`arm id "${a.id}" is on screen in blind mode - the score would measure the label`
			);
			assert.ok(
				!visible.includes(a.params),
				`the params string "${a.params}" is on screen - this is EXACTLY how both existing ` +
					'rating rounds got polluted'
			);
			for (const token of a.params.split(/[\s=]+/).filter((t) => t.length > 4)) {
				assert.ok(!visible.includes(token), `the model token "${token}" leaked into the card`);
			}
		}
		assert.match(visible, /Arm [A-Z]/, 'a blind card must still be identifiable as "Arm X"');
	}
});

test('a blind arm card withholds the machine rank and every machine number', () => {
	for (const card of blindOrder(rater).map((a) => rater.armCard(a))) {
		const visible = card.visibleText();
		assert.ok(!/BEST|WORST/.test(visible), 'the machine ranking badge is visible in blind mode');
		assert.ok(!/Machine rank/.test(visible), 'the machine rank leaked through a hover title');
		// The acronym appears in the copy EXPLAINING what is hidden; a formatted
		// number next to it is the actual leak.
		assert.ok(
			!/SI-SDR\s+-?\d/.test(visible),
			`an SI-SDR reading is on screen and will anchor the rating: ${visible.slice(0, 300)}`
		);
		assert.ok(!/\d+\.\d\d dB/.test(visible), 'a dB machine measurement is on screen');
		assert.ok(!/machine\s+\d/.test(visible), 'the machine quality score is on screen');
		assert.ok(!/cost\s+\d/.test(visible), 'the cost figure identifies the arm by its speed');
	}
});

test('the identity that survives blind mode is machine-readable only, and is recorded', () => {
	// HONEST GUARD, in the style of the H18 note: the requirement says "anywhere
	// in the DOM", and the card genuinely does still carry the arm id in
	// data-arm-id / data-cell and in the audio src (which is the file path, so it
	// cannot simply be blanked without an opaque id map). None of it is readable
	// without opening devtools, so blind mode holds for a human rater - but the
	// letter of the requirement is not met and this is NOT a silent pass.
	const card = rater.armCard(blindOrder(rater)[0]);
	const leaked = ARMS.filter((a) => card.machineText().includes(a.id));

	assert.equal(
		leaked.length,
		1,
		'exactly one arm id should appear in this card\'s machine-readable attributes; ' +
			`got ${leaked.length}, so either the leak was closed (update this test and the ` +
			'scoreboard) or a card now carries OTHER arms\' ids too'
	);
	assert.ok(
		!card.visibleText().includes(leaked[0].id),
		'the machine-readable id has surfaced into readable text - blind mode is broken'
	);
});

test('with blind mode OFF the identities are deliberately shown', () => {
	// The inverse case matters: a test that passes because nothing renders at all
	// would not notice the blind branch being deleted.
	const open = bootRater({ blindDefault: false });
	assert.equal(open.state.blind, false);

	const visible = open
		.extremesFirst(open.state.ranked)
		.map((a) => open.armCard(a).visibleText())
		.join(' ');
	assert.ok(visible.includes('htdemucs_ft_shifts2'), 'a non-blind card should name its arm');
	assert.ok(visible.includes('model=htdemucs_ft shifts=2'), 'a non-blind card should show params');
});

// ========================================= 2. blind order is shuffled

test('blind mode shuffles the arms so the order does not leak the ranking', () => {
	const ranked = rater.state.ranked.map((a) => a.id);
	const seen = new Set();
	for (let i = 0; i < 200; i++) seen.add(rater.shuffled(rater.state.ranked).map((a) => a.id).join('|'));

	assert.ok(
		seen.size > 1,
		'shuffled() returns one fixed order - arm order is a channel for the machine ranking'
	);
	assert.ok(
		seen.size >= 3,
		`only ${seen.size} distinct orders over 200 draws for 3 arms - the shuffle is degenerate`
	);
	for (const order of seen) {
		assert.equal(order.split('|').length, ranked.length, 'the shuffle dropped or duplicated an arm');
		assert.deepEqual(
			order.split('|').slice().sort(),
			ranked.slice().sort(),
			'the shuffle changed the arm set'
		);
	}
});

test('the blind render actually USES the shuffle, and the sighted one does not', () => {
	// Guards the call site, not just shuffled() itself: blind mode could keep a
	// perfectly good shuffle function and stop calling it.
	const orders = new Set();
	for (let i = 0; i < 60; i++) {
		rater.renderFourStem();
		orders.add(rater.state.order.map((a) => a.id).join('|'));
		assert.ok(
			rater.state.order.every((a) => typeof a._letter === 'string' && a._letter.length > 0),
			'a blind arm has no anonymous letter to be scored under'
		);
	}
	assert.ok(orders.size > 1, 'the blind render lays the arms out in machine-score order');

	const open = bootRater({ blindDefault: false });
	open.renderFourStem();
	assert.deepEqual(
		open.state.order.map((a) => a.id),
		open.extremesFirst(open.state.ranked).map((a) => a.id),
		'the sighted render should be deliberate best-then-worst order, not shuffled'
	);
});

test('blind letters are positional, not tied to an arm', () => {
	const letterFor = new Map();
	for (let i = 0; i < 60; i++) {
		for (const a of blindOrder(rater)) {
			if (!letterFor.has(a.id)) letterFor.set(a.id, new Set());
			letterFor.get(a.id).add(a._letter);
		}
	}
	for (const [id, letters] of letterFor) {
		assert.ok(letters.size > 1, `arm ${id} always gets letter ${[...letters]} - the letter IS the id`);
	}
});

// ========================================= 3. the saved file records the mode

test('a rating saved while identities were VISIBLE is stamped blind_mode false', () => {
	const open = bootRater({ blindDefault: false });
	const payload = open.buildFourStemPayload();

	assert.equal(
		payload.blind_mode,
		false,
		'a biased round is now indistinguishable from a clean one in scripts/bench/ratings/'
	);
});

test('a rating saved blind is stamped blind_mode true, and the stamp follows the toggle', () => {
	assert.equal(rater.buildFourStemPayload().blind_mode, true);

	rater.state.blind = false;
	assert.equal(
		rater.buildFourStemPayload().blind_mode,
		false,
		'the stamp is hard-coded rather than reporting the mode the round actually ran in'
	);
});

test('the two-arm (non four-stem) payload carries the same stamp', () => {
	const open = bootRater({ blindDefault: false });
	open.state.manifest = { ...MANIFEST, mode: 'clips', clips: ARMS };
	open.state.ranked = open.rankedByMachine(ARMS);
	assert.equal(open.buildPayload().blind_mode, false, 'the clips payload lost its blind stamp');
	open.state.blind = true;
	assert.equal(open.buildPayload().blind_mode, true);
});

// ========================================= 4. the comparison stays withheld

test('blind mode withholds the machine-vs-human table until every stem is scored', () => {
	const body = new El('div');
	rater.fillFourStemSummary(body);

	const visible = body.visibleText();
	assert.match(visible, /still to go/, 'the comparison rendered with nothing scored yet');
	assert.ok(!/SI-SDR/.test(visible), 'the machine score is on screen and will anchor the ratings');
	for (const a of ARMS) {
		assert.ok(!visible.includes(a.id), `arm id ${a.id} leaked through the summary table`);
	}
});

test('one unrated stem is still enough to keep the comparison hidden', () => {
	const ratings = {};
	for (const a of ARMS) for (const g of STEMS) ratings[rater.cellKey(a.id, g.stem)] = { human_score: 7 };
	// Take exactly one back.
	delete ratings[rater.cellKey(ARMS[2].id, STEMS[1].stem)];

	const partial = bootRater({ ratings });
	const body = new El('div');
	partial.fillFourStemSummary(body);

	assert.match(
		body.visibleText(),
		/1 of 6 stem scores still to go/,
		'the table unlocked with a stem unrated - the machine score anchors the last rating'
	);
});

test('the comparison unlocks once every stem of every arm is scored', () => {
	const ratings = {};
	for (const a of ARMS) for (const g of STEMS) ratings[rater.cellKey(a.id, g.stem)] = { human_score: 7 };

	const complete = bootRater({ ratings });
	const body = new El('div');
	complete.fillFourStemSummary(body);

	const visible = body.visibleText();
	assert.ok(!/still to go/.test(visible), 'the comparison never unlocks, so blind mode is a dead end');
	assert.match(visible, /SI-SDR|si_sdr|mean/i, 'the unlocked table shows no machine comparison');
});

// ========================================= 5. the manifest asks, the rater obeys

test('a manifest with blind_default true opens blind', () => {
	assert.equal(bootRater({ blindDefault: true }).state.blind, true);
});

test('a manifest with no preference opens sighted unless the global toggle says otherwise', () => {
	const open = bootRater({ blindDefault: false });
	assert.equal(open.state.blind, false);

	storage.setItem('vqr:blind', '1');
	assert.equal(
		open.resolveBlind({ ...MANIFEST, blind_default: false }),
		true,
		'the global blind toggle is ignored'
	);
});

test('a per-manifest choice always beats the file default', () => {
	// the maintainer turned blind mode OFF on this track; the file must not turn it back on.
	storage.setItem('vqr:blind:demo-track', '0');
	assert.equal(
		rater.resolveBlind({ ...MANIFEST, blind_default: true }),
		false,
		'the manifest overrode a choice the rater made on this very track'
	);
	storage.setItem('vqr:blind:demo-track', '1');
	assert.equal(rater.resolveBlind({ ...MANIFEST, blind_default: false }), true);
});
