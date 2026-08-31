/**
 * DESK-005 WKWebView spike: criterion registry, verdict recording and evidence
 * rendering. Pure data and pure functions only, so the operator-facing harness
 * and the unit suite share one definition of what the spike measures.
 *
 * Invariant authority is AGENTS.md ("/performance architecture") and
 * .planning/REQUIREMENTS.md (LATENCY-01 to LATENCY-03). This file records WHAT
 * is measured and how a verdict is spelled; it never decides an invariant on
 * its own. It is also the criterion set of record: the lane-A-era
 * docs/quality/wkwebview-spike-protocol.md that first published WKV-01 to
 * WKV-10 never reached main, so this registry is the only published copy.
 *
 * Lane-A vocabulary kept verbatim because recorded evidence cites it, with its
 * meaning on main: `ADR-0005` was the desktop-host decision (settled on main --
 * apps/desktop is the Tauri 2 shell), `ADR-0006` was the evidence taxonomy
 * (PASS / FAIL / UNAVAILABLE, no overwrites -- enforced by the functions
 * below), and `phase_4_entry_gate` is simply the gate this document opens or
 * holds shut. On main that gate is the packaged-decode verdict tracked in
 * docs/perf/RESOURCES.md and in the dated log at
 * .agents/skills/odj-latency-and-performance-loops/SKILL.md.
 *
 * Nothing here observes WKWebView. Every `expectation` string below is an
 * expectation to be tested by a human at a Mac, not an observed fact.
 */

/** The only verdicts an evidence row may carry (ADR-0006 evidence taxonomy). */
export const VERDICTS = ['PASS', 'FAIL', 'UNAVAILABLE'];

/**
 * How a criterion is exercised.
 * - `auto`: the harness drives it end to end inside the WKWebView.
 * - `human-action`: a physical act (unplug, sleep, click) the harness cannot do.
 * - `human-audition`: a listening judgement, recorded alongside a capture.
 */
export const AUTOMATION_CLASSES = ['auto', 'human-action', 'human-audition'];

/**
 * What a failure of this criterion means for the plan.
 * - `host-disqualifying`: argues the shipping WKWebView host cannot carry the
 *   product, which on main is the trigger for the native-audio-engine bet
 *   (perf-queue B3) rather than more tuning inside the current host.
 * - `tuning`: a bounded defect to fix inside the current host choice.
 */
export const FAILURE_CLASSES = ['host-disqualifying', 'tuning'];

/**
 * One row per measured criterion. WKV-01 to WKV-10 keep the IDs the lane-A
 * protocol note first published; WKV-11 to WKV-13 add the repo's own
 * direct-manipulation and one-read-model invariants from AGENTS.md. IDs are
 * stable: never renumber a row, because recorded evidence cites these ids.
 */
