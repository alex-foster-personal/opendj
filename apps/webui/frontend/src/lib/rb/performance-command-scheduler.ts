/**
 * Promise scheduler with explicit overlapping scopes.
 *
 * Commands sharing any scope run in submission order. Commands whose scopes
 * do not overlap start independently, so a slow load on one deck cannot
 * stall another deck. A shared sync scope is used only for engine operations
 * that can coordinate a master and followers.
 */
export class ScopedCommandScheduler<Scope extends PropertyKey> {
	readonly #tails = new Map<Scope, Promise<void>>();

	async run<T>(scopes: readonly Scope[], command: () => Promise<T>): Promise<T> {
		if (scopes.length === 0) throw new Error('command requires at least one scope');
		const uniqueScopes = new Set(scopes);
		if (uniqueScopes.size !== scopes.length) throw new Error('command contains a duplicate scope');

		const predecessors = new Set<Promise<void>>();
		for (const scope of uniqueScopes) {
			const tail = this.#tails.get(scope);
			if (tail !== undefined) predecessors.add(tail);
		}
		const result = Promise.all(predecessors).then(command);
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
