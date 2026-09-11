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

/** The one predicate for "this claim was dropped by an invalidation, not by a
 * failure". Discriminates on `name`, the same way the routes already
 * discriminate an `AbortError`, so it holds for an error that crossed a module
 * boundary rather than only for one built by this exact class object. */
export function isScopedCommandInvalidated(error: unknown): boolean {
	return error instanceof Error && error.name === 'ScopedCommandInvalidatedError';
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

	/** Claim one idle scope without a promise-turn delay. Permission APIs that
	 * require a trusted click use this path and must never wait in the queue. */
	runImmediatelyIfIdle<T>(scope: Scope, command: () => Promise<T>): Promise<T> {
		if (this.#tails.has(scope)) throw new Error(`command scope ${String(scope)} is busy`);
		const generation = this.#generation;
		let result: Promise<T>;
		try {
			result = Promise.resolve(command());
		} catch (error) {
			result = Promise.reject(error);
		}
		const guarded = result.then((value) => {
			if (generation !== this.#generation) {
				throw new ScopedCommandInvalidatedError(this.#invalidationReason);
			}
			return value;
		});
		const tail = guarded.then(
			() => undefined,
			() => undefined
		);
		this.#tails.set(scope, tail);
		void tail.then(() => {
			if (this.#tails.get(scope) === tail) this.#tails.delete(scope);
		});
		return guarded;
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
