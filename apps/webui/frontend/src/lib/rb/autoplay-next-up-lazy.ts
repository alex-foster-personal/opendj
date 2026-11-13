import { isPageGoingAway } from '$lib/api/lifecycle-abort';

/**
 * First-use loader for the NATIVE-5886 next-up client (`autoplay-next-up.ts`).
 *
 * The client is not first-paint code, so "/" does not carry it: the first
 * call fetches it. Every call goes through ONE module promise, so calls reach
 * the client in the order they were made, exactly as the static imports did.
 * The arguments are copied now, because the chain is a reactive array that
 * can change before the chunk arrives.
 */
type NextUpClient = typeof import('$lib/rb/autoplay-next-up');

/**
 * `load` once, shared by every caller. A load that rejects is not kept: the
 * next call loads again, so one failed chunk fetch does not disable the
 * client for the rest of the session.
 */
export function onceUntilFailure<T>(load: () => Promise<T>): () => Promise<T> {
	let pending: Promise<T> | null = null;
	return () =>
		(pending ??= load().catch((error: unknown) => {
			pending = null;
			throw error;
		}));
}

/**
 * The loader around `load`. A leadership bind whose load failed is kept and
 * replayed by the next load that succeeds (a later boot or chart call), so one
 * failed chunk fetch at mount does not leave the tab's writes gated off. The
 * calls a failed load dropped are replayed the same way, as the latest boot
 * and the latest chart only: an unchanged chart is never published again by
 * the page, and a chart that was replaced since must not be.
 */
export function lazyNextUpClient(load: () => Promise<NextUpClient>) {
	const loadClient = onceUntilFailure(load);
	let pendingBind: ((next: NextUpClient) => void) | null = null;
	let lost = false;
	let latestBoot: string | null | undefined;
	let latestChart: string[] | undefined;

	function withClient(call: (next: NextUpClient) => void): void {
		loadClient()
			.then((next) => {
				pendingBind?.(next);
				if (lost) {
					lost = false;
					if (latestBoot !== undefined) next.noteEngineBoot(latestBoot);
					if (latestChart !== undefined) next.publishAutoPlayNextUp(latestChart);
				}
				call(next);
			})
			.catch((error: unknown) => {
				lost = true;
				if (isPageGoingAway()) return;
				console.error('[autoplay] next-up analysis client failed to load or run', error);
			});
	}

	return {
		publishAutoPlayNextUp(chain: readonly string[]): void {
			const copy = [...chain];
			latestChart = copy;
			withClient((next) => next.publishAutoPlayNextUp(copy));
		},

		/** Gate this tab's next-up writes on `lease` (see autoplay-next-up.ts). Returns the unbind. */
		bindNextUpLeadership(
			lease: Parameters<NextUpClient['bindNextUpLeadership']>[0],
			clientId: string
		): () => void {
			let unbind: (() => void) | null = null;
			const bind = (next: NextUpClient): void => {
				if (pendingBind !== bind) return;
				pendingBind = null;
				unbind = next.bindNextUpLeadership(lease, clientId);
			};
			pendingBind = bind;
			withClient(bind);
			return () => {
				if (pendingBind === bind) pendingBind = null;
				unbind?.();
			};
		},

		noteEngineBoot(bootId: string | null): void {
			// Kept synchronous: the health probe reports this as an engine error.
			if (bootId === '') throw new Error('engine boot id is empty');
			latestBoot = bootId;
			withClient((next) => next.noteEngineBoot(bootId));
		}
	};
}

export const { publishAutoPlayNextUp, bindNextUpLeadership, noteEngineBoot } = lazyNextUpClient(
	() => import('$lib/rb/autoplay-next-up')
);
