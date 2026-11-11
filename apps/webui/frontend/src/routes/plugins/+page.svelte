<script lang="ts">
	/**
	 * PLUG-6522h: the in-app plugin store. A view over GET /api/v1/plugins; every
	 * rule (signed catalog, labs opt-in, third-party terms notice, permission consent)
	 * is enforced by the server and its refusal sentence is shown verbatim.
	 *
	 * Nothing here can interrupt playback: changes apply the next time Open DJ
	 * starts, and the page never restarts the engine (PLUG-6522e).
	 */
	import { onMount } from 'svelte';
	import { ApiError } from '$lib/api/errors';
	import {
		badges,
		getPlugins,
		canSideload,
		installConfirmCopy,
		installPlugin,
		labsShown,
		listingStatusBadge,
		previewSideload,
		primaryAction,
		screenshotsFor,
		setEnabled,
		setLabs,
		sideloadPlugin,
		statusLine,
		uninstallPlugin,
		type PluginRow,
		type PluginsOut,
		type SideloadPreview
	} from '$lib/api/plugins';
	import { engineUrl } from '$lib/plugins/slots.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { runButtonAction, runButtonActionGate } from '$lib/ui/run-button-action';

	let store = $state<PluginsOut | null>(null);
	let loadError = $state<string | null>(null);
	let busy = $state<string | null>(null);
	let confirming = $state<{ row: PluginRow; kind: 'install' | 'update' | 'uninstall' } | null>(null);
	let noticeRead = $state(false);
	/** The listing open in the Details view (PLUG-6522k), or null. */
	let details = $state<PluginRow | null>(null);
	let sideloadPath = $state('');
	let sideloadNoticeRead = $state(false);
	/** The build the developer reviewed; consent is to exactly this (PLUG-6522j). */
	let sideloadPreview = $state<SideloadPreview | null>(null);
	const pluginActionGate = runButtonActionGate(
		() => busy,
		(value) => {
			busy = value;
		}
	);

	async function checkSideload(): Promise<void> {
		busy = 'sideload-preview';
		sideloadPreview = null;
		sideloadNoticeRead = false;
		try {
			sideloadPreview = await previewSideload(sideloadPath);
		} catch (exc) {
			pushToast(message(exc), 'error');
		} finally {
			busy = null;
		}
	}

	const stable = $derived(store?.plugins.filter((p) => p.channel === 'stable') ?? []);
	const labs = $derived(store?.plugins.filter((p) => p.channel === 'labs') ?? []);
	const installedLabs = $derived(labsShown(labs, false));
	const faults = $derived(store?.channels.filter((c) => c.visible && c.fault !== null) ?? []);

	function message(exc: unknown): string {
		if (exc instanceof ApiError) return exc.message;
		return exc instanceof Error ? exc.message : String(exc);
	}

	async function load(refresh: boolean): Promise<void> {
		if (!refresh) {
			try {
				store = await getPlugins(false);
				loadError = null;
			} catch (exc) {
				loadError = message(exc);
			}
			return;
		}
		await runButtonAction(pluginActionGate, 'refresh', async () => {
			store = await getPlugins(true);
			loadError = null;
		});
	}

	async function act(id: string, work: () => Promise<{ message: string }>): Promise<void> {
		await runButtonAction(pluginActionGate, id, async () => {
			const out = await work();
			pushToast(out.message, 'info');
			await load(false);
		});
	}

	function onPrimary(row: PluginRow): void {
		details = null;
		const action = primaryAction(row);
		if (action === 'install' || action === 'update') {
			noticeRead = false;
			confirming = { row, kind: action };
		} else if (action === 'enable' || action === 'disable') {
			void act(row.plugin_id, () => setEnabled(row.plugin_id, action === 'enable'));
		}
	}

	async function confirm(): Promise<void> {
		if (confirming === null) return;
		const { row, kind } = confirming;
		confirming = null;
		if (kind !== 'uninstall') await act(row.plugin_id, () => installPlugin(row, noticeRead));
		else await act(row.plugin_id, () => uninstallPlugin(row.plugin_id));
	}

	async function toggleLabs(optIn: boolean): Promise<void> {
		busy = 'labs';
		try {
			store = await setLabs(optIn);
		} catch (exc) {
			pushToast(message(exc), 'error');
		} finally {
			busy = null;
		}
	}

	function onDialogKey(event: KeyboardEvent): void {
		if (event.key === 'Escape' && confirming === null) {
			details = null;
			return;
		}
		if (event.key === 'Escape') confirming = null;
		if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && confirmReady) void confirm();
	}

	const confirmReady = $derived(
		confirming !== null && (confirming.kind === 'uninstall' || confirming.row.tier !== 2 || noticeRead)
	);

	const ACTION_LABEL: Record<string, string> = {
		install: 'Install',
		update: 'Update',
		enable: 'Turn on',
		disable: 'Turn off'
	};

	onMount(() => load(true));
