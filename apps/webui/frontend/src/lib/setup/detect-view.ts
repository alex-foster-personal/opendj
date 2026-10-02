/**
 * How the detect step RENDERS what detection found. Pure, so it is testable
 * without mounting a component.
 *
 * ORIGIN (the maintainer, MacBook Air, Wed 19 Aug 2026). The detect step read as
 * "failed to find rekordbox library", in grey, with nothing enabled to press
 * next -- on a machine where the engine log showed detection answering 200
 * three times and the import afterwards succeeding with 8558 tracks. Three
 * separate things conspired:
 *
 *   1. A NULL detection rendered as `<p class="muted">Looking...</p>` forever
 *      when nothing was going to re-run it, so an in-flight visual described a
 *      state that was not in flight. Grey, and indistinguishable from "we
 *      looked and found nothing".
 *   2. The probe list rendered "rekordbox database: not present at ..." in
 *      plain body text. That IS the failure, and it did not look like one.
 *   3. The reason Continue was disabled lived in a hover `title`, which on a
 *      trackpad nobody ever sees.
 *
 * So: the phase is explicit (scanning is a state, not the absence of one),
 * every probe line carries its own severity, and the refusal is a value the
 * caller renders INLINE rather than a tooltip.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 detectPhase() answers 'scanning' for a null detection that is
 *     still idle or in flight, so a not-found visual cannot be painted before
 *     an answer exists. A `failed` ask is the exception: that is a verdict,
 *     and painting the scanning sentence beside the red error is issue #3422.
 *     [if] a null detection that is not `failed` renders the not-found state [then ⛔️] broken
 *     [if] detectState `failed` returns `scanning` [then ⛔️] broken
 *   ✔︎ ✅ 🎯 probeRows() marks a MISSING file as danger exactly when it is the
 *     reason nothing is importable, never merely because it is absent -- a
 *     working copy that is absent because the plain copy is being used is not
 *     a problem and must not be painted as one.
 *     [if] every absent probe reads red [then ⛔️] broken
 *   ✔︎ ✅ 🎯 blockerTone() agrees with isFatalBlocker(), so a sentence that
 *     stops the import and a sentence that does not cannot look alike.
 *     [if] a fatal blocker renders in the muted tone [then ⛔️] broken
 *   ✔︎ ✅ 🎯 ESCAPE_ACTIONS is a constant, never derived from detection, so
 *     there is no state in which the step offers nothing to press.
 *     [if] an escape action is gated on a blocker [then ⛔️] broken
 */

import { formatBytes, isFatalBlocker, type RekordboxDetection } from './setup-api';

/**
 * What the detect step is doing right now.
 *
 * 'scanning' covers BOTH "the first answer has not arrived" and "we are
 * re-asking", because to an operator they are the same fact: the machine is
 * looking. 'answered' is the only phase allowed to draw a verdict.
 */
export type DetectPhase = 'scanning' | 'answered';

/** Mirrors wizard.svelte.ts detectState without importing the store. */
export type DetectState = 'idle' | 'scanning' | 'answered' | 'failed';

export function detectPhase(
	detection: RekordboxDetection | null,
	detectState: DetectState
): DetectPhase {
	// Failed is a verdict. The scanning sentence beside the red error is the
	// #3422 detect bug; idle/scanning with no answer stays in flight.
	if (detectState === 'failed') return 'answered';
	if (detectState === 'scanning') return 'scanning';
	if (detection === null) return 'scanning';
	return 'answered';
}

/** The sentence the scanning phase shows. Spelled once so the e2e can pin it
 * and so it can never be confused with a verdict. */
export const SCANNING_SENTENCE = 'Looking for a rekordbox library on this machine...';

/** How loud a blocker sentence is. 'danger' stops the import outright. */
export type BlockerTone = 'danger' | 'warning';

export function blockerTone(code: string): BlockerTone {
	return isFatalBlocker(code) ? 'danger' : 'warning';
}

/** One line of the "what is on this machine" list. */
export interface ProbeRow {
	/** Stable key for the {#each}. */
	key: string;
	/** What the line renders. */
	text: string;
	/** True when this missing thing is WHY nothing can be imported. */
	danger: boolean;
	/** The hover explanation every readout carries (house rule). */
	title: string;
}

