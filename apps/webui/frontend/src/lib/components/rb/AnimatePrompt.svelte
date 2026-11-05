<script lang="ts">
	/**
	 * ANIM-01: the right-click "Animate..." box for one dial or fader.
	 *
	 * The operator types the tail of the CLI ramp (`0 --over 8bars --anchor
	 * next_downbeat`); the box validates it live (green or red border, one
	 * error line naming the failing token), shows the grammar with an example
	 * for this control, and the CLI twin of what was typed. Enter runs the
	 * AGENT-03 ramp executor on the beat clock; Esc closes it.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ Every keystroke re-validates.
	 *     [if] an invalid expression shows green, or a valid one red [then] ⛔️
	 *   ✔︎ Enter on a valid expression starts the ramp and closes the box; a refusal at start (no master, clock not playing, no grid) stays in the box.
	 *     [if] Enter on a red expression dispatches anything [then] ⛔️
	 *   ✔︎ Esc closes without dispatching.
	 */
	import { tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import {
		ANIMATE_CONTROLS,
		animateCli,
		animateErrorLine,
		animateHint,
		animateRampPayload,
		animateStartRefusal,
		parseAnimateExpr
	} from '$lib/rb/animate-expr';
	import { animatePrompt, closeAnimatePrompt } from '$lib/rb/dial-animations.svelte';
	import { runRamp } from '$lib/rb/agent-orders';
	import { pushToast } from '$lib/stores.svelte';

	let text = $state('');
	let startError = $state<string | null>(null);
	let starting = $state(false);
	let inputEl: HTMLInputElement | undefined = $state();
	let boxEl: HTMLDivElement | undefined = $state();
	let pos = $state({ left: 0, top: 0 });

	const control = $derived(animatePrompt.control);
	const parse = $derived(parseAnimateExpr(text));
	const empty = $derived(text.trim() === '');

	$effect(() => {
		if (!animatePrompt.open) return;
		text = '';
		startError = null;
		starting = false;
		pos = { left: animatePrompt.x, top: animatePrompt.y };
		void tick().then(() => {
			inputEl?.focus();
			if (boxEl === undefined) return;
			const box = clampToViewport(
				animatePrompt.x,
				animatePrompt.y,
				{ width: boxEl.offsetWidth, height: boxEl.offsetHeight },
				{ width: window.innerWidth, height: window.innerHeight }
			);
			pos = { left: box.x, top: box.y };
		});
	});

	async function submit(): Promise<void> {
		if (control === null || !parse.ok || starting) return;
		const deck = animatePrompt.deck;
		const expr = text;
		const label = `${ANIMATE_CONTROLS[control].label}: ${expr.trim()}`;
		starting = true;
		startError = null;
		let started = false;
		try {
			const result = await runRamp(animateRampPayload(deck, control, expr), {
				source: 'user-ui',
				label,
				onStarted: () => {
					started = true;
					closeAnimatePrompt();
				}
			});
			if (result.status === 'failed') pushToast(`${label} - ${result.error}`, result.error?.startsWith('animation stopped:') ? 'info' : 'error');
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			if (started) pushToast(`${label} - ${message}`, 'error');
			else startError = animateStartRefusal(message);
		} finally {
			starting = false;
		}
	}

	function onWindowPointerDown(e: PointerEvent): void {
		if (!animatePrompt.open || starting) return;
		if (e.target instanceof Node && boxEl?.contains(e.target)) return;
		closeAnimatePrompt();
	}

	function onKeydown(e: KeyboardEvent): void {
		// The performance page binds bare keys to deck actions; typing here must not fire them.
		e.stopPropagation();
		if (e.key === 'Escape') {
			e.preventDefault();
			closeAnimatePrompt();
		} else if (e.key === 'Enter') {
			e.preventDefault();
			void submit();
		}
	}
</script>

<svelte:window onpointerdowncapture={onWindowPointerDown} />

{#if animatePrompt.open && control !== null}
	<div
		class="animate-prompt rb-panel"
		role="dialog"
		aria-label={`animate ${ANIMATE_CONTROLS[control].label}`}
		data-testid="animate-prompt"
		bind:this={boxEl}
		style={`left: ${pos.left}px; top: ${pos.top}px;`}
	>
		<div class="title">
			Animate {ANIMATE_CONTROLS[control].label}{animatePrompt.deck === null ? '' : ` deck ${animatePrompt.deck}`}
		</div>
		<input
			bind:this={inputEl}
			bind:value={text}
			class="expr"
			class:valid={!empty && parse.ok}
			class:invalid={!empty && !parse.ok}
			aria-invalid={!empty && !parse.ok}
			spellcheck="false"
			autocomplete="off"
			placeholder={ANIMATE_CONTROLS[control].example}
			data-testid="animate-prompt-input"
			onkeydown={onKeydown}
		/>
		{#if startError !== null}
			<div class="line error" role="alert" data-testid="animate-prompt-error">{startError}</div>
		{:else if !empty && !parse.ok}
			<div class="line error" role="alert" data-testid="animate-prompt-error">{animateErrorLine(parse)}</div>
		{:else if !empty && parse.ok}
			<div class="line ok" data-testid="animate-prompt-cli">Enter runs = {animateCli(animatePrompt.deck, control, text)}</div>
		{/if}
		<div class="line hint" data-testid="animate-prompt-hint">{animateHint(control)}</div>
	</div>
{/if}

<style>
	.animate-prompt {
		position: fixed;
		z-index: 10001;
		display: flex;
		flex-direction: column;
		gap: 4px;
		width: 460px;
		max-width: calc(100vw - 16px);
		padding: 8px 10px;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
	}
	.title {
		font-weight: 600;
		letter-spacing: 0.04em;
	}
	.expr {
		font-family: var(--rb-mono, ui-monospace, monospace);
		font-size: 12px;
		padding: 4px 6px;
		background: transparent;
		color: var(--rb-text);
		border: 1px solid var(--rb-border);
		outline: none;
	}
	.expr.valid {
		border-color: var(--rb-green, #3c3);
	}
	.expr.invalid {
		border-color: var(--rb-red);
	}
	.line {
		overflow-wrap: anywhere;
	}
	.error {
		color: var(--rb-red);
	}
	.ok {
		color: var(--rb-green, #3c3);
		font-family: var(--rb-mono, ui-monospace, monospace);
	}
	.hint {
		opacity: 0.7;
		font-family: var(--rb-mono, ui-monospace, monospace);
	}
</style>