export const SPIKE_CRITERIA = [
	{
		id: 'WKV-01',
		title: 'AudioWorklet plus signalsmith-stretch 1.3.2 constructs',
		automation: 'auto',
		failure_class: 'host-disqualifying',
		measurement:
			'Load one real analyzed track on deck 1 and read processor_error plus the resolved stretch module URL.',
		pass_criterion:
			'Processor constructs with no timeout and no processor_error; the executed module is the serialized worklet shipped by signalsmith-stretch at exactly 1.3.2, not a bundler-rewritten copy.',
		expectation:
			'AudioWorklet is expected to be available in WKWebView on current macOS; verify, do not assume (source: no observed run exists, and the packaged app has logged two Signalsmith schedule timeouts under a WKWebView user agent -- see the dated perf skill log).',
		human_step: null,
		fallback: null
	},
	{
		id: 'WKV-02',
		title: 'Presentation-clock semantics of getOutputTimestamp',
		automation: 'auto',
		failure_class: 'host-disqualifying',
		measurement:
			'Sample transport_clock and captured context time across warmup, playback and seeks; count rejected, non-finite, negative and regressing values.',
		pass_criterion:
			'contextTime 0 during warmup never publishes audible or a moving position_ms; positive context time is treated as presentation truth; no rejected value leaks into published state.',
		expectation:
			'AudioContext.getOutputTimestamp is expected to be implemented in WKWebView; its warmup behavior is unknown to this document and is exactly what the spike measures.',
		human_step: null,
		fallback: null
	},
	{
		id: 'WKV-03',
		title: 'Activation mode auto presents audio with no gesture',
		automation: 'auto',
		failure_class: 'tuning',
		measurement:
			'Cold start with VITE_PERFORMANCE_AUDIO_ACTIVATION=auto, dispatch play without any user gesture, then read presented state.',
		pass_criterion:
			'A deck reaches audible with presented_revision equal to desired_revision and the loopback capture is not silent, with no gesture and no synthetic gesture injection.',
		expectation:
			'WKWebView autoplay policy for Web Audio is NOT known here. Chromium grants Web Audio autoplay even under user-gesture-required (AGENTS.md); whether WKWebView does is an expectation to be tested.',
		human_step: null,
		fallback:
			'If auto activation is denied, record FAIL with the exact denial signal and continue with WKV-04; a host-level gesture requirement is a product decision, not a silent workaround.'
	},
	{
		id: 'WKV-04',
		title: 'Activation mode require-gesture gates until one real click',
		automation: 'human-action',
		failure_class: 'tuning',
		measurement:
			'Cold start with VITE_PERFORMANCE_AUDIO_ACTIVATION=require-gesture; read state before the click, then click the activation gate once and read again.',
		pass_criterion:
			'No audible deck and no running context before the gesture; after one real click the queue-idle gate releases and real audio presents.',
		expectation:
			'The application activation seam is explicit and fail-fast (AGENTS.md); the host policy underneath it is unverified on WKWebView.',
		human_step: 'Click the activation gate once, with a real pointer, when the harness prompts.',
		fallback: null
	},
	{
		id: 'WKV-05',
		title: 'Four-deck simultaneous real decoded audio',
		automation: 'auto',
		failure_class: 'host-disqualifying',
		measurement:
			'Load four real analyzed library tracks, play all four, and sample state for a recorded soak window.',
		pass_criterion:
			'Four decks decode and publish audible with matching revisions, zero processor_error, no unexplained transport_pending latch, and the capture shows a four-deck program.',
		expectation: null,
		human_step: null,
		fallback:
			'If the library exposes fewer than four playable analyzed tracks, record UNAVAILABLE naming the shortfall; do not repeat one track across decks and call it four-deck load.'
	},
	{
		id: 'WKV-06',
		title: 'Explicit output device selection',
		automation: 'auto',
		failure_class: 'host-disqualifying',
		measurement:
			'Dispatch headphone_outputs_refresh, headphone_output_acquire and headphone_output_select through the production dispatcher and read the resulting headphone state.',
		pass_criterion:
			'A non-default output is selectable and audio follows it. A missing host API is recorded as FAIL with the exact API name; it is not waivable.',
		expectation:
			'setSinkId and enumerateDevices support in WKWebView is unverified here; the engine device path is the only surface the spike is allowed to use.',
		human_step:
			'Confirm by ear which physical output carries audio after selection, and record it.',
		fallback:
			'With only one output device present, record UNAVAILABLE naming the missing second device rather than PASS from a one-device enumeration.'
	},
	{
		id: 'WKV-07',
		title: 'Default-device change during playback',
		automation: 'human-action',
		failure_class: 'host-disqualifying',
		measurement:
			'While a deck is audible, change the macOS default output; the harness keeps sampling and timestamps the recovery.',
		pass_criterion:
			'Audio continues or recovers on the new device within a recorded bound; the presentation clock does not regress; no silent state still publishes audible.',
		expectation: null,
		human_step:
			'Switch the system default output device (or unplug the active one) while playback is audible.',
		fallback:
			'With no second output device, switching between built-in output and a loopback device is an acceptable substitute; record which pair was used.'
	},
	{
		id: 'WKV-08',
		title: 'Sleep and wake recovery',
		automation: 'human-action',
		failure_class: 'host-disqualifying',
		measurement:
			'Sleep the machine for at least 60 s mid-playback, wake it, and read the sampled state across the gap.',
		pass_criterion:
			'The engine resumes presentation or surfaces an explicit recoverable error; published state never claims audibility while the capture is silent; clock rejection rules hold across the gap.',
		expectation: null,
		human_step: 'Sleep the lid for at least 60 s during playback, then wake and unlock.',
		fallback: null
	},
	{
		id: 'WKV-09',
		title: 'Teardown settles to verified silence and remounts',
		automation: 'auto',
		failure_class: 'tuning',
		measurement:
			'Leave the performance route, wait for the stop transaction, then return to it in the same process.',
		pass_criterion:
			'Stop settles with revisions equal and no audible deck, the capture falls to the silence floor, and a remount plays again cleanly without a process restart.',
		expectation: null,
		human_step: null,
		fallback: null
	},
	{
		id: 'WKV-10',
		title: 'Strict BAR sync keeps real PQTZ phase 1-2-3-4',
		automation: 'auto',
		failure_class: 'tuning',
		measurement:
			'Sync two decks in default BAR mode, compare real PQTZ beat numbers and phase at the settled position, then repeat with an explicit BEAT selection.',
		pass_criterion:
			'Beat 1 aligns with 1, 2 with 2, 3 with 3, 4 with 4 in BAR; half and double normalization is rejected in BAR and permitted only after an explicit BEAT selection; fresh decks default Quantize, Beat Sync and Master Tempo on.',
		expectation: null,
		human_step: null,
		fallback: null
	},
	{
		id: 'WKV-11',
		title: 'Waveform drag seeks while paused',
		automation: 'human-action',
		failure_class: 'tuning',
		measurement:
			'Drag the scrolling waveform with a real pointer while the deck is paused; the harness records the engine-published position before, during and after the drag.',
		pass_criterion:
			'The published position follows the drag, the final pointer target is not dropped, and the settled position matches the released target.',
		expectation: null,
		human_step:
			'Drag deck 1 scrolling waveform left and right with the trackpad while paused, then release.',
		fallback:
			'A scripted pointer-event drag may be used for triage only; it is synthesized input and cannot stand as the evidence for this row.'
	},
	{
		id: 'WKV-12',
		title: 'Waveform drag seeks while playing',
		automation: 'human-action',
		failure_class: 'tuning',
		measurement:
			'Repeat the drag while the deck is audible; the harness records presented revisions and audibility across the drag.',
		pass_criterion:
			'Audio follows the drag, presentation resumes with matching revisions, and no optimistic position is published as heard.',
		expectation: null,
		human_step: 'Repeat the waveform drag while deck 1 is playing and audible.',
		fallback: null
	},
	{
		id: 'WKV-13',
		title: 'Waveform, IPC and output presentation are one read model',
		automation: 'auto',
		failure_class: 'host-disqualifying',
		measurement:
			'At settled playback, compare the rendered waveform cursor value with the dispatcher position_ms and the presentation-clock derived position.',
		pass_criterion:
			'All three agree within the recorded tolerance at settled state, and the waveform renders only engine-published position.',
		expectation: null,
		human_step: null,
		fallback: null
	}
];

