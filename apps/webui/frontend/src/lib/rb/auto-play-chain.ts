/**
 * AutoPlay's pure chain and pick algebra: which track follows which, and in
 * what order the whole playlist can be walked.
 *
 * Extracted from auto-play.ts, which reached 650 lines through a MERGE-UNION
 * (PR #588's branch took it to 572, PR #592 to 472, each honestly green on its
 * own base; their merge produced 650) and pushed the count of frontend files
 * over 600 lines from 10 to 11. That counter is cumulative over the tree, so
 * it went red on trunk while both contributing PRs were green - it cannot be
 * evaluated on a branch in isolation, only against the thing it accumulates
 * over.
 *
 * WHY THIS SEAM AND NOT THE OBVIOUS ONE. The feed snapshot is the newer and
 * larger block, so it looks like the natural thing to cut. It is not: its
 * consumer is BrowserPanel.svelte, which sits exactly on the
 * frontend.max_fan_out ceiling of 35 imports. Moving it would hand BrowserPanel
 * a 36th import and trade a file-size red for a coupling red. Nothing here has
 * a BrowserPanel consumer, so this cut moves no ceiling. auto-play.ts re-exports
 * everything below, so no consumer of either module changes.
 *
 * Pure: no Svelte, no timers, no engine mutation. Transport goes through
 * auto-play.svelte.ts.
 */
import { camelotKeysAreCompatible } from '$lib/player/key/camelot';

/** Compatibility checks allowed per maximize-reach pick; over budget -> greedy. */
export const AUTO_PLAY_REACH_MAX_STEPS = 50_000;

/** One playlist membership row for AutoPlay pick (key/BPM for smart mode). */
export type AutoPlayTrackRow = {
	stable_id: string;
	key: string | null;
	bpm: number | null;
	/** Disk truth from listing hydrate (dataless iCloud stubs are false). */
	file_exists: boolean;
	/** Frozen display metadata for PLAY-05's user-viewable queue. */
	title?: string | null;
	artist?: string | null;
};

export type ReachPick = {
	next: string | null;
	/** Compatibility checks actually performed. */
	steps: number;
	/** True when the budget was exhausted and greedy order was used instead. */
	fell_back: boolean;
};

// ----- track pick ---------------------------------------------------------

export type TempoLockClass = 'exact' | 'fold';

/**
 * Exact (`candidateBpm / refBpm`) then Beat Sync's remaining fold scan
 * (`refBpm / candidateBpm * {0.5, 2}`, matching `_tempoRatioWithinRangeOrNull`).
 * `n = 1` is owned by exact. Pickers rank exact above fold (#1743 option 2).
 */
export function tempoLockClass(
	candidateBpm: number | null,
	refBpm: number | null,
	minRatio: number,
	maxRatio: number
): TempoLockClass | null {
	if (
		candidateBpm === null ||
		refBpm === null ||
		!(candidateBpm > 0) ||
		!(refBpm > 0) ||
		!(minRatio > 0) ||
		!(maxRatio >= minRatio)
	) {
		return null;
	}
	const exact = candidateBpm / refBpm;
	if (exact >= minRatio && exact <= maxRatio) return 'exact';
	const raw = refBpm / candidateBpm;
	for (const n of [0.5, 2] as const) {
		const folded = raw * n;
		if (folded >= minRatio && folded <= maxRatio) return 'fold';
	}
	return null;
}

/**
 * RAW/exact BPM ratio only (`candidateBpm / refBpm` inside `[min, max]`).
 *
 * Fold lives in `tempoLockClass` and is applied only as the ranked fallback
 * in the pickers: exact always wins; a half/double pair is chosen only when
 * nothing exact is reachable. That is issue #1743 option 2, mirroring
 * `_bestFollowerAnchor`. This predicate stays RAW on purpose so walkthrough
 * demo-4 (220 vs 128) stays a reject, matching BAR.
 */
export function bpmWithinPhaseLockRange(
	candidateBpm: number | null,
	refBpm: number | null,
	minRatio: number,
	maxRatio: number
): boolean {
	return tempoLockClass(candidateBpm, refBpm, minRatio, maxRatio) === 'exact';
}

