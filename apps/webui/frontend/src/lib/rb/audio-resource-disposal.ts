interface AudioDisconnectable {
	disconnect(): void;
}

interface AudioProcessorDisposable extends AudioDisconnectable {
	dispose(): Promise<void>;
}

interface AudioContextDisposable {
	readonly state: AudioContextState;
	close(): Promise<void>;
}

interface AudioResources {
	rafId: number | null;
	processors: readonly AudioProcessorDisposable[];
	nodes: readonly AudioDisconnectable[];
	masterGain: AudioDisconnectable | null;
	context: AudioContextDisposable | null;
}

/**
 * Synchronously silence an owned audio graph, then retire processors and close
 * the context. Disconnection happens before the first await so route teardown
 * cannot leave playback running invisibly while asynchronous cleanup settles.
 */
export async function disposeAudioResources(
	resources: AudioResources,
	cancelFrame?: (rafId: number) => void
): Promise<void> {
	const failures: unknown[] = [];
	const attempt = (operation: () => void): void => {
		try {
			operation();
		} catch (error) {
			failures.push(error);
		}
	};
	const rafId = resources.rafId;
	if (rafId !== null) {
		attempt(() => {
			const cancel = cancelFrame ?? globalThis.cancelAnimationFrame;
			if (cancel === undefined) {
				throw new Error('cannot dispose active audio clock: cancelAnimationFrame is unavailable');
			}
			cancel(rafId);
		});
	}
	for (const processor of resources.processors) attempt(() => processor.disconnect());
	for (const node of resources.nodes) attempt(() => node.disconnect());
	const masterGain = resources.masterGain;
	if (masterGain !== null) attempt(() => masterGain.disconnect());
	const outcomes = await Promise.allSettled(
		resources.processors.map((processor) => processor.dispose())
	);
	for (const outcome of outcomes) {
		if (outcome.status === 'rejected') failures.push(outcome.reason);
	}
	if (resources.context !== null && resources.context.state !== 'closed') {
		try {
			await resources.context.close();
		} catch (error) {
			failures.push(error);
		}
	}
	if (failures.length === 1) throw failures[0];
	if (failures.length > 1) throw new AggregateError(failures, 'multiple audio teardown operations failed');
}

/** Detaches an owner's worklet/processor node for disposal without leaving it
 * reachable afterward. Lives here (not audio-engine.svelte.ts, file-size
 * ratchet) alongside the `AudioDisconnectable` shape it shares. */
export function detachProcessorForDisposal<T extends AudioDisconnectable>(owner: {
	processor: T | null;
}): T | null {
	const processor = owner.processor;
	owner.processor = null;
	return processor;
}
