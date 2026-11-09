<script lang="ts">
	import {
		formatSettingNumber,
		parseStepperText,
		stepSettingNumber,
		stepperKeySteps,
		type NumberControl
	} from '$lib/settings/setting-number-step';

	let {
		id,
		label,
		title,
		control,
		value,
		onchange
	}: {
		id: string;
		label: string;
		title: string;
		control: NumberControl;
		value: number;
		onchange: (next: number) => void;
	} = $props();

	let draft = $state<string | null>(null);
	let invalid = $state(false);

	function _nudge(steps: number): void {
		const next = stepSettingNumber(control, value, steps);
		if (next !== null) onchange(next);
	}

	function _key(e: KeyboardEvent): void {
		const steps = stepperKeySteps(e.key, e.shiftKey);
		if (steps === null) return;
		e.preventDefault();
		_nudge(steps);
	}

	function _commit(): void {
		const text = draft ?? formatSettingNumber(value, control.step);
		const parsed = parseStepperText(control, text);
		if (parsed === null) {
			invalid = true;
			return;
		}
		invalid = false;
		draft = null;
		if (parsed !== value) onchange(parsed);
	}

	function _blur(): void {
		draft = null;
		invalid = false;
	}
</script>

<div class="sns" role="group" aria-label={label} {title}>
	<button
		type="button"
		class="sns-dec"
		tabindex={-1}
		aria-label={`Decrease ${label}`}
		disabled={value <= control.min}
		onclick={() => _nudge(-1)}>−</button
	>
	<input
		{id}
		class="sns-value"
		type="text"
		inputmode="decimal"
		role="spinbutton"
		aria-valuemin={control.min}
		aria-valuemax={control.max}
		aria-valuenow={value}
		aria-invalid={invalid ? 'true' : undefined}
		title={invalid
			? `Enter a value from ${formatSettingNumber(control.min, control.step)} to ${formatSettingNumber(control.max, control.step)}${control.unit}`
			: title}
		value={draft ?? formatSettingNumber(value, control.step)}
		oninput={(e) => {
			draft = (e.currentTarget as HTMLInputElement).value;
			invalid = false;
		}}
		onchange={_commit}
		onkeydown={_key}
		onblur={_blur}
	/>
	<span class="sns-unit">{control.unit.trim()}</span>
	<button
		type="button"
		class="sns-inc"
		tabindex={-1}
		aria-label={`Increase ${label}`}
		disabled={value >= control.max}
		onclick={() => _nudge(1)}>+</button
	>
</div>

<style>
	.sns {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		font-size: 10px;
	}
	.sns-dec,
	.sns-inc {
		font: inherit;
		font-size: 10px;
		line-height: 1;
		padding: 1px 4px;
		background: var(--rb-surface, #0d1016);
		color: var(--rb-text, #e6e9ef);
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 2px;
		cursor: pointer;
	}
	.sns-dec:disabled,
	.sns-inc:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.sns-value {
		width: 5ch;
		text-align: right;
		font: inherit;
		font-size: 10px;
		background: var(--rb-surface, #0d1016);
		color: var(--rb-text, #e6e9ef);
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 2px;
		padding: 1px 2px;
	}
	.sns-value[aria-invalid='true'] {
		border-color: var(--rb-danger, #e5484d);
	}
	.sns-unit {
		color: var(--rb-text-dim, #9aa3b2);
		font-size: 10px;
	}
</style>