function _lockClass(
	fromKey: string | null,
	fromBpm: number | null,
	to: AutoPlayTrackRow,
	minRatio: number,
	maxRatio: number
): TempoLockClass | null {
	if (!to.file_exists) return null;
	if (!camelotKeysAreCompatible(to.key, fromKey)) return null;
	return tempoLockClass(to.bpm, fromBpm, minRatio, maxRatio);
}

function _matchesLock(
	fromKey: string | null,
	fromBpm: number | null,
	to: AutoPlayTrackRow,
	minRatio: number,
	maxRatio: number,
	allowed: TempoLockClass | 'any'
): boolean {
	const cls = _lockClass(fromKey, fromBpm, to, minRatio, maxRatio);
	if (cls === null) return false;
	return allowed === 'any' || cls === allowed;
}

/**
 * Slack-aware next pick: among rows compatible with the current track, prefer
 * the one with the fewest onward options, never a dead end while another
 * candidate still has one. Ties keep membership order.
 */
export function pickNextMaximizingReach(input: {
	candidates: readonly AutoPlayTrackRow[];
	current_key: string | null;
	current_bpm: number | null;
	min_tempo_ratio: number;
	max_tempo_ratio: number;
	max_steps: number;
}): ReachPick {
	let steps = 0;
	const collect = (allowed: TempoLockClass | 'any'): AutoPlayTrackRow[] => {
		const rows: AutoPlayTrackRow[] = [];
		for (const row of input.candidates) {
			steps += 1;
			if (
				_matchesLock(
					input.current_key,
					input.current_bpm,
					row,
					input.min_tempo_ratio,
					input.max_tempo_ratio,
					allowed
				)
			) {
				rows.push(row);
			}
		}
		return rows;
	};
	// Exact pool first: bitwise identical to today's reachable. Fold is scanned
	// only when that pool is empty, so Warnsdorff degree on exact libraries
	// does not count half/double outward.
	let reachable = collect('exact');
	let degreeAllowed: TempoLockClass | 'any' = 'exact';
	if (reachable.length === 0) {
		reachable = collect('fold');
		degreeAllowed = 'any';
	}
	if (reachable.length === 0) return { next: null, steps, fell_back: false };

	const budgetNeeded = reachable.length * input.candidates.length;
	if (budgetNeeded > input.max_steps) {
		return { next: reachable[0].stable_id, steps, fell_back: true };
	}

	let best: AutoPlayTrackRow | null = null;
	let bestDegree = Number.POSITIVE_INFINITY;
	let bestHasOut = false;
	for (const cand of reachable) {
		let degree = 0;
		for (const other of input.candidates) {
			if (other.stable_id === cand.stable_id) continue;
			steps += 1;
			if (
				_matchesLock(
					cand.key,
					cand.bpm,
					other,
					input.min_tempo_ratio,
					input.max_tempo_ratio,
					degreeAllowed
				)
			) {
				degree += 1;
			}
		}
		const hasOut = degree >= 1;
		if (best === null) {
			best = cand;
			bestDegree = degree;
			bestHasOut = hasOut;
			continue;
		}
		// Prefer any non-dead-end over a dead end; among same class, fewest onward.
		if (hasOut && !bestHasOut) {
			best = cand;
			bestDegree = degree;
			bestHasOut = true;
		} else if (hasOut === bestHasOut && degree < bestDegree) {
			best = cand;
			bestDegree = degree;
		}
		// else keep earlier membership (first reachable wins ties)
	}
	return { next: best?.stable_id ?? null, steps, fell_back: false };
}

// ----- same recording -----------------------------------------------------

/*
 * PLAY-16: one recording often sits in a library several times (a playlist
 * copy, a USB import, a "6 - " energy-prefixed MIK copy). Every copy has the
 * same key and BPM as the playing track, so the compatibility pick rates it
 * the best next track there is. AutoPlay once filled all four decks with
 * four different stable_ids of one recording that
 * way. Rows are told apart by stable_id; this is the second, recording-level
 * no-repeat rule. A remix keeps its suffix, so it stays a different recording.
 */
const _identityByRow = new WeakMap<AutoPlayTrackRow, string | null>();

