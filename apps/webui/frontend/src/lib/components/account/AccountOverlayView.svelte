<script lang="ts">
	/**
	 * Presentational body of the account panel (AccountOverlay.svelte owns the
	 * stores, the loads and the sign-out and delete calls). Props only, so
	 * Storybook can render every state of it without a daemon.
	 */
	import type { components } from '$lib/api-types';
	import type { HostedBillingState } from '$lib/account/hosted-billing.svelte';

	type AccountOut = components['schemas']['AccountOut'];
	type FeatureEntitlementOut = components['schemas']['FeatureEntitlementOut'];
	type FlagOut = components['schemas']['FlagOut'];

	interface BuildFlagsView {
		error: string | null;
		loaded: boolean;
		profile: string;
		sandboxed: boolean;
	}

	interface Props {
		open: boolean;
		/** accountStore.error: a load or delete failure, verbatim. */
		error: string | null;
		account: AccountOut | null;
		/** accountStore.deleted: the daemon's confirmation after a delete. */
		deleted: string | null;
		/** The signed-in session's avatar, used when the account row has none. */
		sessionAvatarUrl: string | null;
		features: FeatureEntitlementOut[];
		planRefusal: (featureId: string) => string | null;
		buildFlags: BuildFlagsView;
		absentInThisBuild: { flag: FlagOut; refusal: string | null }[];
		disabledLocally: FlagOut[];
		everythingAvailable: boolean;
		confirmingDelete: boolean;
		busy: boolean;
		onPanelKeydown: (event: KeyboardEvent) => void;
		onClose: () => void;
		onSignOut: () => void;
		onDelete: () => void;
		onAskDelete: () => void;
		onCancelDelete: () => void;
		/** The hosted hub's billing read (hosted-billing.svelte.ts); the section shows when eligible or errored. */
		hostedBilling: HostedBillingState;
		/** A Subscribe or Manage call is in flight. */
		billingBusy: boolean;
		onSubscribe: () => void;
		onManage: () => void;
	}

	let {
		open,
		error,
		account,
		deleted,
		sessionAvatarUrl,
		features,
		planRefusal,
		buildFlags,
		absentInThisBuild,
		disabledLocally,
		everythingAvailable,
		confirmingDelete,
		busy,
		onPanelKeydown,
		onClose,
		onSignOut,
		onDelete,
		onAskDelete,
		onCancelDelete,
		hostedBilling,
		billingBusy,
		onSubscribe,
		onManage
	}: Props = $props();

	/** Set when the avatar URL 404s or Google returns no picture. */
	let avatarBroken = $state(false);

	const plan = $derived(account?.plan ?? null);
	const user = $derived(account?.user ?? null);

	const avatarUrl = $derived(user?.avatar_url ?? sessionAvatarUrl ?? null);
	const showAvatar = $derived(Boolean(avatarUrl) && !avatarBroken);
	const initial = $derived(
		(user?.name?.trim()?.[0] ?? user?.email?.[0] ?? '?').toUpperCase()
	);
	const firstSeenLabel = $derived.by(() => {
		if (user?.created_at === undefined || user.created_at === '') return null;
		const parsed = new Date(user.created_at);
		if (Number.isNaN(parsed.getTime())) return null;
		return parsed.toLocaleDateString(undefined, {
			day: 'numeric',
			month: 'short',
			year: 'numeric'
		});
	});

	$effect(() => {
		if (avatarUrl) avatarBroken = false;
	});
</script>