function _criterionIds() {
	return SPIKE_CRITERIA.map((criterion) => criterion.id);
}

/** The criterion with `id`, or a thrown error naming the unknown id. */
export function criterionById(id) {
	const found = SPIKE_CRITERIA.find((criterion) => criterion.id === id);
	if (found === undefined) {
		throw new Error(`unknown spike criterion "${id}"; known ids: ${_criterionIds().join(', ')}`);
	}
	return found;
}

const REQUIRED_HOST_FIELDS = [
	'commit_sha',
	'macos_version',
	'webkit_version',
	'user_agent',
	'hardware',
	'audio_device',
	'activation_mode',
	'operator'
];

/**
 * A fresh evidence document. `host` must carry every field ADR-0006 expects to
 * see on a real-machine result; a missing field fails fast rather than
 * producing evidence nobody can attribute to a machine or a commit.
 */
export function newResultsDocument(host) {
	if (host === null || typeof host !== 'object') {
		throw new Error('spike results need a host metadata object');
	}
	const missing = REQUIRED_HOST_FIELDS.filter(
		(field) => typeof host[field] !== 'string' || host[field].trim() === ''
	);
	if (missing.length > 0) {
		throw new Error(`host metadata is incomplete: ${missing.join(', ')}`);
	}
	return {
		schema: 'desk-005-wkwebview-spike/1',
		host: { ...host },
		rows: []
	};
}

