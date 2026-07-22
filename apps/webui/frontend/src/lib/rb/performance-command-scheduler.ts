/**
 * Promise scheduler with explicit overlapping scopes.
 *
 * Commands sharing any scope run in submission order. Commands whose scopes
 * do not overlap start independently, so a slow load on one deck cannot
 * stall another deck. A shared sync scope is used only for engine operations
 * that can coordinate a master and followers.
 *
 * Route-session invalidation clears current tails and rejects every old
 * command that has not begun. In-flight work remains the audio engine's
 * disposal responsibility.
 */
export class ScopedCommandInvalidatedError extends Error {
	constructor(reason: string) {
		super(reason);
		this.name = 'ScopedCommandInvalidatedError';
	}
}

export class ScopedCommandScheduler<Scope extends PropertyKey> {
	readonly #tails = new Map<Scope, Promise<void>>();
	#generation = 0;
	#invalidationReason = 'scoped command session was invalidated';

	invalidateQueued(reason: string): void {
		if (typeof reason !== 'string' || reason.trim() === '') {
			throw new TypeError('command invalidation reason must be a non-empty string');
		}
		this.#generation += 1;
		this.#invalidationReason = reason;
		this.#tails.clear();
	}

	async run<T>(scopes: readonly Scope[], command: () => Promise<T>): Promise<T> {
		if (scopes.length === 0) throw new Error('command requires at least one scope');
		const uniqueScopes = new Set(scopes);
		if (uniqueScopes.size !== scopes.length) throw new Error('command contains a duplicate scope');
		const generation = this.#generation;

		const predecessors = new Set<Promise<void>>();
		for (const scope of uniqueScopes) {
			const tail = this.#tails.get(scope);
			if (tail !== undefined) predecessors.add(tail);
		}
		const result = Promise.all(predecessors).then(() => {
			if (generation !== this.#generation) {
				throw new ScopedCommandInvalidatedError(this.#invalidationReason);
			}
			return command();
		});
		const tail = result.then(
			() => undefined,
			() => undefined
		);
		for (const scope of uniqueScopes) this.#tails.set(scope, tail);
		void tail.then(() => {
			for (const scope of uniqueScopes) {
				if (this.#tails.get(scope) === tail) this.#tails.delete(scope);
			}
		});
		return result;
	}
}