/** Lower-cased primary artist + title without leading "N - " / "9A - " prefixes; null when untitled. */
export function autoPlaySongIdentity(
	title: string | null | undefined,
	artist: string | null | undefined
): string | null {
	if (typeof title !== 'string') return null;
	const name = title
		.replace(/^(?:\d{1,2}[AB]?\s*-\s*){1,2}/i, '')
		.trim()
		.replace(/\s+/g, ' ')
		.toLowerCase();
	if (name.length === 0) return null;
	const lead = (artist ?? '').split(/\s*[,;/&]\s*|\s+(?:feat\.?|ft\.?|x)\s+/i)[0].trim().toLowerCase();
	return `${lead}|${name}`;
}

function _rowIdentity(row: AutoPlayTrackRow): string | null {
	let identity = _identityByRow.get(row);
	if (identity === undefined) {
		identity = autoPlaySongIdentity(row.title, row.artist);
		_identityByRow.set(row, identity);
	}
	return identity;
}

/** True for a row that is another copy of the current, a loaded, an excluded or a played recording. */
export function sameRecordingAsSpent(input: {
	playlist: readonly AutoPlayTrackRow[];
	current_stable_id: string;
	exclude_ids: ReadonlySet<string>;
	played_ids: ReadonlySet<string>;
	exclude_identities?: ReadonlySet<string>;
}): (row: AutoPlayTrackRow) => boolean {
	const spent = new Set<string>(input.exclude_identities ?? []);
	for (const row of input.playlist) {
		const id = row.stable_id;
		if (id !== input.current_stable_id && !input.exclude_ids.has(id) && !input.played_ids.has(id)) continue;
		const identity = _rowIdentity(row);
		if (identity !== null) spent.add(identity);
	}
	return (row) => {
		const identity = _rowIdentity(row);
		return identity !== null && spent.has(identity);
	};
}

/**
 * Next AutoPlay track from the open playlist.
 *
 * - enforce_play_order: first membership row after current not excluded/played.
 * - default (smart): earliest un-played membership row, walked starting AFTER
 *   current's own position in the playlist and wrapping to the rows before it
 *   only once that forward stretch is exhausted, with Camelot key +-1 and BPM
 *   inside Beat Sync pitch bounds vs the source (exact raw-ratio first;
 *   half/double fold only when nothing exact is reachable).
 * - maximize_reach (smart only): Warnsdorff slack pick among compatible rows,
 *   same forward-then-wrap candidate order for its tie-breaks.
 *
 * Pin 0a047b8a4e4d: a manually loaded (double-clicked) track sits wherever the
 * user put it in the current view, not at row 0. Filtering candidates by
 * `!played` alone is not enough - every row before that position is also
 * un-played, so without this ordering the picker walks straight back to the
 * top of the list the instant the user starts mid-view rather than at
 * membership row 0.
 */
export function pickNextStableId(input: {
	playlist: readonly AutoPlayTrackRow[];
	current_stable_id: string;
	current_key: string | null;
	current_bpm: number | null;
	exclude_ids: ReadonlySet<string>;
	played_ids: ReadonlySet<string>;
	enforce_play_order: boolean;
	min_tempo_ratio: number;
	max_tempo_ratio: number;
	maximize_reach?: boolean;
	max_reach_steps?: number;
	/** PLAY-16: recordings already on a deck that the playlist cannot name. */
	exclude_identities?: ReadonlySet<string>;
}): string | null {
	const current = input.current_stable_id;
	const played = input.played_ids;
	const exclude = input.exclude_ids;
	const currentIdx = input.playlist.findIndex((r) => r.stable_id === current);

	if (input.enforce_play_order) {
		for (let i = Math.max(0, currentIdx + 1); i < input.playlist.length; i++) {
			const row = input.playlist[i];
			const id = row.stable_id;
			if (id === current || exclude.has(id) || played.has(id)) continue;
			if (!row.file_exists) continue;
			return id;
		}
		return null;
	}

	// currentIdx === -1 (current not a member, e.g. a fresh playlist switch):
	// there is no "after" to prefer, so walk the list as published.
	const orderedFromCurrent =
		currentIdx === -1
			? input.playlist
			: input.playlist.slice(currentIdx + 1).concat(input.playlist.slice(0, currentIdx));

	const repeatsARecording = sameRecordingAsSpent(input);
	const candidates = orderedFromCurrent.filter((row) => {
		const id = row.stable_id;
		return id !== current && !exclude.has(id) && !played.has(id) && row.file_exists && !repeatsARecording(row);
	});

	if (input.maximize_reach) {
		const pick = pickNextMaximizingReach({
			candidates,
			current_key: input.current_key,
			current_bpm: input.current_bpm,
			min_tempo_ratio: input.min_tempo_ratio,
			max_tempo_ratio: input.max_tempo_ratio,
			max_steps: input.max_reach_steps ?? AUTO_PLAY_REACH_MAX_STEPS
		});
		return pick.next;
	}

	for (const row of candidates) {
		if (
			_lockClass(
				input.current_key,
				input.current_bpm,
				row,
				input.min_tempo_ratio,
				input.max_tempo_ratio
			) === 'exact'
		) {
			return row.stable_id;
		}
	}
	for (const row of candidates) {
		if (
			_lockClass(
				input.current_key,
				input.current_bpm,
				row,
				input.min_tempo_ratio,
				input.max_tempo_ratio
			) === 'fold'
		) {
			return row.stable_id;
		}
	}
	return null;
}