</script>

<svelte:window onkeydown={confirming || details ? onDialogKey : undefined} />

<header class="store-head">
	<div>
		<h2>Plugins</h2>
		<p class="lede">
			Optional modules for Open DJ. Anything you install or turn on here takes effect the next
			time Open DJ starts, so nothing changes under a playing deck.
		</p>
		<p class="lede">
			Not every module adds buttons. Some work in the background, and some you use by asking in
			the AI sidebar, where they add skills and MCP tools.
		</p>
	</div>
	<button
		type="button"
		onclick={() => load(true)}
		disabled={busy !== null}
		title="Fetch the latest signed catalog from the plugin store"
	>
		{busy === 'refresh' ? 'Checking...' : 'Check for plugins'}
	</button>
</header>

{#if loadError}
	<p class="fault" role="alert">Could not reach the engine's plugin store: {loadError}</p>
{/if}

{#if store}
	{#if store.restart_required}
		<p class="banner" role="status">
			You have changes waiting. They apply next time Open DJ starts.
		</p>
	{/if}

	{#each faults as channel (channel.channel)}
		<p class="fault" title={channel.url}>
			{channel.channel === 'labs' ? 'Labs' : 'Store'} catalog unavailable
			<code>{channel.fault?.code}</code>: {channel.fault?.message}
			{#if channel.stale && channel.fetched_at}
				<br />Showing the last verified copy from {channel.fetched_at}.
			{/if}
		</p>
	{/each}

	<section aria-label="Plugins" class="grid">
		{#each stable as row (row.plugin_id)}
			{@render card(row)}
		{:else}
			<p class="empty">
				{faults.some((f) => f.channel === 'stable')
					? 'No verified catalog yet, so there is nothing to list.'
					: 'The store lists no plugins yet.'}
			</p>
		{/each}
	</section>

	<section class="labs" aria-label="Labs">
		<div class="labs-head">
			<h3>Labs</h3>
			<label title="Show experimental modules. Nothing is installed or turned on until you choose it.">
				<input
					type="checkbox"
					checked={store.labs_opt_in}
					disabled={busy !== null}
					onchange={(e) => toggleLabs(e.currentTarget.checked)}
				/>
				Show labs experiments
			</label>
		</div>
		<p class="lede">
			Experimental modules live here and stay off until you install one. Some work with outside
			services or content whose terms may limit how you can use them; each of those says so
			before you install it.
		</p>
		{#if store.labs_opt_in}
			<div class="grid">
				{#each labs as row (row.plugin_id)}
					{@render card(row)}
				{:else}
					<p class="empty">No labs modules listed.</p>
				{/each}
			</div>
		{:else if installedLabs.length > 0}
			<div class="grid">
				{#each installedLabs as row (row.plugin_id)}
					{@render card(row)}
				{/each}
			</div>
		{/if}
	</section>

	{#if store.dev_mode}
		<section class="dev" aria-label="Developer sideload">
			<h3>Developer sideload</h3>
			<p class="lede">
				Install an unsigned build from a folder on this computer. It loads only while the engine
				runs with <code>MDT_PLUGIN_DEV=1</code>.
			</p>
			<form
				onsubmit={(e) => {
					e.preventDefault();
					void checkSideload();
				}}
			>
				<input
					type="text"
					placeholder="/path/to/plugin-build"
					bind:value={sideloadPath}
					oninput={() => (sideloadPreview = null)}
					aria-label="Plugin folder"
				/>
				<button
					type="submit"
					disabled={busy !== null || sideloadPath.trim() === ''}
					title="Read the build's manifest and show what it asks for; nothing is installed or run"
				>
					Check build
				</button>
			</form>
			{#if sideloadPreview}
				{@const preview = sideloadPreview}
				<div class="sideload-preview" aria-label="Build to sideload">
					<p>
						<strong>{preview.name}</strong>
						{preview.version} <span class="sideload-id">({preview.plugin_id}, {preview.channel})</span>
					</p>
					<p>
						Permissions it gets:
						{preview.permissions.length ? preview.permissions.join(', ') : 'none'}
					</p>
					{#if preview.notice}
						<p class="notice"><strong>Third-party terms.</strong> {preview.notice}</p>
						<label class="sideload-notice">
							<input type="checkbox" bind:checked={sideloadNoticeRead} />
							I have read these third-party terms, and I am responsible for using this build within
							them.
						</label>
					{/if}
					<button
						type="button"
						disabled={busy !== null || !canSideload(preview, sideloadNoticeRead)}
						title={canSideload(preview, sideloadNoticeRead)
							? 'Install this build with the permissions above'
							: 'Accept its third-party terms first'}
						onclick={() =>
							void act('sideload', () => sideloadPlugin(sideloadPath, preview, sideloadNoticeRead))}
					>
						Sideload
					</button>
				</div>
			{/if}
		</section>
	{/if}
{:else if !loadError}
	<p class="lede">Loading plugins...</p>
{/if}

{#snippet card(row: PluginRow)}
	{@const action = primaryAction(row)}
	{@const status = statusLine(row)}
	{@const shots = row.listing ? screenshotsFor(row.listing.screenshots, uiPrefs.theme) : []}
	<article class="card" class:terms={row.tier === 2} data-plugin={row.plugin_id}>
		{#if shots.length > 0}
			<button
				type="button"
				class="thumb"
				title="See what {row.name} does"
				onclick={() => (details = row)}
			>
				<img
					src={engineUrl(shots[0].url)}
					alt={shots[0].alt}
					width={shots[0].width}
					height={shots[0].height}
					loading="lazy"
				/>
			</button>
		{/if}
		<div class="card-head">
			<h4>{row.name}</h4>
			<span
				class="version"
				title="Installed version, and the newest version in the store when it differs"
			>
				{row.installed_version ?? row.available_version}
				{#if row.update_available}<span class="arrow">to {row.available_version}</span>{/if}
			</span>
		</div>
		<div class="badges">
			{#each badges(row) as badge}<span class="badge" data-badge={badge}>{badge}</span>{/each}
			{#if row.listing && listingStatusBadge(row.listing.status)}
				<span class="badge" data-badge="status">{listingStatusBadge(row.listing.status)}</span>
			{/if}
		</div>
		<p class="summary">{row.listing?.tagline ?? row.summary}</p>
		{#if row.notice}
			<p class="notice"><strong>Third-party terms.</strong> {row.notice}</p>
		{/if}
		{#if row.permissions.length > 0}
			<p class="perms" title="What this plugin is allowed to do once it runs">
				Can: {row.permissions.join(', ')}
			</p>
		{/if}
		{#if status}
			<p class="status" class:bad={row.load_status !== null && !['loaded', 'dev-only', 'no-consumer'].includes(row.load_status)}>
				{status}
			</p>
		{/if}
		<div class="actions">
			{#if action}
				<button
					type="button"
					class:primary={action === 'install' || action === 'update'}
					disabled={busy !== null}
					onclick={() => onPrimary(row)}
				>
					{busy === row.plugin_id ? 'Working...' : ACTION_LABEL[action]}
				</button>
			{/if}
			{#if row.listing}
				<button type="button" class="quiet" onclick={() => (details = row)}>Details</button>
			{/if}
			{#if row.installed_version !== null}
				<button
					type="button"
					class="quiet"
					disabled={busy !== null}
					onclick={() => (confirming = { row, kind: 'uninstall' })}
				>
					Uninstall
				</button>
			{/if}
		</div>
	</article>
{/snippet}

{#if details && details.listing && !confirming}
	{@const row = details}
	{@const listing = details.listing}
	{@const action = primaryAction(row)}
	<div
		class="scrim"
		role="presentation"
		onclick={(e) => {
			if (e.target === e.currentTarget) details = null;
		}}
	>
		<div
			class="dialog details"
			role="dialog"
			aria-modal="true"
			aria-labelledby="plugin-details-title"
			data-details={row.plugin_id}
		>
			<h3 id="plugin-details-title">{row.name}</h3>
			<p class="tagline">{listing.tagline}</p>
			<p>{listing.problem}</p>
			<h4>You can</h4>
			<ul>
				{#each listing.you_can as item}<li>{item}</li>{/each}
			</ul>
			<h4>How it works</h4>
			<p>{listing.how_it_works}</p>
			{#if listing.limits.length > 0}
				<h4>Limits</h4>
				<ul>
					{#each listing.limits as item}<li>{item}</li>{/each}
				</ul>
			{/if}
			{#if row.notice}
				<p class="notice"><strong>Third-party terms.</strong> {row.notice}</p>
			{/if}
			<div class="gallery">
				{#each screenshotsFor(listing.screenshots, uiPrefs.theme) as shot (shot.url)}
					<img
						src={engineUrl(shot.url)}
						alt={shot.alt}
						width={shot.width}
						height={shot.height}
						loading="lazy"
					/>
				{/each}
			</div>
			<div class="dialog-actions">
				<button type="button" class="quiet" onclick={() => (details = null)}>Close</button>
				{#if action}
					<button
						type="button"
						class:primary={action === 'install' || action === 'update'}
						disabled={busy !== null}
						onclick={() => onPrimary(row)}
					>
						{ACTION_LABEL[action]}
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

{#if confirming}
	{@const row = confirming.row}
	<div
		class="scrim"
		role="presentation"
		onclick={(e) => {
			if (e.target === e.currentTarget) confirming = null;
		}}
	>
		<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="plugin-confirm-title">
			{#if confirming.kind !== 'uninstall'}
				{@const copy = installConfirmCopy(row, confirming.kind)}
				<h3 id="plugin-confirm-title">{copy.title}</h3>
				<p>{copy.line}</p>
				{#if row.permissions.length > 0}
					<p>It will be allowed to:</p>
					<ul>
						{#each row.permissions as perm}<li><code>{perm}</code></li>{/each}
					</ul>
				{/if}
				{#if row.tier === 2 && row.notice}
					<p class="notice"><strong>Third-party terms.</strong> {row.notice}</p>
					<label>
						<input type="checkbox" bind:checked={noticeRead} />
						I have read this, and I am responsible for using it within those terms.
					</label>
				{/if}
			{:else}
				<h3 id="plugin-confirm-title">Uninstall {row.name}?</h3>
				<p>Its files are removed. If it is running, it stops next time Open DJ starts.</p>
			{/if}
			<div class="dialog-actions">
				<button type="button" class="quiet" onclick={() => (confirming = null)}>Cancel</button>
				<button
					type="button"
					class="primary"
					disabled={!confirmReady}
					onclick={confirm}
					title="Cmd+Enter"
				>
					{confirming.kind === 'uninstall' ? 'Uninstall' : installConfirmCopy(row, confirming.kind).button}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.store-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem;
	}
	.lede {
		color: var(--muted);
		max-width: 46rem;
	}
	.banner {
		border: 1px solid var(--accent-dim);
		background: var(--surface);
		padding: 0.5rem 0.75rem;
		border-radius: 6px;
	}
	.fault {
		color: var(--warning);
		border-left: 3px solid var(--warning);
		padding-left: 0.6rem;
	}
	.fault code {
		font-size: 0.85em;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr));
		gap: 0.9rem;
		margin-top: 1rem;
	}
	.empty {
		color: var(--muted);
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.85rem 0.95rem;
	}
	.card.terms {
		border-color: var(--warning);
	}
	.card-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
	}
	.card h4 {
		margin: 0;
		font-size: 1rem;
	}
	.version {
		color: var(--muted);
		font-size: 0.8rem;
		font-variant-numeric: tabular-nums;
	}
	.arrow {
		color: var(--accent);
	}
	.badges {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
	}
	.badge {
		font-size: 0.7rem;
		padding: 0.1rem 0.45rem;
		border-radius: 999px;
		background: var(--surface-raised);
		color: var(--muted);
	}
	.badge[data-badge='Labs'],
	.badge[data-badge='Third-party terms'],
	.badge[data-badge='Unsigned'] {
		color: var(--warning);
	}
	.summary {
		margin: 0;
	}
	.notice {
		margin: 0;
		font-size: 0.85rem;
		color: var(--warning);
	}
	.perms,
	.status {
		margin: 0;
		font-size: 0.8rem;
		color: var(--muted);
	}
	.status.bad {
		color: var(--danger);
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		margin-top: auto;
		padding-top: 0.3rem;
	}
	button.primary {
		background: var(--accent);
		color: var(--on-accent);
		border-color: var(--accent);
	}
	button.primary:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	button.quiet {
		background: transparent;
	}
	.dialog .notice {
		margin: 0.6rem 0 0.4rem;
	}
	.labs,
	.dev {
		margin-top: 2rem;
	}
	.labs-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		max-width: 46rem;
	}
	.labs h3,
	.dev h3 {
		color: var(--accent);
		font-size: 0.95rem;
		margin: 0;
	}
	.dev form {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}
	.dev input[type='text'] {
		flex: 1;
		max-width: 30rem;
	}
	.sideload-notice {
		display: block;
		font-size: 0.85rem;
		color: var(--muted);
		margin-bottom: 0.5rem;
	}
	.sideload-preview {
		margin-top: 0.75rem;
		max-width: 46rem;
		padding: 0.75rem 1rem;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.sideload-preview p {
		margin: 0 0 0.5rem;
	}
	.sideload-id {
		color: var(--muted);
	}
	.scrim {
		position: fixed;
		inset: 0;
		background: rgb(0 0 0 / 55%);
		display: grid;
		place-items: center;
		z-index: 50;
	}
	.dialog {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 1.1rem 1.25rem;
		width: min(28rem, 92vw);
	}
	.dialog h3 {
		margin-top: 0;
	}
	.thumb {
		padding: 0;
		border: 0;
		background: none;
		cursor: pointer;
	}
	.thumb img {
		display: block;
		width: 100%;
		height: auto;
		aspect-ratio: 16 / 9;
		object-fit: cover;
		border-radius: 6px;
		border: 1px solid var(--border);
	}
	.dialog.details {
		width: min(46rem, 94vw);
		max-height: 88vh;
		overflow-y: auto;
	}
	.details .tagline {
		color: var(--muted);
		margin-top: -0.4rem;
	}
	.details h4 {
		margin: 0.9rem 0 0.3rem;
	}
	.details ul {
		margin: 0;
		padding-left: 1.2rem;
	}
	.gallery {
		display: grid;
		gap: 0.6rem;
		margin-top: 1rem;
	}
	.gallery img {
		width: 100%;
		height: auto;
		border-radius: 6px;
		border: 1px solid var(--border);
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 1rem;
	}
</style>
