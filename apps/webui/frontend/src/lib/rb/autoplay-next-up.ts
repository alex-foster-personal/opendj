/**
 * NATIVE-5886: tell the engine which tracks AutoPlay will load next, so the
 * ahead-analysis drain fills their beatgrid and key WHILE a deck plays. Without
 * it the drain pauses for the whole set, and a gridless next-up refused the
 * master handover on silver (Wed 7 Oct 2026, 16:11:29Z): silence.
 *
 * The charted chain (`publishAutoPlayOrder`) is the source; only its head is
 * sent. One PUT is in flight at a time: a newer chain replaces any unsent
 * head. lastSent is the key the server accepted, including a response whose
 * chain is no longer the one the page wants; pump then sends the desired
 * chain. A failed PUT clears the chain the server was assumed to hold, then
 * retries the latest desired chain, so a lost response cannot leave the
 * server on a chain the page has already replaced.
 *
 * Retries are bounded. Each failure waits the next delay in RETRY_DELAYS_MS;
 * after the last one the page stops asking and logs that it stopped. An
 * engine that keeps refusing (503 while its drain is not armed) or cannot be
 * reached is therefore asked a fixed number of times, not every few seconds
 * for the rest of the session. A changed chain or a newly observed engine
 * boot starts a fresh schedule.
 *
 * The engine keeps its list across a page reload and loses it on its own
 * restart, so every newly observed engine boot is a sync point: the chain the
 * page wants, empty included, is sent again. A PUT that was in flight across
 * that boot confirms nothing, because nobody can say which process answered;
 * nor does one in flight while this tab lost and regained the lease, or
 * while its route unbound and bound a lease that already leads.
 * Only the tab that holds the engine-confirmed leader lease writes (AGENT-18,
 * the same gate as the UI mirror). The engine keeps ONE list, so a second
 * tab, or the library desk with no AutoPlay at all, would otherwise overwrite
 * the playing tab's chain with its own (an empty one, from its first health
 * ping). A tab with no leadership bound writes nothing; a tab that becomes
 * leader sends the chain it wants, because the list it finds is another tab's.
 *
 * CLI twin: `python -m apps.webui.ahead_analysis_cli next-up`.
 */
import { api, unwrap } from '$lib/api/rb-http';
import type { TabLeadership } from '$lib/rb/tab-leadership';

/** How many charted tracks ahead get analyzed while playing (the engine keeps at most 4). */
export const NEXT_UP_COUNT = 2;

/**
 * Wait before each retry of a failed PUT, in order. The first is soon so a
 * failed localhost PUT recovers without a chart change. About a minute in all;
 * after the last delay's retry fails the page stops (see `stoppedKey`).
 */
export const RETRY_DELAYS_MS: readonly number[] = [50, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000];

/** Key the server accepted, including a response whose chain the page no longer wants. */
let lastSent: string | null = null;
let desiredKey: string | null = null;
let desiredIds: string[] = [];
let inFlight = false;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
/** Failed PUTs in a row since the last success, chain change or new boot. */
let failures = 0;
/** The chain the page stopped retrying, or null while it is still trying. */
let stoppedKey: string | null = null;
/**
 * False until an engine has been seen or a PUT has started. Before that an
 * empty chain has nowhere to go. After it an empty chain IS a request: a
 * previous page may have left tracks on the engine, and a failed clear must
 * still retry because the server may hold the chain the page is dropping.
 */
let engineMayHoldChain = false;

/** Stands in for the boot of a daemon whose health carries no boot id. */
const NO_BOOT_IDENTITY = Symbol('daemon without a boot id');

/** Boot last observed. The drain's next-up list dies with the process. */
let engineBoot: string | typeof NO_BOOT_IDENTITY | null = null;

/**
 * Counts sync points: an observed boot, or this tab becoming leader (the list
 * is then another tab's). An ack from before one confirms nothing.
 */
let syncGeneration = 0;

type Leadership = Pick<TabLeadership, 'isLeader' | 'subscribe'>;

/** The lease this tab writes under; null (no /performance route mounted) writes nothing. */
let leadership: Leadership | null = null;
/** This tab's ui-mirror client id, sent as the lease header so the engine refuses a former leader. */
let writerId: string | null = null;

/**
 * Write only while `lease` leads. On BECOMING leader the list on the engine is
 * the previous leader's, so the chain this tab wants is sent again, empty
 * included. Returns the unbind.
 */