function _line(label: string, path: string, exists: boolean, size: number | null): string {
	const bytes = formatBytes(size);
	return exists
		? `${label}: ${path}${bytes === null ? '' : ` (${bytes})`}`
		: `${label}: not present at ${path}`;
}

/**
 * The probe list, with severity resolved.
 *
 * THE DANGER RULE, stated once: a missing database file is red only when
 * `import_source` is null, i.e. when its absence is the reason there is
 * nothing to read. A missing `master.db.copy` on a machine that already has a
 * decrypted `master.plain.db` is not a problem and painting it red would
 * teach the operator to distrust the colour.
 *
 * The analysis folder is never red here: tracks import without it and only
 * the waveforms are missing. It gets its own warning sentence from
 * blockerTone('rekordbox_share_missing') instead.
 */
export function probeRows(detection: RekordboxDetection): ProbeRow[] {
	const nothingToRead = detection.import_source === null;
	const keyMissing = (detection.blockers ?? []).includes('rekordbox_key_unavailable');
	return [
		{
			key: 'live_db',
			text: _line(
				'rekordbox database',
				detection.live_db.path,
				detection.live_db.exists,
				detection.live_db.size_bytes ?? null
			),
			danger: !detection.live_db.exists && nothingToRead,
			title:
				'The rekordbox install on this machine. Never opened or copied by ' +
				'this step -- only stat()ed.'
		},
		{
			key: 'share_dir',
			text: _line('Analysis folder', detection.share_dir.path, detection.share_dir.exists, null),
			// Absent is a real problem, but not THIS problem: without it the
			// tracks still land and only waveforms and beatgrids are missing.
			danger: false,
			title:
				'Where rekordbox keeps its ANLZ analyses (waveforms, beatgrids). ' +
				'Tracks import without it; waveforms do not draw.'
		},
		{
			key: 'working_copy',
			text: _line(
				'Encrypted working copy',
				detection.working_copy.path,
				detection.working_copy.exists,
				detection.working_copy.size_bytes ?? null
			),
			danger: !detection.working_copy.exists && nothingToRead,
			title:
				'A snapshot of the rekordbox database inside this engine data ' +
				'dir. Absent is normal until the first import takes one.'
		},
		{
			key: 'plain_copy',
			text: _line(
				'Decrypted working copy',
				detection.plain_copy.path,
				detection.plain_copy.exists,
				detection.plain_copy.size_bytes ?? null
			),
			danger: !detection.plain_copy.exists && nothingToRead,
			title:
				'The decrypted snapshot the import reads. Absent is normal until ' +
				'the first import decrypts one.'
		},
		{
			key: 'key',
			text: `Database key: ${detection.key_detail}`,
			danger: keyMissing,
			title:
				'Whether this process can unlock an encrypted rekordbox database ' +
				'right now, and why not when it cannot.'
		}
	];
}

/** One escape hatch out of the detect step. */
export interface EscapeAction {
	id: 'redetect' | 'folder' | 'dismiss';
	label: string;
	title: string;
}

/**
 * The three ways forward that are ALWAYS available on the detect step.
 *
 * A constant, deliberately: the bug this whole module exists for was a screen
 * where the only enabled control was one the operator did not recognise as an
 * escape. There is no detection result that removes any of these, so there is
 * no state in which the step dead-ends.
 */
export const ESCAPE_ACTIONS: readonly EscapeAction[] = [
	{
		id: 'redetect',
		label: 'Look again',
		title: 'Re-run detection now (GET /api/v1/setup/detect/rekordbox). Reads nothing else.'
	},
	{
		id: 'folder',
		label: 'Choose a folder instead',
		title:
			'Import a folder of audio files instead of a rekordbox collection ' +
			'(GET /api/v1/setup/detect/folder). Tags only: no BPM, key or beatgrid.'
	},
	{
		id: 'dismiss',
		label: 'Continue without importing',
		title:
			'Close setup and use the app with an empty library ' +
			'(POST /api/v1/setup/dismiss). Re-openable from Settings > Run setup.'
	}
] as const;
