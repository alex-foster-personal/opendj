<script lang="ts">
	import {
		cancelMidiLearn,
		midiUi,
		retryMidiLearn
	} from '$lib/components/rb/midi/midi-ui-state.svelte';
	import type { InferredControlKind } from '$lib/rb/midi/learn-infer';

	const KIND_LABEL: Record<InferredControlKind, string> = {
		absolute: 'absolute',
		'absolute-14bit': '14-bit (LSB +32)',
		'button-cc': 'button (CC)',
		'button-note': 'button (note)',
		'relative-centred': 'relative (centre 64)',
		'relative-twos': "relative (two's complement)",
		ambiguous: 'ambiguous'
	};

	function describeBoundSource(
		kind: InferredControlKind,
		source: { ch: number; kind: string; id: number }
	): string {
		if (source.kind === 'note') return `ch ${source.ch} note ${source.id}`;
		if (source.kind === 'cc') return `ch ${source.ch} CC ${source.id}`;
		return `ch ${source.ch} pitchbend`;
	}
</script>

{#if midiUi.learn.phase === 'armed'}
	<div class="learn-status">
		<span>Move the control to learn…</span>
		<button type="button" class="log-btn" onclick={cancelMidiLearn}>Cancel</button>
	</div>
{:else if midiUi.learn.phase === 'capturing'}
	<div class="learn-status">
		<span>Listening… keep moving the control</span>
	</div>
{:else if midiUi.learn.phase === 'ambiguous'}
	<div class="learn-status learn-ambiguous" role="alert" data-testid="midi-learn-ambiguous">
		<p>{midiUi.learn.reason}</p>
		<div class="learn-actions">
			<button
				type="button"
				class="log-btn"
				data-testid="midi-learn-retry"
				onclick={() => void retryMidiLearn()}
			>
				Retry
			</button>
			<button type="button" class="log-btn" onclick={cancelMidiLearn}>Cancel</button>
		</div>
	</div>
{:else if midiUi.learn.phase === 'bound'}
	<div class="learn-status learn-bound">
		<span>
			Learned {describeBoundSource(midiUi.learn.kind, midiUi.learn.binding.source)} as
			{KIND_LABEL[midiUi.learn.kind]}
		</span>
		<button type="button" class="log-btn" onclick={cancelMidiLearn}>Dismiss</button>
	</div>
{/if}

<style>
	.learn-status {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		padding: 4px 0;
	}
	.learn-ambiguous {
		flex-direction: column;
		align-items: stretch;
		color: var(--rb-error, #f66);
	}
	.learn-ambiguous p {
		margin: 0;
	}
	.learn-actions {
		display: flex;
		gap: 4px;
	}
	.log-btn {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 6px;
		cursor: pointer;
	}
	.log-btn:hover {
		color: var(--rb-text);
	}
	.learn-bound {
		color: var(--rb-text);
	}
</style>
