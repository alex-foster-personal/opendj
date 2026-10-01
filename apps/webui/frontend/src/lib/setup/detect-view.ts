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
 *   ✔︎ ✅ 🎯 detectPhase() never answers 'answered' for a null detection, so
 *     the failure visual cannot be painted before an answer exists.
 *     [if] a null detection renders the not-found state [then ⛔️] broken
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

import {
	agentEscapeEndpoint,
	humanEscapeTitle,
	humanKeyLine,
	humanProbeLabel,
	humanScanningSentence,
	shortenPath
} from './present';
import { isFatalBlocker, type RekordboxDetection } from './setup-api';

/**
 * What the detect step is doing right now.
 *
 * 'scanning' covers BOTH "the first answer has not arrived" and "we are
 * re-asking", because to an operator they are the same fact: the machine is
 * looking. 'answered' is the only phase allowed to draw a verdict.
 */
export type DetectPhase = 'scanning' | 'answered';

export function detectPhase(
	detection: RekordboxDetection | null,
	busy: boolean
): DetectPhase {
	if (detection === null) return 'scanning';
	return busy ? 'scanning' : 'answered';
}

/** The sentence the scanning phase shows. Spelled once so the e2e can pin it
 * and so it can never be confused with a verdict. */
export const SCANNING_SENTENCE = humanScanningSentence();

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
	/** Raw path or detail for agent diagnostics. */
	agentDetail: string;
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
			text: humanProbeLabel('Your DJ collection', detection.live_db.exists),
			danger: !detection.live_db.exists && nothingToRead,
			title: 'Whether a DJ collection database was found on this machine.',
			agentDetail: detection.live_db.path
		},
		{
			key: 'share_dir',
			text: humanProbeLabel('Waveform data folder', detection.share_dir.exists),
			// Absent is a real problem, but not THIS problem: without it the
			// tracks still land and only waveforms and beatgrids are missing.
			danger: false,
			title: 'Whether waveform and beatgrid data was found alongside the collection.',
			agentDetail: detection.share_dir.path
		},
		{
			key: 'working_copy',
			text: humanProbeLabel('Saved collection copy', detection.working_copy.exists),
			danger: !detection.working_copy.exists && nothingToRead,
			title: 'Whether a working copy of the collection is already saved locally.',
			agentDetail: detection.working_copy.path
		},
		{
			key: 'plain_copy',
			text: humanProbeLabel('Unlocked collection copy', detection.plain_copy.exists),
			danger: !detection.plain_copy.exists && nothingToRead,
			title: 'Whether an unlocked copy is ready to read from.',
			agentDetail: detection.plain_copy.path
		},
		{
			key: 'key',
			text: humanKeyLine(!keyMissing),
			danger: keyMissing,
			title: 'Whether the collection can be unlocked for import right now.',
			agentDetail: detection.key_detail
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
		title: humanEscapeTitle('redetect')
	},
	{
		id: 'folder',
		label: 'Choose a folder instead',
		title: humanEscapeTitle('folder')
	},
	{
		id: 'dismiss',
		label: 'Continue without importing',
		title: humanEscapeTitle('dismiss')
	}
] as const;

/** Agent endpoint for an escape action. Not shown in default copy. */
export function escapeAgentEndpoint(id: EscapeAction['id']): string {
	return agentEscapeEndpoint(id);
}

/** Shorten a path for any human-visible setup copy. */
export { shortenPath };
