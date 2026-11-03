<script lang="ts">
	/**
	 * THE account surface: who you are signed in as, what plan that puts you
	 * on, and exactly what is stored about you on this machine.
	 *
	 * SHAPE. An OVERLAY over the live app, following SetupOverlay rather than
	 * the settings catalog (constraint 5 in (private path)). Account
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
	import AccountOverlayView from './AccountOverlayView.svelte';
	import { hostedBilling } from '$lib/account/hosted-billing.svelte';
	// Loaded on mount, off the initial bundle; the buttons render only after it answered.
	let billingApi: typeof import('$lib/account/hosted-billing-api') | null = null;
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
		void import('$lib/account/hosted-billing-api').then(
			(api) => {
				billingApi = api;
				return api.loadHostedBilling();
			},
			(exc: unknown) => {
				// A chunk a newer build removed: show it, never a silently missing section.
				hostedBilling.error = exc instanceof Error ? exc.message : String(exc);
			}
		);
	});

	$effect(() => {
		if (!accountOverlay.open || !hostedBilling.eligible) return;
		const onFocus = (): void => {
			void billingApi?.onHostedBillingWindowFocus();
		};
		window.addEventListener('focus', onFocus);
		return () => window.removeEventListener('focus', onFocus);
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

	let billingBusy = $state(false);

	async function runBilling(open: () => Promise<void>): Promise<void> {
		billingBusy = true;
		try {
			await open();
		} catch (exc) {
			pushToast(exc instanceof Error ? exc.message : 'billing failed', 'error');
		} finally {
			billingBusy = false;
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

<AccountOverlayView
	open={accountOverlay.open}
	error={accountStore.error}
	{account}
	deleted={accountStore.deleted}
	sessionAvatarUrl={auth.user?.avatar_url ?? null}
	features={entitlements.features}
	{planRefusal}
	{buildFlags}
	{absentInThisBuild}
	{disabledLocally}
	{everythingAvailable}
	confirmingDelete={accountOverlay.confirmingDelete}
	{busy}
	{onPanelKeydown}
	hostedBilling={hostedBilling}
	{billingBusy}
	onSubscribe={() => runBilling(() => billingApi!.openHostedCheckout())}
	onManage={() => runBilling(() => billingApi!.openHostedPortal())}
	onClose={() => closeAccountOverlay()}
	{onSignOut}
	{onDelete}
	onAskDelete={() => askAccountDeleteConfirm()}
	onCancelDelete={() => cancelAccountDeleteConfirm()}
/>
