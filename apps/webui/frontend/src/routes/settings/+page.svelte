<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { getSettings, type SettingsOut } from '$lib/api';
	import { capabilities } from '$lib/api/capabilities.svelte';
	import { setupRefusal } from '$lib/setup/setup-api';
	import { openSettings } from '$lib/settings/hotkeys';
	import { setupWizard } from '$lib/setup/wizard.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let settings = $state<SettingsOut | null>(null);

	/** Why the setup wizard is unreachable from here, or null when it is not.
	 * Reads the capability store, so it re-evaluates once the health probe
	 * lands rather than being decided before the daemon has answered. */
	const setupBlocked = $derived(capabilities.flavor === 'engine' ? null : setupRefusal());

	/** Re-arm the wizard engine-side, then open it. Dismissal is persisted in
	 * the data dir, so clearing it has to be a request, not a local flag. */
	async function reopenSetup(): Promise<void> {
		await setupWizard.reopen();
		if (setupWizard.error !== null) {
			pushToast(`Could not reopen setup: ${setupWizard.error}`, 'error');
			return;
		}
		await goto('/setup');
	}

	async function load(): Promise<void> {
		try {
			settings = await getSettings();
		} catch (exc) {
			pushToast(`Failed to load settings: ${exc}`, 'error');
		}
	}

	function displayValue(value: unknown): string {
		if (value === null || value === undefined) return '';
		if (Array.isArray(value)) return value.join(', ');
		if (typeof value === 'object') return JSON.stringify(value);
		return String(value);
	}

	onMount(load);
</script>

<h2>Settings</h2>
<p style="color: var(--muted);">
	Editable UI prefs open via
	<button type="button" class="linkish" onclick={() => openSettings()}>Cmd+,</button>
	(searchable overlay). This page remains a read-only dump of the daemon's
	effective runtime config. Values marked
	<span class="chip">TBD</span> are not yet introspectable or configurable.
</p>

<section class="settings-group" aria-label="First-run setup">
	<h3>Library setup</h3>
	<p style="color: var(--muted);">
		The first-run wizard imports a rekordbox collection into this engine's
		library. It appears on its own when the library is empty; this is how you
		get back to it afterwards.
		{#if setupBlocked !== null}
			<br />{setupBlocked}
		{/if}
	</p>
	<button
		type="button"
		onclick={reopenSetup}
		disabled={setupBlocked !== null}
		title={setupBlocked ?? 'Re-arm and open the first-run setup wizard'}
	>
		Run first-run setup
	</button>
</section>

{#if settings}
	{#each settings.groups as group}
		<section class="settings-group">
			<h3>{group.group}</h3>
			<table class="library">
				<thead>
					<tr><th>Key</th><th>Value</th><th>Note</th></tr>
				</thead>
				<tbody>
					{#each group.items as item}
						<tr>
							<td><code>{item.key}</code></td>
							<td>
								{#if item.tbd}
									<span class="chip">TBD</span>
								{:else}
									{displayValue(item.value)}
								{/if}
							</td>
							<td style="color: var(--muted); font-size: 0.85rem;">{item.note ?? ''}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</section>
	{/each}
{:else}
	<p style="color: var(--muted); margin-top: 1rem;">Loading settings...</p>
{/if}

<style>
	.settings-group {
		margin-top: 1.5rem;
	}
	.settings-group h3 {
		margin-bottom: 0.4rem;
		color: var(--accent);
		font-size: 0.95rem;
	}
	.linkish {
		display: inline;
		padding: 0;
		border: none;
		background: transparent;
		color: var(--accent);
		font: inherit;
		cursor: pointer;
		text-decoration: underline;
	}
</style>
