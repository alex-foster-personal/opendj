<script lang="ts">
	import { onMount } from 'svelte';
	import { getSettings, type SettingsOut } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';

	let settings = $state<SettingsOut | null>(null);

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
	Read-only view of the daemon's effective runtime config. Values marked
	<span class="chip">TBD</span> are not yet introspectable or configurable.
</p>

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
</style>