export function bindNextUpLeadership(lease: Leadership, clientId: string): () => void {
	if (clientId === '') throw new Error('next-up writer client id is empty');
	leadership = lease;
	writerId = clientId;
	let led = lease.isLeader();
	const unsubscribe = lease.subscribe(() => {
		const leads = lease.isLeader();
		if (leads && !led && leadership === lease) beginLeaderTenure(true);
		led = leads;
	});
	// A lease that already leads when bound is a new tenure too. Module state
	// outlives a route remount, and while no lease was bound another tab may
	// have held the lease and replaced the list this tab last sent, so lastSent
	// confirms nothing. Before any engine is known there is nothing to clear
	// yet: the first boot does that (noteEngineBoot).
	if (led) beginLeaderTenure(engineMayHoldChain);
	return () => {
		unsubscribe();
		if (leadership === lease) {
			leadership = null;
			writerId = null;
		}
	};
}

/**
 * Record the engine boot the health probe just read. `null` is the legacy
 * daemon, which has no boot id: it is observed once and its restarts cannot
 * be told apart.
 *
 * Every newly observed boot sends the chain the page wants, empty included.
 * On the first one that clears whatever a previous page left behind (a
 * reload keeps the engine and its list). On a later one it refills a list
 * that died with the old process.
 */
export function noteEngineBoot(bootId: string | null): void {
	if (bootId === '') throw new Error('engine boot id is empty');
	const observed = bootId === null ? NO_BOOT_IDENTITY : bootId;
	if (observed === engineBoot) return;
	engineBoot = observed;
	syncGeneration += 1;
	engineMayHoldChain = true;
	lastSent = null;
	startFreshSchedule();
	if (desiredKey === null) {
		desiredIds = [];
		desiredKey = '';
	}
	pump();
}


export function nextUpIds(chain: readonly string[]): string[] {
	return chain.slice(0, NEXT_UP_COUNT);
}

export function publishAutoPlayNextUp(chain: readonly string[]): void {
	const ids = nextUpIds(chain);
	const key = ids.join('\n');
	if (key !== desiredKey) startFreshSchedule();
	desiredIds = ids;
	desiredKey = key;
	pump();
}

/**
 * This tab starts writing under a new tenure: the list on the engine is not
 * known to be the one it last sent. `clear` also sends the empty chain when
 * nothing is charted, because the list found there is another tab's.
 */
function beginLeaderTenure(clear: boolean): void {
	syncGeneration += 1;
	lastSent = null;
	startFreshSchedule();
	if (clear) {
		engineMayHoldChain = true;
		if (desiredKey === null) {
			desiredIds = [];
			desiredKey = '';
		}
	}
	pump();
}

function startFreshSchedule(): void {
	failures = 0;
	stoppedKey = null;
}

function pump(): void {
	if (inFlight) return;
	const writer = writerId;
	if (leadership === null || writer === null || !leadership.isLeader()) return;
	if (desiredKey === null || desiredKey === lastSent) return;
	if (desiredKey === stoppedKey) return;
	if (!engineMayHoldChain && desiredKey === '') return;
	if (retryTimer !== null) {
		clearTimeout(retryTimer);
		retryTimer = null;
	}
	inFlight = true;
	engineMayHoldChain = true;
	const ids = desiredIds;
	const key = desiredKey;
	const generation = syncGeneration;
	void unwrap(
		api.PUT('/api/v1/ahead-analysis/next-up', {
			params: { header: { 'x-opendj-lease': writer } },
			body: { stable_ids: ids }
		})
	)
		.then(() => {
			inFlight = false;
			// A boot, or a lost and regained lease, while this PUT was in
			// flight already cleared lastSent. Writing the key back would mark
			// the chain confirmed on a list that may have changed since.
			lastSent = generation === syncGeneration ? key : null;
			failures = 0;
			pump();
		})
		.catch((error: unknown) => {
			inFlight = false;
			// A lost response may already have been applied. Forget the chain
			// we thought the server held, or a desired chain equal to it
			// looks confirmed and armRetry sends nothing.
			lastSent = null;
			failures += 1;
			if (failures > RETRY_DELAYS_MS.length) {
				stoppedKey = desiredKey;
				console.error(
					`[autoplay] next-up analysis request failed ${failures} times in a row; stopped asking ` +
						'until the charted order changes or the engine restarts. Tracks charted next are not ' +
						'analyzed while a deck plays until then.',
					error
				);
				return;
			}
			console.warn('[autoplay] next-up analysis request failed', error);
			armRetry(RETRY_DELAYS_MS[failures - 1]);
		});
}

function armRetry(delay: number): void {
	if (retryTimer !== null) return;
	if (desiredKey === null || desiredKey === lastSent) return;
	retryTimer = setTimeout(() => {
		retryTimer = null;
		pump();
	}, delay);
	if (typeof retryTimer === 'object' && retryTimer !== null && 'unref' in retryTimer) {
		(retryTimer as { unref: () => void }).unref();
	}
}
