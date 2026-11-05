/**
 * AUDFID-03: the live fidelity guard's loop, over injected deps so the
 * episode, toast and client_errors behaviour is testable without Web Audio.
 * The engine supplies `sample` (master bus tap + source decode window, or why
 * this tick is skipped); this module never imports the engine.
 */
import {
	compareFidelity,
	fidelityStats,
	fidelityToastText,
	FIDELITY_EPISODE_IDLE,
	FIDELITY_GUARD_CFG,
	stepFidelityEpisode,
	type FidelityEpisode,
	type FidelityVerdict,
	type StereoWindow
} from '$lib/player/fidelity-guard';

export type FidelitySample = { master: StereoWindow; source: StereoWindow } | { skip: string };

/** What agents read (mixer headphones `fidelity_guard`). */
export interface FidelityGuardState {
	status: 'idle' | 'ok' | 'degraded' | 'skipped';
	reason: string | null;
	checked_at_ms: number | null;
}

export interface FidelityGuardDeps {
	sample(): FidelitySample;
	toast(text: string): void;
	reportClientError(reason: string, verdict: FidelityVerdict): void;
	publish(state: FidelityGuardState): void;
	now(): number;
}

export interface FidelityGuard {
	tick(): FidelityVerdict;
	readonly episode: FidelityEpisode;
}

export function createFidelityGuard(deps: FidelityGuardDeps): FidelityGuard {
	let episode: FidelityEpisode = FIDELITY_EPISODE_IDLE;
	return {
		get episode() {
			return episode;
		},
		tick(): FidelityVerdict {
			const sample = deps.sample();
			const verdict: FidelityVerdict =
				'skip' in sample
					? { kind: 'skip', reason: sample.skip }
					: compareFidelity(fidelityStats(sample.master), fidelityStats(sample.source));
			const step = stepFidelityEpisode(episode, verdict);
			episode = step.state;
			if (step.fire !== null) {
				deps.toast(fidelityToastText(step.fire));
				deps.reportClientError(step.fire, verdict);
			}
			const status: FidelityGuardState['status'] =
				verdict.kind === 'skip' ? 'skipped' : episode.firing !== null ? 'degraded' : 'ok';
			deps.publish({
				status,
				reason:
					verdict.kind === 'ok'
						? null
						: verdict.kind === 'skip'
							? verdict.reason
							: (episode.firing ?? verdict.reason),
				checked_at_ms: deps.now()
			});
			return verdict;
		}
	};
}

/** Run `guard.tick` every CFG tick; returns the stop function. A tick that
 * throws is reported once per message and the loop keeps running. */
export function startFidelityGuardLoop(
	guard: FidelityGuard,
	onError: (error: unknown) => void,
	intervalMs: number = FIDELITY_GUARD_CFG.TICK_MS
): () => void {
	const timer = setInterval(() => {
		try {
			guard.tick();
		} catch (error) {
			onError(error);
		}
	}, intervalMs);
	// A pure observer must never keep a process alive (node tests, workers).
	(timer as { unref?: () => void }).unref?.();
	return () => clearInterval(timer);
}
