/**
 * DESK-005 WKWebView spike harness (operator-run, manual).
 *
 * Runs INSIDE the WKWebView, on top of the already-loaded production
 * `/performance` route, and drives everything that can be automated through the
 * production typed dispatcher (`window.musicDjToolsPerformance`). It creates no
 * audio graph of its own, mocks nothing, and holds no fallback: when a
 * capability is absent it records UNAVAILABLE naming the capability.
 *
 * Injection and full procedure: README.md in this directory, and
 * docs/quality/desk-005-wkwebview-spike-runbook.md.
 *
 * Scope limits, stated so no reader over-reads a green run:
 * - `ipc.capture(deck)` reads the real analyser inside the real graph. That is
 *   evidence of signal in the graph, NOT of sound at the physical output. Output
 *   truth still comes from the loopback capture the runbook requires.
 * - Device changes, sleep and wake, the activation click and the waveform drags
 *   are physical acts; the harness only samples around them.
 */

import {
	newResultsDocument,
	recordVerdict,
	renderEvidenceMarkdown,
	rollupGate,
	SPIKE_CRITERIA
} from './spike-criteria.mjs';

const DECKS = [1, 2, 3, 4];
const SETTLE_TIMEOUT_MS = 20_000;
const SAMPLE_INTERVAL_MS = 100;
/** Analyser peak below this counts as no signal in the graph. */
const SIGNAL_FLOOR = 0.0005;
/** Tolerance for the one-read-model comparison, in ms. */
const READ_MODEL_TOLERANCE_MS = 60;

function _requireIpc(host) {
	const ipc = host.musicDjToolsPerformance;
	if (ipc === undefined) {
		throw new Error(
			'UNAVAILABLE: window.musicDjToolsPerformance is absent. Load /performance in this window first.'
		);
	}
	if (ipc.version !== 1) {
		throw new Error(`UNAVAILABLE: performance IPC version ${String(ipc.version)} is not 1`);
	}
	return ipc;
}

function _requireDom(host) {
	if (host.document === undefined || host.document === null) {
		throw new Error('UNAVAILABLE: no document; the harness runs inside the app window only');
	}
	return host.document;
}

function _sleep(host, ms) {
	return new Promise((resolve) => host.setTimeout(resolve, ms));
}

async function _waitFor(host, describe, predicate, timeoutMs = SETTLE_TIMEOUT_MS) {
	const deadline = host.performance.now() + timeoutMs;
	let last = null;
	while (host.performance.now() < deadline) {
		last = predicate();
		if (last !== false && last !== null && last !== undefined) return last;
		await _sleep(host, 25);
	}
	throw new Error(`timed out after ${timeoutMs} ms waiting for ${describe}`);
}

function _deckPresented(state, deck, audible) {
	const snapshot = state.decks[deck];
	return (
		snapshot.audible === audible &&
		!snapshot.transport_pending &&
		snapshot.transport_clock.presented_revision === snapshot.transport_clock.desired_revision
	);
}

function _peakAmplitude(snapshot) {
	let peak = 0;
	for (const sample of snapshot.time_domain) {
		const magnitude = Math.abs(sample);
		if (magnitude > peak) peak = magnitude;
	}
	return peak;
}

function _waveformCursorMs(document_, deck) {
	const canvas = document_.querySelector(`canvas[aria-label="deck ${deck} waveform seek"]`);
	if (canvas === null) return null;
	const rendered = Number(canvas.getAttribute('aria-valuenow'));
	return Number.isFinite(rendered) ? rendered : null;
}

/** Beat number and phase within the bar at `positionMs`, from real PQTZ. */
function _beatAt(snapshot, positionMs) {
	const grid = snapshot.beatgrid;
	if (!Array.isArray(grid) || grid.length === 0) return null;
	let current = null;
	for (const beat of grid) {
		if (beat.time_ms <= positionMs) current = beat;
		else break;
	}
	return current === null ? null : { n: current.n, time_ms: current.time_ms };
}