{#if open}
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
					onclick={() => onClose()}
					title="Close the account panel. Nothing here is saved on close; every action took effect when you pressed it."
				>
					Close
				</button>
			</header>

			<div class="ac-body">
				{#if error !== null}
					<p class="ac-error" role="alert">{error}</p>
				{:else if account === null}
					<p class="ac-muted">Reading your account from the daemon...</p>
				{:else}
					<!-- ------------------------------------------- identity -->
					<section class="ac-section ac-primary" aria-label="Identity">
						{#if user === null}
							<p class="ac-muted">
								Not signed in. openDJ works exactly the same either way; signing in only
								tells it who you are.
							</p>
						{:else}
							<div class="ac-identity">
								<div class="ac-avatar" aria-hidden="true">
									{#if showAvatar && avatarUrl}
										<img
											src={avatarUrl}
											alt=""
											referrerpolicy="no-referrer"
											onerror={() => (avatarBroken = true)}
										/>
									{:else}
										<span class="ac-avatar-initial">{initial}</span>
									{/if}
								</div>
								<div class="ac-identity-text">
									{#if user.name}
										<p class="ac-name">{user.name}</p>
									{/if}
									<p class="ac-email">{user.email}</p>
									{#if firstSeenLabel !== null}
										<p
											class="ac-meta"
											title="When this account row was first written on this machine."
										>
											First seen {firstSeenLabel}
										</p>
									{/if}
								</div>
							</div>
							<button
								type="button"
								class="ac-btn ac-identity-action"
								disabled={busy}
								onclick={onSignOut}
								title="POST /api/v1/auth/logout. Ends this browser's session and deletes the Google tokens held for it. Your account row stays."
							>
								Sign out
							</button>
						{/if}
					</section>

					<!-- ----------------------------------------------- plan -->
					<section class="ac-section ac-primary" aria-label="Plan">
						<h3>Plan</h3>
						<p class="ac-notice" role="note">{account.authorisation_note}</p>
						{#if plan}
							<dl class="ac-rows ac-rows-primary">
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
						{#if hostedBilling.eligible || hostedBilling.error !== null}
							<section class="ac-hosted-billing" aria-label="CloudSync hosted plan">
								<h4>CloudSync hosted hub</h4>
								{#if hostedBilling.loading}
									<p class="ac-muted">Reading hosted plan...</p>
								{:else if hostedBilling.error !== null}
									<p class="ac-muted" title={hostedBilling.error}>
										Could not read hosted billing: {hostedBilling.error}
									</p>
								{:else}
									<p class="ac-muted">Provider: {hostedBilling.provider ?? 'unknown'}</p>
									<ul class="ac-features">
										{#each hostedBilling.standings as row (row.feature_id)}
											<li>
												<span class="ac-store-label">{row.feature_id}</span>
												<span class="ac-muted"
													>{row.state === null ? 'No subscription' : row.state.replaceAll('_', ' ')}</span
												>
											</li>
										{/each}
									</ul>
									<div class="ac-actions">
										<button
											type="button"
											class="ac-btn"
											disabled={busy || billingBusy}
											onclick={() => onSubscribe()}
											title="Polar checkout via engine."
										>
											Subscribe
										</button>
										<button
											type="button"
											class="ac-btn"
											disabled={busy || billingBusy}
											onclick={() => onManage()}
											title="Polar portal via engine."
										>
											Manage
										</button>
									</div>
								{/if}
							</section>
						{/if}

						{#if features.length === 0}
							<p class="ac-muted">
								No feature in this build is gated by a plan, so there is nothing here to
								include or leave out.
							</p>
						{:else}
							<ul class="ac-features">
								{#each features as feature (feature.feature_id)}
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

					<div class="ac-secondary">
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
									<!-- COPY THAT COVERS BOTH WAYS A FLAG GETS HERE, deliberately
										 (PR #1720 round-2 P2). This group is every flag that is off
										 without the store profile refusing it, and there are two of
										 those: a local override file, and a flag whose DECLARED
										 DEFAULT is false. The earlier wording named the first ("this
										 machine's own configuration ... whatever switched these off
										 can switch them back on") and would have been simply false of
										 the second, which no file on this machine touched.

										 Branching on `flag.overridden` would separate them, and is
										 NOT what this does: usb.export is a default-on flag that the
										 App Store profile turns off, and this
										 heading already covers declared defaults as well as local
										 overrides. One sentence true of both states beats two
										 sentences where one is unreachable. -->
									<p class="ac-muted">
										Off in the flag configuration this daemon resolved: a declared
										default, or a local override. Neither the App Store build nor
										your plan is involved.
									</p>
									<ul class="ac-features">
										<!-- NO `flag.note` here, unlike the store group above, and that
											 is the point rather than an omission. The note is the flag's
											 BUILD-time description, and usb.export's reads "ON everywhere
											 except the Mac App Store build" - true of the flag, false of
											 this row, which exists precisely because something on this
											 machine turned it off instead. Rendering it beside "Turned off
											 here" both contradicts the row and re-attributes the decision
											 to Apple, which is the misattribution this whole group was
											 added to stop (PR #1720 round-1 P2). The component composes no
											 replacement sentence for the same reason it composes no
											 refusal: a description invented here is a second truth free to
											 drift from the daemon's. -->
										{#each disabledLocally as flag (flag.flag_id)}
											<li>
												<span class="ac-store-label ac-mono">{flag.flag_id}</span>
												<span
													class="ac-muted"
													title="Off in the flag configuration this daemon resolved ({buildFlags.profile} profile), from its declared default or a local override. Nothing about the App Store or your plan is involved."
													>Turned off here</span
												>
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

							{#if deleted !== null}
								<p class="ac-notice" role="status">{deleted}</p>
							{/if}
						</section>
					</div>

					{#if user !== null}
						<section class="ac-section ac-danger-zone" aria-label="Danger zone">
							<h3>Danger zone</h3>
							{#if confirmingDelete}
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
										onclick={() => onCancelDelete()}
										title="Change nothing."
									>
										Cancel
									</button>
								</div>
							{:else}
								<p class="ac-muted">
									Permanently remove your account row and sign-in sessions from this machine.
									Your music library is not affected.
								</p>
								<button
									type="button"
									class="ac-btn ac-danger"
									disabled={busy}
									onclick={() => onAskDelete()}
									title="Start the two-step deletion of your account row and its sessions. Nothing is deleted until you confirm."
								>
									Delete my account data
								</button>
							{/if}
						</section>
					{/if}
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
	.ac-primary {
		padding-bottom: 0.25rem;
	}
	.ac-section h3 {
		margin: 0 0 0.5rem;
		font-size: 0.85rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted);
	}
	.ac-secondary {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		padding-top: 0.75rem;
		border-top: 1px solid var(--border);
	}
	.ac-secondary .ac-section h3 {
		font-size: 0.72rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		opacity: 0.85;
	}
	.ac-secondary .ac-muted,
	.ac-secondary .ac-store-label,
	.ac-secondary .ac-features,
	.ac-secondary .ac-stores {
		font-size: 0.74rem;
	}
	.ac-secondary .ac-mono {
		font-size: 0.7rem;
	}
	.ac-identity {
		display: flex;
		align-items: center;
		gap: 1rem;
		margin-bottom: 0.85rem;
	}
	.ac-avatar {
		flex-shrink: 0;
		width: 60px;
		height: 60px;
		border-radius: 50%;
		overflow: hidden;
		border: 1px solid var(--border);
		background: var(--bg);
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.ac-avatar img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.ac-avatar-initial {
		font-size: 1.35rem;
		font-weight: 600;
		color: var(--accent);
		line-height: 1;
	}
	.ac-identity-text {
		min-width: 0;
	}
	.ac-name {
		margin: 0;
		font-size: 1.15rem;
		font-weight: 600;
		line-height: 1.25;
	}
	.ac-email {
		margin: 0.15rem 0 0;
		font-size: 0.9rem;
		color: var(--muted);
		word-break: break-all;
	}
	.ac-meta {
		margin: 0.35rem 0 0;
		font-size: 0.78rem;
		color: var(--muted);
	}
	.ac-identity-action {
		margin-top: 0.15rem;
	}
	.ac-rows {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.9rem;
		margin: 0 0 0.75rem;
		font-size: 0.82rem;
	}
	.ac-rows-primary {
		font-size: 0.88rem;
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
	.ac-hosted-billing {
		margin: 0.75rem 0 0.5rem;
		padding: 0.65rem 0.75rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
	}
	.ac-hosted-billing h4 {
		margin: 0 0 0.45rem;
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted);
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
	.ac-danger-zone {
		margin-top: 0.25rem;
		padding: 0.85rem 0.95rem;
		border: 1px solid var(--danger, #ff6b6b);
		border-radius: 8px;
		background: rgb(255 107 107 / 8%);
	}
	.ac-danger-zone h3 {
		color: var(--danger, #ff6b6b);
		margin-bottom: 0.6rem;
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
