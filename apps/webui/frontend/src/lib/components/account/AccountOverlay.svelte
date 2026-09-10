<script lang="ts">
	/**
	 * THE account surface: who you are signed in as, what plan that puts you
	 * on, and exactly what is stored about you on this machine.
	 *
	 * SHAPE. An OVERLAY over the live app, following SetupOverlay rather than
	 * the settings catalog (constraint 5 in specs/saas-spec.md). Account
	 * management is a stateful flow that ends in an irreversible action, not a
	 * flat searchable preference, and it is raised through the one
	 * `openAccountOverlay()` door so more entry points cannot drift apart.
	 *
	 * THE HONESTY REQUIREMENT (ACCT-02) IS THE POINT OF THE PANEL. Showing a
	 * "plan" in an app where nothing is gated invites exactly the wrong
	 * inference, so the plan block leads with the daemon's own sentence saying
	 * sign-in is identity and not authorisation, in the panel's most prominent
	 * position rather than in a footnote. Every word of it comes from
	 * GET /api/v1/account; this component composes no claim of its own about
	 * what is stored or enforced.
	 *
	 * THE DISCLOSURE (ACCT-03) mirrors https://open-dj.com/privacy: one row per
	 * store, each naming the real file path, what is in it, and the exact
	 * request that erases it. The destructive action is behind a two-step
	 * confirm and sends the daemon's typed token, so neither a stray click nor
	 * a stray curl can fire it.
	 *
	 * AGENT-NATIVE PARITY. Every control here is one HTTP call:
	 *   Refresh        GET    /api/v1/account
	 *   Sign out       POST   /api/v1/auth/logout
	 *   Delete data    DELETE /api/v1/account?confirm=...
	 */
	import { onMount } from 'svelte';
	import { auth, logout, refreshUser } from '$lib/auth.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { accountStore } from '$lib/account/account-store.svelte';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import { entitlements, planRefusal } from '$lib/api/entitlements.svelte';
	import { buildFlags, storeBuildRefusal } from '$lib/api/store-build.svelte';
	import {
		accountOverlay,
		askAccountDeleteConfirm,
		cancelAccountDeleteConfirm,
		closeAccountOverlay
	} from '$lib/account/overlay.svelte';

	let busy = $state(false);

	const account = $derived(accountStore.account);
	const plan = $derived(account?.plan ?? null);
	const user = $derived(account?.user ?? null);

	// Reload every time the panel is raised: it reopens after a sign-in and
	// after a delete, and a stale body would show the previous identity. The
	// entitlement load is memoized on success, so re-asking here costs one
	// request on a page where the root layout has not got there yet and
	// nothing after that.
	$effect(() => {
		if (!accountOverlay.open) return;
		void accountStore.load();
		void entitlements.load();
		void buildFlags.load();
	});

	/**
	 * Every declared flag this BUILD does not have (SAND-01), read off the
	 * server's per-flag refusal rather than inferred from `enabled` here: a
	 * flag can be off because a developer switched it off locally, and saying
	 * "the App Store build removed it" about that would be the fourth state
	 * telling the same kind of lie the third one was added to stop.
	 */
	const absentInThisBuild = $derived(
		buildFlags.flags
			.map((flag) => ({ flag, refusal: storeBuildRefusal(flag.flag_id) }))
			.filter((row) => row.refusal !== null)
	);

	/**
	 * Flags that are OFF for a reason that is not the App Store build: a plain
	 * override, or an explicit MDT_FEATURE_FLAGS_FILE.
	 *
	 * These need their own row rather than falling through the gap between the
	 * two branches below. `storeBuildRefusal` returns null both for a flag that
	 * is ON and for one turned off locally - correct, because neither entitles
	 * anything to say "App Store" - so filtering only on `refusal !== null` left
	 * a locally-disabled flag listed nowhere, and the panel then rendered "Every
	 * capability openDJ ships is available in this build" while one was off.
	 * A panel whose whole job is to state what this build cannot do must not
	 * claim it can do everything (PR #1668 round-4 P2).
	 *
	 * The row carries NO App Store attribution, mirroring the split
	 * `usb_export.py::_disabled_response` already makes on the server: blaming
	 * Apple's sandbox for a decision this machine made on its own is the same
	 * misattribution the fourth state exists to prevent, pointed the other way.
	 */
	const disabledLocally = $derived(
		buildFlags.flags.filter(
			(flag) => !flag.enabled && storeBuildRefusal(flag.flag_id) === null
		)
	);

	/** Nothing is switched off, by the store build or by this machine. */
	const everythingAvailable = $derived(
		absentInThisBuild.length === 0 && disabledLocally.length === 0
	);

	onMount(() => {
		// This overlay is mounted at the root but starts closed, so its
		// who-am-I is the second /auth/me of the boot burst and the one
		// nobody can even see. Deferred (PERF-R6), not dropped: it still
		// runs, seconds later, off the critical path.
		bootScheduler.defer('account-overlay:refreshUser', () => {
			if (auth.user === null) void refreshUser();
		});
	});

	function onPanelKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') closeAccountOverlay();
	}

	async function onSignOut(): Promise<void> {
		busy = true;
		try {
			await logout();
			await accountStore.load();
		} catch (exc) {
			pushToast(exc instanceof Error ? exc.message : 'sign-out failed', 'error');
		} finally {
			busy = false;
		}
	}

	async function onDelete(): Promise<void> {
		busy = true;
		try {
			const failure = await accountStore.deleteAccount();
			if (failure !== null) {
				pushToast(failure, 'error', 12000);
				return;
			}
			cancelAccountDeleteConfirm();
			pushToast('account data deleted from this machine', 'info');
		} finally {
			busy = false;
		}
	}
