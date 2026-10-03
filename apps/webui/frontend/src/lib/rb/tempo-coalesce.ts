/**
 * S1 (round 2): queued tempo coalescing for the performance dispatcher.
 *
 * Every queued tempo takes a ticket. A queued tempo that a newer tempo for the
 * same deck has overtaken runs as a no-op when its turn comes, so a fader
 * sweep on a busy synced master costs one group re-lock, not one per message.
 *
 * Only a contiguous run of tempos coalesces. A command queued between two
 * tempos whose scopes overlap the tempo's (`[deck, 'sync']`) is a barrier: the
 * tempo before it is pinned and still executes, so that command runs at the
 * pitch the DJ had set when they pressed it, not the one before.
 */
export class TempoCoalescer<Deck, Scope> {
	readonly #latest = new Map<Deck, number>();
	readonly #pinned = new Set<number>();
	#tickets = 0;

	/** Ticket for a queued command: a positive number for a tempo, 0 otherwise.
	 * `scopes` are the command's queue scopes; `sync` stands for every deck. */
	mark(isTempo: boolean, deck: Deck, scopes: readonly Scope[], sync: Scope): number {
		if (isTempo) {
			const ticket = ++this.#tickets;
			this.#latest.set(deck, ticket);
			return ticket;
		}
		const all = scopes.includes(sync);
		for (const [tempoDeck, ticket] of this.#latest) {
			if (all || scopes.some((scope) => Object.is(scope, tempoDeck))) {
				this.#pinned.add(ticket);
				this.#latest.delete(tempoDeck);
			}
		}
		return 0;
	}

	/** Whether the command holding `ticket` executes now that its turn came. */
	runs(deck: Deck, ticket: number): boolean {
		if (!ticket) return true;
		const latest = this.#latest.get(deck) === ticket;
		if (latest) this.#latest.delete(deck);
		return this.#pinned.delete(ticket) || latest;
	}

	/** Entries still held, for the leak check in its tests. */
	get held(): number {
		return this.#latest.size + this.#pinned.size;
	}
}