/**
 * Record one verdict. Recording the same criterion twice throws: a retry keeps
 * the first failure on record (ADR-0006), so a second observation belongs in a
 * second document, not on top of the first.
 */
export function recordVerdict(document_, entry) {
	const criterion = criterionById(entry.id);
	if (!VERDICTS.includes(entry.verdict)) {
		throw new Error(`verdict for ${criterion.id} must be one of ${VERDICTS.join(', ')}`);
	}
	if (typeof entry.detail !== 'string' || entry.detail.trim() === '') {
		throw new Error(`verdict for ${criterion.id} needs a detail string describing the observation`);
	}
	if (entry.verdict === 'UNAVAILABLE' && (entry.missing_capability ?? '').trim() === '') {
		throw new Error(`UNAVAILABLE on ${criterion.id} must name the missing capability`);
	}
	if (document_.rows.some((row) => row.id === criterion.id)) {
		throw new Error(`${criterion.id} already has a recorded verdict; start a new run document`);
	}
	document_.rows.push({
		id: criterion.id,
		verdict: entry.verdict,
		detail: entry.detail,
		missing_capability: entry.missing_capability ?? null,
		evidence: entry.evidence ?? null,
		observations: entry.observations ?? null,
		recorded_at: entry.recorded_at ?? null
	});
	return document_;
}

/**
 * Gate verdict for a document: FAIL if any row failed, PASS only when every
 * criterion has a PASS, otherwise INCOMPLETE with the gap named. An UNAVAILABLE
 * row is a named qualification gap, never a pass.
 */
export function rollupGate(document_) {
	const recorded = new Map(document_.rows.map((row) => [row.id, row]));
	const failed = _criterionIds().filter((id) => recorded.get(id)?.verdict === 'FAIL');
	const unavailable = _criterionIds().filter((id) => recorded.get(id)?.verdict === 'UNAVAILABLE');
	const unrecorded = _criterionIds().filter((id) => !recorded.has(id));
	const hostDisqualifying = failed.filter(
		(id) => criterionById(id).failure_class === 'host-disqualifying'
	);
	let verdict = 'PASS';
	if (failed.length > 0) verdict = 'FAIL';
	else if (unavailable.length > 0 || unrecorded.length > 0) verdict = 'INCOMPLETE';
	return {
		verdict,
		failed,
		unavailable,
		unrecorded,
		host_disqualifying: hostDisqualifying,
		phase_4_entry_gate: verdict === 'PASS' ? 'open' : 'closed'
	};
}

function _escapeCell(text) {
	return String(text).replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

/** The evidence document as markdown, in the shape ADR-0006 expects. */
export function renderEvidenceMarkdown(document_) {
	const gate = rollupGate(document_);
	const lines = [
		'# DESK-005 WKWebView spike evidence',
		'',
		`- schema: ${document_.schema}`,
		...REQUIRED_HOST_FIELDS.map((field) => `- ${field.replace(/_/g, ' ')}: ${document_.host[field]}`),
		`- gate verdict: ${gate.verdict} (Phase 4 entry gate ${gate.phase_4_entry_gate})`,
		'',
		'| ID | Criterion | Verdict | Missing capability | Detail | Evidence |',
		'| --- | --- | --- | --- | --- | --- |'
	];
	for (const criterion of SPIKE_CRITERIA) {
		const row = document_.rows.find((candidate) => candidate.id === criterion.id);
		lines.push(
			`| ${criterion.id} | ${_escapeCell(criterion.title)} | ${row?.verdict ?? 'NOT RECORDED'} | ` +
				`${_escapeCell(row?.missing_capability ?? '')} | ${_escapeCell(row?.detail ?? '')} | ` +
				`${_escapeCell(row?.evidence ?? '')} |`
		);
	}
	if (gate.host_disqualifying.length > 0) {
		lines.push(
			'',
			`Host-disqualifying failures (see the runbook decision section): ${gate.host_disqualifying.join(', ')}.`
		);
	}
	lines.push('');
	return lines.join('\n');
}
