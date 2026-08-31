/**
 * The account panel's data: identity, plan, and the local-storage disclosure.
 *
 * ONE SOURCE. Everything the panel shows comes from `GET /api/v1/account`,
 * including the sentence that says sign-in is identity rather than
 * authorisation and the list of what is stored on this machine. The wording is
 * NOT restated here: the daemon is what has to be right about its own storage,
 * and a second copy in the UI is a second thing to forget to update when the
 * schema changes.
 *
 * THE DESTRUCTIVE ACTION carries the daemon's own typed confirm
 * (`?confirm=delete-my-local-account-data`). The token is spelled here once so
 * the panel cannot send a subtly different one and get a 422 the user cannot
 * act on; the server is still the thing that enforces it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 load() surfaces a failure verbatim and leaves `account` null, so the
 *     panel shows an error rather than an empty disclosure.
 *     [if] a failed load renders as "nothing is stored about you" [then ⛔️]
 *   ✔︎ 🎯 deleteAccount() refreshes identity afterwards, so the bauble cannot
 *     keep showing an avatar for a row that no longer exists.
 *     [if] the UI stays signed in after a successful delete [then ⛔️] broken
 */

import { api, unwrap } from '$lib/api/client';
import type { components } from '$lib/api-types';
import { refreshUser } from '$lib/auth.svelte';

export type AccountOut = components['schemas']['AccountOut'];

/** The daemon's typed confirm for DELETE /api/v1/account. Must match
 * `apps/engine_core/account/api.py:ACCOUNT_DELETE_CONFIRM` exactly. */
export const ACCOUNT_DELETE_CONFIRM = 'delete-my-local-account-data';

function _message(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

class AccountStore {
	account = $state<AccountOut | null>(null);
	loading = $state(false);
	error = $state<string | null>(null);
	/** Set after a successful delete, so the panel can say what happened. */
	deleted = $state<string | null>(null);

	/** Fetch the account. Not memoized: the panel reopens after a sign-in or a
	 * delete, and a stale memo would show the previous identity. */
	async load(): Promise<void> {
		this.loading = true;
		try {
			this.account = (await unwrap(api.GET('/api/v1/account'))) as AccountOut;
			this.error = null;
		} catch (exc) {
			this.account = null;
			this.error = _message(exc);
		} finally {
			this.loading = false;
		}
	}

	/**
	 * Erase the account row and every session it owns.
	 *
	 * Returns null on success or the daemon's sentence on failure. Never
	 * throws: every failure here already has a message a person can read, and
	 * a caller that had to catch would only translate it back into one.
	 */
	async deleteAccount(): Promise<string | null> {
		this.loading = true;
		try {
			const body = await unwrap(
				api.DELETE('/api/v1/account', {
					params: { query: { confirm: ACCOUNT_DELETE_CONFIRM } }
				})
			);
			this.deleted = body.message;
			this.error = null;
			// The cookie is cleared server-side; re-ask who we are so the
			// bauble and this panel agree that nobody is signed in.
			await refreshUser();
			await this.load();
			return null;
		} catch (exc) {
			this.error = _message(exc);
			return this.error;
		} finally {
			this.loading = false;
		}
	}

	_resetForTests(): void {
		this.account = null;
		this.loading = false;
		this.error = null;
		this.deleted = null;
	}
}

export const accountStore = new AccountStore();