/**
 * Replay the whole AutoPlay chain from a start row (greedy, slack, or enforce).
 * Used to assert last-in-order == last accessible under the picker, and to
 * publish the library AutoPlay column's rank map.
 */
export function simulateAutoPlayChain(input: {
	playlist: readonly AutoPlayTrackRow[];
	start_stable_id: string;
	start_key?: string | null;
	start_bpm?: number | null;
	enforce_play_order: boolean;
	maximize_reach: boolean;
	min_tempo_ratio: number;
	max_tempo_ratio: number;
	exclude_ids?: ReadonlySet<string>;
	played_ids?: ReadonlySet<string>;
	max_reach_steps?: number;
	/**
	 * Stop after this many rows (start row included). The library column only
	 * needs the near future; walking all 8558 rows under maximize_reach cost
	 * 6.9 s of main thread per simulation (Wed 2 Sep 2026). Unset = whole chain,
	 * which the order-invariant tests rely on.
	 */
	max_chain_length?: number;
	/** The live chart passes its fallback-aware picker; compatibility explainers
	 * retain the default compatibility-only policy. Both replay this same walk. */
	select_next?: typeof pickNextStableId;
	/** PLAY-16: forwarded to every pick (see `pickNextStableId`). */
	exclude_identities?: ReadonlySet<string>;
}): readonly string[] {
	const selectNext = input.select_next ?? pickNextStableId;
	const played = new Set<string>(input.played_ids ?? []);
	played.add(input.start_stable_id);
	const exclude = input.exclude_ids ?? new Set<string>();
	const chain: string[] = [input.start_stable_id];
	let current = input.start_stable_id;
	// One index instead of a linear find per step: the chain can be as long as
	// the playlist, which made this O(rows^2) on its own.
	const byId = new Map(input.playlist.map((r) => [r.stable_id, r] as const));
	const limit = input.max_chain_length ?? Number.POSITIVE_INFINITY;
	while (chain.length < limit) {
		const cur = byId.get(current);
		const currentKey = cur === undefined ? (input.start_key ?? null) : cur.key;
		const currentBpm = cur === undefined ? (input.start_bpm ?? null) : cur.bpm;
		const next = selectNext({
			playlist: input.playlist,
			current_stable_id: current,
			current_key: currentKey,
			current_bpm: currentBpm,
			exclude_ids: exclude,
			played_ids: played,
			enforce_play_order: input.enforce_play_order,
			min_tempo_ratio: input.min_tempo_ratio,
			max_tempo_ratio: input.max_tempo_ratio,
			maximize_reach: input.maximize_reach,
			...(input.exclude_identities === undefined ? {} : { exclude_identities: input.exclude_identities }),
			...(input.max_reach_steps === undefined
				? {}
				: { max_reach_steps: input.max_reach_steps })
		});
		if (next === null) break;
		played.add(next);
		chain.push(next);
		current = next;
	}
	return chain;
}
