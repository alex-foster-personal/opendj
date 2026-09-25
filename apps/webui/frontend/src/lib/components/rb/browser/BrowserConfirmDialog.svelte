<script lang="ts">
	let {
		open = $bindable(false),
		title,
		message,
		primaryLabel = 'OK',
		secondaryLabel = 'Cancel',
		showRemember = true,
		rememberLabel = "Don't show this again",
		showDefault = false,
		defaultLabel = 'Default to this',
		onPrimary,
		onSecondary
	}: {
		open?: boolean;
		title: string;
		message: string;
		primaryLabel?: string;
		secondaryLabel?: string;
		showRemember?: boolean;
		rememberLabel?: string;
		showDefault?: boolean;
		defaultLabel?: string;
		onPrimary: (opts: { remember: boolean; setDefault: boolean }) => void;
		onSecondary?: () => void;
	} = $props();

	let remember = $state(false);
	let setDefault = $state(false);

	function _primary(): void {
		onPrimary({ remember, setDefault });
		open = false;
		remember = false;
		setDefault = false;
	}

	function _secondary(): void {
		onSecondary?.();
		open = false;
		remember = false;
		setDefault = false;
	}
</script>

{#if open}
	<div class="browser-confirm" role="dialog" aria-modal="true" aria-label={title}>
		<p class="browser-confirm-title">{title}</p>
		<p class="browser-confirm-msg">{message}</p>
		{#if showDefault}
			<label class="browser-confirm-opt">
				<input type="checkbox" bind:checked={setDefault} />
				{defaultLabel}
			</label>
		{/if}
		{#if showRemember}
			<label class="browser-confirm-opt">
				<input type="checkbox" bind:checked={remember} />
				{rememberLabel}
			</label>
		{/if}
		<div class="browser-confirm-actions">
			<button type="button" class="browser-confirm-primary" onclick={_primary}>{primaryLabel}</button>
			<button type="button" class="browser-confirm-secondary" onclick={_secondary}>{secondaryLabel}</button>
		</div>
	</div>
{/if}

<style>
	.browser-confirm {
		position: fixed;
		z-index: 12000;
		left: 50%;
		top: 40%;
		transform: translate(-50%, -50%);
		max-width: 28rem;
		padding: 1rem 1.1rem;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 8px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
	}
	.browser-confirm-title {
		font-weight: 600;
		margin: 0 0 0.5rem;
	}
	.browser-confirm-msg {
		margin: 0 0 0.75rem;
		white-space: pre-wrap;
	}
	.browser-confirm-opt {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		font-size: 0.85rem;
		margin-bottom: 0.35rem;
	}
	.browser-confirm-actions {
		display: flex;
		gap: 0.5rem;
		justify-content: flex-end;
		margin-top: 0.75rem;
	}
	.browser-confirm-primary,
	.browser-confirm-secondary {
		padding: 0.35rem 0.75rem;
	}
</style>