</script>

{#if accountOverlay.open}
	<div class="ac-backdrop" role="presentation">
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<div
			class="ac-panel"
			role="dialog"
			aria-modal="true"
			aria-label="Account"
			tabindex="-1"
			onkeydown={onPanelKeydown}
		>
			<header class="ac-head">
				<h2>Account</h2>
				<button
					type="button"
					class="ac-btn"
					onclick={() => closeAccountOverlay()}
					title="Close the account panel. Nothing here is saved on close; every action took effect when you pressed it."
				>
					Close
				</button>
			</header>

			<div class="ac-body">
				{#if accountStore.error !== null}
					<p class="ac-error" role="alert">{accountStore.error}</p>
				{:else if account === null}
					<p class="ac-muted">Reading your account from the daemon...</p>
				{:else}
					<!-- ------------------------------------------- identity -->
					<section class="ac-section" aria-label="Identity">
						<h3>Signed in</h3>
						{#if user === null}
							<p class="ac-muted">
								Not signed in. openDJ works exactly the same either way; signing in only
								tells it who you are.
							</p>
						{:else}
							<dl class="ac-rows">
								<dt>Email</dt>
								<dd>{user.email}</dd>
								{#if user.name}
									<dt>Name</dt>
									<dd>{user.name}</dd>
								{/if}
								<dt title="Google's stable identifier for your account. It is what your data here is keyed on.">
									Google sub
								</dt>
								<dd class="ac-mono">{user.google_sub}</dd>
								<dt title="When this account row was first written on this machine.">First seen</dt>
								<dd>{user.created_at}</dd>
							</dl>
							<button
								type="button"
								class="ac-btn"
								disabled={busy}
								onclick={onSignOut}
								title="POST /api/v1/auth/logout. Ends this browser's session and deletes the Google tokens held for it. Your account row stays."
							>
								Sign out
							</button>
						{/if}
					</section>

					<!-- ----------------------------------------------- plan -->
					<section class="ac-section" aria-label="Plan">
						<h3>Plan</h3>
						<p class="ac-notice" role="note">{account.authorisation_note}</p>
						{#if plan}
							<dl class="ac-rows">
								<dt>Plan</dt>
								<dd>{plan.label}</dd>
								<dt title="The payment provider this plan came from. Empty means none is configured, so the plan gates nothing.">
									Payment provider
								</dt>
								<dd>{plan.provider ?? 'none configured'}</dd>
							</dl>
							<p class="ac-muted">{plan.note}</p>
						{/if}

						<!-- What the plan includes, read through the entitlement
						     seam rather than from a second list here, so this
						     panel and any gated control can never disagree. -->
						{#if entitlements.features.length === 0}
							<p class="ac-muted">
								No feature in this build is gated by a plan, so there is nothing here to
								include or leave out.
							</p>
						{:else}
							<ul class="ac-features">
								{#each entitlements.features as feature (feature.feature_id)}
									{@const refusal = planRefusal(feature.feature_id)}
									<li>
										<span class="ac-store-label">{feature.label}</span>
										{#if refusal === null}
											<span
												class="ac-muted"
												title={feature.quota === null
													? 'Included, with no limit on how much you may use.'
													: `Included, up to ${feature.quota} per billing period.`}
												>Included{feature.quota === null ? '' : ` (up to ${feature.quota})`}</span
											>
										{:else}
											<span class="ac-muted" title={refusal}>Not included</span>
										{/if}
									</li>
								{/each}
							</ul>
						{/if}
					</section>

					<!-- ----------------------------------------- this build -->
					<!-- SAND-01. The FOURTH reason a control is dead, and it
					     belongs beside the plan because a user hitting a dead
					     control has no way to tell "not on my plan" from "not
					     in this build" without being told which. It names no
					     download outside the store: guideline 3.2.2(vi) reads
					     that as circumventing the store, so the honest store
					     build states the absence and stops there. -->
					<section class="ac-section" aria-label="This build">
						<h3>What this build can do</h3>
						{#if buildFlags.error !== null}
							<p class="ac-muted" title={buildFlags.error}>
								Could not read this build's capabilities from the daemon: {buildFlags.error}
							</p>
						{:else if !buildFlags.loaded}
							<p class="ac-muted">Reading this build's capabilities from the daemon...</p>
						{:else if everythingAvailable}
							<p
								class="ac-muted"
								title="Build profile {buildFlags.profile}, sandboxed: {buildFlags.sandboxed}. No declared capability is switched off, by this build or by this machine."
							>
								Every capability openDJ ships is available in this build.
							</p>
						{:else}
							{#if absentInThisBuild.length > 0}
								<p class="ac-muted">
									This build does not include the following. They are not missing from
									openDJ and they are not withheld from your account; this particular
									build cannot offer them.
								</p>
								<ul class="ac-features">
									{#each absentInThisBuild as row (row.flag.flag_id)}
										<li>
											<span class="ac-store-label ac-mono">{row.flag.flag_id}</span>
											<span class="ac-muted" title={row.refusal}>Not in this build</span>
											<span class="ac-muted">{row.flag.note}</span>
										</li>
									{/each}
								</ul>
							{/if}
							{#if disabledLocally.length > 0}
								<p class="ac-muted">
									Turned off by this machine's own configuration, not by the build.
									Whatever switched these off can switch them back on.
								</p>
								<ul class="ac-features">
									{#each disabledLocally as flag (flag.flag_id)}
										<li>
											<span class="ac-store-label ac-mono">{flag.flag_id}</span>
											<span
												class="ac-muted"
												title="Off in this daemon's flag file ({buildFlags.profile} profile). Nothing about the App Store or your plan is involved."
												>Turned off here</span
											>
											<span class="ac-muted">{flag.note}</span>
										</li>
									{/each}
								</ul>
							{/if}
						{/if}
					</section>

					<!-- ------------------------------- stored on this machine -->
					<section class="ac-section" aria-label="Data stored locally">
						<h3>What is stored about you on this machine</h3>
						<ul class="ac-stores">
							{#each account.local_data as store (store.label)}
								<li>
									<span class="ac-store-label">{store.label}</span>
									<span class="ac-mono ac-store-loc">{store.location}</span>
									<span class="ac-muted">{store.contents}</span>
									<span class="ac-muted">Delete with: <code>{store.delete_with}</code></span>
								</li>
							{/each}
						</ul>
						<p class="ac-muted">
							This mirrors the published policy at
							<a href={account.privacy_policy_url} target="_blank" rel="noreferrer noopener"
								>{account.privacy_policy_url}</a
							>.
						</p>

						{#if accountStore.deleted !== null}
							<p class="ac-notice" role="status">{accountStore.deleted}</p>
						{/if}

						{#if user !== null}
							{#if accountOverlay.confirmingDelete}
								<p class="ac-error" role="alert">
									This deletes your account row and every sign-in session on this machine.
									It cannot be undone. Your music library, playlists and analysis are not
									touched.
								</p>
								<div class="ac-actions">
									<button
										type="button"
										class="ac-btn ac-danger"
										disabled={busy}
										onclick={onDelete}
										title="DELETE /api/v1/account?confirm=delete-my-local-account-data. Erases the users row and, by cascade, every auth_sessions row for it."
									>
										Delete it permanently
									</button>
									<button
										type="button"
										class="ac-btn"
										disabled={busy}
										onclick={() => cancelAccountDeleteConfirm()}
										title="Change nothing."
									>
										Cancel
									</button>
								</div>
							{:else}
								<button
									type="button"
									class="ac-btn ac-danger"
									disabled={busy}
									onclick={() => askAccountDeleteConfirm()}
									title="Start the two-step deletion of your account row and its sessions. Nothing is deleted until you confirm."
								>
									Delete my account data
								</button>
							{/if}
						{/if}
					</section>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.ac-backdrop {
		position: fixed;
		inset: 0;
		z-index: 385;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 3vh 1rem;
		/* Translucent on purpose: the app stays visible behind the panel. */
		background: rgb(0 0 0 / 55%);
	}
	.ac-panel {
		width: min(720px, 96vw);
		max-height: 92vh;
		display: flex;
		flex-direction: column;
		background: var(--surface, #121720);
		border: 1px solid var(--border, #1c222c);
		border-radius: 12px;
		box-shadow: 0 18px 50px rgb(0 0 0 / 50%);
		color: var(--fg);
		overflow: hidden;
		outline: none;
	}
	.ac-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid var(--border);
	}
	.ac-head h2 {
		margin: 0;
		font-size: 1.05rem;
	}
	.ac-body {
		overflow: auto;
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
	}
	.ac-section h3 {
		margin: 0 0 0.5rem;
		font-size: 0.85rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted);
	}
	.ac-rows {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.9rem;
		margin: 0 0 0.75rem;
		font-size: 0.82rem;
	}
	.ac-rows dt {
		color: var(--muted);
	}
	.ac-rows dd {
		margin: 0;
		word-break: break-all;
	}
	.ac-mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.78rem;
	}
	.ac-muted {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0.25rem 0;
	}
	.ac-notice {
		margin: 0 0 0.6rem;
		padding: 0.55rem 0.7rem;
		border: 1px solid var(--border);
		border-left: 3px solid var(--accent);
		border-radius: 6px;
		background: var(--bg);
		font-size: 0.82rem;
		line-height: 1.45;
	}
	.ac-error {
		margin: 0 0 0.6rem;
		color: var(--danger, #ff6b6b);
		font-size: 0.82rem;
		line-height: 1.45;
	}
	.ac-stores {
		list-style: none;
		margin: 0 0 0.5rem;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.7rem;
	}
	.ac-stores li {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		padding: 0.55rem 0.7rem;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg);
	}
	.ac-store-label {
		font-size: 0.82rem;
	}
	.ac-features {
		list-style: none;
		margin: 0.4rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.ac-features li {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
	}
	.ac-store-loc {
		color: var(--muted);
		word-break: break-all;
	}
	.ac-stores code {
		font-size: 0.74rem;
		word-break: break-all;
	}
	.ac-actions {
		display: flex;
		gap: 0.4rem;
	}
	.ac-btn {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.3rem 0.75rem;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		cursor: pointer;
	}
	.ac-btn:hover:not(:disabled) {
		border-color: var(--accent);
	}
	.ac-btn:disabled {
		cursor: progress;
		opacity: 0.6;
	}
	.ac-danger {
		color: var(--danger, #ff6b6b);
		border-color: var(--danger, #ff6b6b);
	}
</style>