/**
 * Install the harness on `host` (normally `window`). Throws a named
 * UNAVAILABLE error when the host cannot support the spike, so a broken
 * injection can never look like a clean run.
 */
export function installSpikeHarness(host, options = {}) {
	const ipc = _requireIpc(host);
	const document_ = _requireDom(host);
	const apiBase = options.apiBase ?? '';
	let results = null;
	let samplerId = null;
	const samples = [];
	const anomalies = [];

	function _capabilities() {
		return {
			user_agent: host.navigator?.userAgent ?? 'unknown',
			platform: host.navigator?.platform ?? 'unknown',
			hardware_concurrency: host.navigator?.hardwareConcurrency ?? null,
			has_audio_worklet: typeof host.AudioWorklet !== 'undefined',
			has_audio_context: typeof host.AudioContext !== 'undefined',
			has_get_output_timestamp:
				typeof host.AudioContext !== 'undefined' &&
				typeof host.AudioContext.prototype?.getOutputTimestamp === 'function',
			has_enumerate_devices: typeof host.navigator?.mediaDevices?.enumerateDevices === 'function',
			has_set_sink_id: typeof host.HTMLMediaElement?.prototype?.setSinkId === 'function',
			has_audio_context_sink_id:
				typeof host.AudioContext !== 'undefined' &&
				'setSinkId' in (host.AudioContext.prototype ?? {})
		};
	}

	function _sample() {
		const state = ipc.query();
		const at = host.performance.now();
		for (const deck of DECKS) {
			const snapshot = state.decks[deck];
			const clock = snapshot.transport_clock;
			const contextTime = clock.presentation_context_time_s;
			const previous = samples.length > 0 ? samples[samples.length - 1].decks[deck] : null;
			if (contextTime !== null && (!Number.isFinite(contextTime) || contextTime < 0)) {
				anomalies.push({ at, deck, kind: 'published_invalid_context_time', value: contextTime });
			}
			if (
				previous !== null &&
				previous.context_time_s !== null &&
				contextTime !== null &&
				contextTime < previous.context_time_s
			) {
				anomalies.push({
					at,
					deck,
					kind: 'published_regressing_context_time',
					value: contextTime,
					previous: previous.context_time_s
				});
			}
			if (snapshot.audible && (contextTime === null || contextTime === 0)) {
				anomalies.push({ at, deck, kind: 'audible_without_presented_output', value: contextTime });
			}
			if (snapshot.processor_error !== null) {
				anomalies.push({ at, deck, kind: 'processor_error', value: snapshot.processor_error });
			}
		}
		samples.push({
			at,
			decks: Object.fromEntries(
				DECKS.map((deck) => {
					const snapshot = state.decks[deck];
					return [
						deck,
						{
							position_ms: snapshot.position_ms,
							audible: snapshot.audible,
							playing: snapshot.playing,
							transport_pending: snapshot.transport_pending,
							context_time_s: snapshot.transport_clock.presentation_context_time_s,
							source: snapshot.transport_clock.source,
							desired_revision: snapshot.transport_clock.desired_revision,
							presented_revision: snapshot.transport_clock.presented_revision,
							waveform_cursor_ms: _waveformCursorMs(document_, deck)
						}
					];
				})
			)
		});
	}

	async function _dispatch(command) {
		return ipc.dispatch(command);
	}

	async function _settled(deck, audible) {
		return _waitFor(host, `deck ${deck} presented audible=${String(audible)}`, () => {
			const state = ipc.query();
			return _deckPresented(state, deck, audible) ? state : false;
		});
	}

	/** Real playable analyzed tracks from the real backend. No fixtures. */
	async function discoverTracks(count) {
		const listing = await host.fetch(`${apiBase}/api/v1/tracks?limit=1000&available=true`);
		if (!listing.ok) throw new Error(`real track listing failed: HTTP ${listing.status}`);
		const payload = await listing.json();
		const found = [];
		for (const track of payload.items) {
			if (found.length >= count) break;
			if (!track.file_exists || typeof track.bpm !== 'number' || track.bpm <= 0) continue;
			const anlz = await host.fetch(
				`${apiBase}/api/v1/tracks/${encodeURIComponent(track.stable_id)}/anlz?points=100`
			);
			if (!anlz.ok) continue;
			const parsed = await anlz.json();
			if ((parsed.beatgrid?.beats?.length ?? 0) < 32) continue;
			found.push({ stable_id: track.stable_id, bpm: track.bpm });
		}
		return found;
	}

	async function _runWorkletCriterion(track) {
		await _dispatch({ type: 'load', deck: 1, stable_id: track.stable_id });
		const state = await _waitFor(host, 'deck 1 load to settle', () => {
			const candidate = ipc.query();
			return candidate.decks[1].command_pending === false ? candidate : false;
		});
		const deck = state.decks[1];
		if (deck.processor_error !== null) {
			return {
				verdict: 'FAIL',
				detail: `deck 1 reported processor_error: ${deck.processor_error}`,
				observations: { stable_id: track.stable_id }
			};
		}
		if (deck.duration_ms === null || deck.beatgrid.length === 0) {
			return {
				verdict: 'FAIL',
				detail: 'deck 1 loaded without a decoded duration or a real beatgrid',
				observations: { duration_ms: deck.duration_ms, beats: deck.beatgrid.length }
			};
		}
		return {
			verdict: 'PASS',
			detail:
				'stretch processor constructed and the real track decoded with a real PQTZ beatgrid; ' +
				'confirm the executed worklet module URL in Web Inspector Sources before accepting this row',
			observations: {
				stable_id: track.stable_id,
				duration_ms: deck.duration_ms,
				beats: deck.beatgrid.length,
				last_load_stages: deck.last_load_stages
			}
		};
	}

	async function _runClockCriterion() {
		await _dispatch({ type: 'play', deck: 1, playing: true });
		await _settled(1, true);
		await _sleep(host, 3_000);
		await _dispatch({ type: 'seek', deck: 1, position_ms: 30_000 });
		await _settled(1, true);
		await _sleep(host, 2_000);
		const published = anomalies.filter((entry) => entry.kind.startsWith('published_'));
		const claimed = anomalies.filter((entry) => entry.kind === 'audible_without_presented_output');
		if (published.length > 0 || claimed.length > 0) {
			return {
				verdict: 'FAIL',
				detail: `presentation-clock rule violations sampled: ${published.length} invalid or regressing, ${claimed.length} audible without presented output`,
				observations: { published, claimed }
			};
		}
		const audibleSamples = samples.filter((sample) => sample.decks[1].audible);
		if (audibleSamples.length === 0) {
			return {
				verdict: 'FAIL',
				detail: 'deck 1 never published audible, so no presentation-clock behavior was sampled',
				observations: { samples: samples.length }
			};
		}
		return {
			verdict: 'PASS',
			detail: `sampled ${samples.length} states with no invalid, regressing or unpresented published clock value`,
			observations: { samples: samples.length, audible_samples: audibleSamples.length }
		};
	}

	async function _runFourDeckCriterion(tracks) {
		if (tracks.length < 4) {
			return {
				verdict: 'UNAVAILABLE',
				missing_capability: `four playable analyzed library tracks (found ${tracks.length})`,
				detail: 'four-deck load not attempted; one track repeated across decks is not this test',
				observations: { found: tracks.length }
			};
		}
		for (const deck of DECKS) {
			await _dispatch({ type: 'load', deck, stable_id: tracks[deck - 1].stable_id });
		}
		for (const deck of DECKS) {
			await _dispatch({ type: 'play', deck, playing: true });
		}
		const failures = [];
		for (const deck of DECKS) {
			try {
				await _settled(deck, true);
			} catch (error) {
				failures.push(`deck ${deck}: ${String(error)}`);
			}
		}
		const soakStart = samples.length;
		await _sleep(host, options.soakMs ?? 60_000);
		const peaks = Object.fromEntries(
			DECKS.map((deck) => [deck, _peakAmplitude(ipc.capture(deck))])
		);
		const silentDecks = DECKS.filter((deck) => peaks[deck] < SIGNAL_FLOOR);
		const soakAnomalies = anomalies.filter((entry) => entry.at >= (samples[soakStart]?.at ?? 0));
		if (failures.length > 0 || silentDecks.length > 0 || soakAnomalies.length > 0) {
			return {
				verdict: 'FAIL',
				detail: `four-deck load failed: ${failures.length} decks never presented, decks with no graph signal ${JSON.stringify(silentDecks)}, ${soakAnomalies.length} sampled anomalies`,
				observations: { failures, peaks, soakAnomalies }
			};
		}
		return {
			verdict: 'PASS',
			detail: `four real decks presented audible with graph signal on all four and no anomalies over ${(options.soakMs ?? 60_000) / 1000} s; the runbook soak length and the loopback capture remain the output-truth evidence`,
			observations: { peaks, tracks: tracks.map((track) => track.stable_id) }
		};
	}

	async function _runDeviceCriterion() {
		const capabilities = _capabilities();
		await _dispatch({ type: 'headphone_outputs_refresh' });
		const state = ipc.query();
		const outputs = state.mixer.headphones.outputs ?? [];
		if (outputs.length < 2) {
			return {
				verdict: 'UNAVAILABLE',
				missing_capability: `a second selectable output device (engine enumerated ${outputs.length}); host APIs: enumerateDevices=${String(capabilities.has_enumerate_devices)}, setSinkId=${String(capabilities.has_set_sink_id)}`,
				detail: 'device selection not exercised; see the runbook fallback for the built-in plus loopback pair',
				observations: { outputs, capabilities }
			};
		}
		const target = outputs[1];
		await _dispatch({ type: 'headphone_output_acquire' });
		await _dispatch({ type: 'headphone_output_select', device_id: target.id });
		const after = ipc.query().mixer.headphones;
		return {
			verdict: 'PASS',
			detail: `engine selected a non-default output; confirm by ear which physical output carries audio. Selected state: ${JSON.stringify(after)}`,
			observations: { outputs, capabilities }
		};
	}

	async function _runReadModelCriterion() {
		const state = await _settled(1, true);
		const deck = state.decks[1];
		const cursor = _waveformCursorMs(document_, 1);
		if (cursor === null) {
			return {
				verdict: 'UNAVAILABLE',
				missing_capability: 'the scrolling waveform canvas for deck 1 is not mounted in this view',
				detail: 'waveform cursor could not be read, so the three-way comparison was not made',
				observations: null
			};
		}
		const delta = Math.abs(cursor - deck.position_ms);
		const presented = deck.transport_clock.source === 'audio_output';
		if (!presented || delta > READ_MODEL_TOLERANCE_MS) {
			return {
				verdict: 'FAIL',
				detail: `waveform cursor and dispatcher position differ by ${delta.toFixed(1)} ms with clock source ${deck.transport_clock.source}`,
				observations: { cursor, position_ms: deck.position_ms, clock: deck.transport_clock }
			};
		}
		return {
			verdict: 'PASS',
			detail: `waveform cursor, dispatcher position and audio-output clock agree within ${delta.toFixed(1)} ms`,
			observations: { cursor, position_ms: deck.position_ms, clock: deck.transport_clock }
		};
	}

	async function _runBarSyncCriterion(tracks) {
		if (tracks.length < 2) {
			return {
				verdict: 'UNAVAILABLE',
				missing_capability: `two playable analyzed tracks with real beatgrids (found ${tracks.length})`,
				detail: 'BAR sync alignment not exercised',
				observations: null
			};
		}
		await _dispatch({ type: 'load', deck: 2, stable_id: tracks[1].stable_id });
		await _dispatch({ type: 'play', deck: 2, playing: true });
		await _settled(2, true);
		await _dispatch({ type: 'master', deck: 1 });
		await _dispatch({ type: 'sync_mode', deck: 2, mode: 'BAR' });
		await _dispatch({ type: 'beat_sync', deck: 2, enabled: true });
		await _settled(2, true);
		const state = ipc.query();
		const master = state.decks[1];
		const follower = state.decks[2];
		const masterBeat = _beatAt(master, master.position_ms);
		const followerBeat = _beatAt(follower, follower.position_ms);
		if (masterBeat === null || followerBeat === null) {
			return {
				verdict: 'UNAVAILABLE',
				missing_capability: 'real PQTZ beat numbers on both decks at the settled position',
				detail: 'phase alignment could not be read from the published read model',
				observations: { masterBeat, followerBeat }
			};
		}
		const masterPhase = ((masterBeat.n - 1) % 4) + 1;
		const followerPhase = ((followerBeat.n - 1) % 4) + 1;
		const ratio =
			follower.effective_bpm !== null && master.effective_bpm !== null
				? follower.effective_bpm / master.effective_bpm
				: null;
		const normalized = ratio !== null && (Math.abs(ratio - 0.5) < 0.02 || Math.abs(ratio - 2) < 0.02);
		if (masterPhase !== followerPhase || normalized) {
			return {
				verdict: 'FAIL',
				detail: `BAR sync misaligned: master phase ${masterPhase}, follower phase ${followerPhase}, effective bpm ratio ${String(ratio)}`,
				observations: { masterBeat, followerBeat, ratio, sync_error: follower.sync_error }
			};
		}
		return {
			verdict: 'PASS',
			detail: `BAR sync aligned phase ${masterPhase} to ${followerPhase} with no half or double normalization (ratio ${String(ratio)}); exercise the explicit BEAT opt-out by hand before accepting this row`,
			observations: { masterBeat, followerBeat, ratio }
		};
	}

	async function _runTeardownCriterion() {
		for (const deck of DECKS) {
			await _dispatch({ type: 'play', deck, playing: false });
		}
		const state = await _waitFor(host, 'every deck to settle silent', () => {
			const candidate = ipc.query();
			return DECKS.every((deck) => _deckPresented(candidate, deck, false)) ? candidate : false;
		});
		const peaks = Object.fromEntries(
			DECKS.map((deck) => [deck, _peakAmplitude(ipc.capture(deck))])
		);
		const loud = DECKS.filter((deck) => peaks[deck] >= SIGNAL_FLOOR);
		if (loud.length > 0) {
			return {
				verdict: 'FAIL',
				detail: `decks ${JSON.stringify(loud)} still carry graph signal after the stop transaction settled`,
				observations: { peaks, command_pending: state.command_pending }
			};
		}
		return {
			verdict: 'PASS',
			detail:
				'stop settled with matching revisions and no graph signal; the route-unmount and remount half of this row is a runbook step, not automated here',
			observations: { peaks }
		};
	}

	async function _runActivationAutoCriterion() {
		if ((options.activationMode ?? 'auto') !== 'auto') {
			return {
				verdict: 'UNAVAILABLE',
				missing_capability: `a cold start under VITE_PERFORMANCE_AUDIO_ACTIVATION=auto (this window ran ${String(options.activationMode)})`,
				detail: 'activation mode auto must be measured in its own cold start',
				observations: null
			};
		}
		const state = ipc.query();
		const audible = DECKS.some((deck) => state.decks[deck].audible);
		if (!audible) {
			return {
				verdict: 'FAIL',
				detail: 'no deck reached audible without a gesture under activation mode auto',
				observations: { last_error: state.last_error }
			};
		}
		return {
			verdict: 'PASS',
			detail: 'a deck presented audible with no user gesture in this window under activation mode auto',
			observations: null
		};
	}

	/**
	 * Start a run document. Operator-supplied metadata (commit SHA, macOS and
	 * WebKit versions, hardware, audio device, activation mode, operator) is
	 * mandatory; `user_agent` and the capability probe are read from the host.
	 */
	async function begin(hostMetadata) {
		const capabilities = _capabilities();
		results = newResultsDocument({ user_agent: capabilities.user_agent, ...hostMetadata });
		results.capabilities = capabilities;
		return results;
	}

	function startSampling(intervalMs = SAMPLE_INTERVAL_MS) {
		if (samplerId !== null) throw new Error('sampler is already running');
		samplerId = host.setInterval(_sample, intervalMs);
		return samplerId;
	}

	function stopSampling() {
		if (samplerId === null) return;
		host.clearInterval(samplerId);
		samplerId = null;
	}

	function mark(id, verdict, detail, extra = {}) {
		if (results === null) throw new Error('call begin(hostMetadata) before recording verdicts');
		return recordVerdict(results, { id, verdict, detail, ...extra });
	}

	/** Every criterion the harness can drive itself, in run order. */
	async function runAutomatable() {
		if (results === null) throw new Error('call begin(hostMetadata) before running criteria');
		const tracks = await discoverTracks(4);
		if (tracks.length === 0) {
			mark('WKV-01', 'UNAVAILABLE', 'no playable analyzed track in the real library', {
				missing_capability: 'a real analyzed track with audio on disk'
			});
			return rollupGate(results);
		}
		const steps = [
			['WKV-01', () => _runWorkletCriterion(tracks[0])],
			['WKV-02', () => _runClockCriterion()],
			['WKV-03', () => _runActivationAutoCriterion()],
			['WKV-13', () => _runReadModelCriterion()],
			['WKV-10', () => _runBarSyncCriterion(tracks)],
			['WKV-05', () => _runFourDeckCriterion(tracks)],
			['WKV-06', () => _runDeviceCriterion()],
			['WKV-09', () => _runTeardownCriterion()]
		];
		for (const [id, run] of steps) {
			let outcome;
			try {
				outcome = await run();
			} catch (error) {
				outcome = { verdict: 'FAIL', detail: `harness step threw: ${String(error)}` };
			}
			mark(id, outcome.verdict, outcome.detail, {
				missing_capability: outcome.missing_capability,
				observations: outcome.observations ?? null,
				recorded_at: new Date().toISOString()
			});
		}
		return rollupGate(results);
	}

	/** Criteria that still need the human, with the exact act required. */
	function humanSteps() {
		return SPIKE_CRITERIA.filter((criterion) => criterion.human_step !== null).map((criterion) => ({
			id: criterion.id,
			automation: criterion.automation,
			step: criterion.human_step,
			fallback: criterion.fallback
		}));
	}

	/**
	 * The run as machine-readable evidence. Returns the JSON text and the
	 * markdown table and prints both; saving is the operator step in the runbook
	 * because a WKWebView download path is an expectation, not a known.
	 */
	function dump() {
		if (results === null) throw new Error('call begin(hostMetadata) before dumping evidence');
		const document__ = { ...results, samples, anomalies, gate: rollupGate(results) };
		const json = JSON.stringify(document__, null, 2);
		const markdown = renderEvidenceMarkdown(results);
		host.console.log(json);
		host.console.log(markdown);
		return { json, markdown, gate: document__.gate };
	}

	const harness = {
		version: 1,
		capabilities: _capabilities,
		begin,
		startSampling,
		stopSampling,
		discoverTracks,
		runAutomatable,
		humanSteps,
		mark,
		anomalies: () => [...anomalies],
		samples: () => [...samples],
		dump
	};
	host.wkwebviewSpike = harness;
	return harness;
}
