import { runRamp } from './agent-orders';
import { animateRampPayload, animateTargetOfKnobId, ANIMATE_CONTROLS } from './animate-expr';
import { pushToast } from '$lib/stores.svelte';
import { commandedRampExpr, type CommandedMove } from './commanded-keys';

/** Start one ANIM-01 ramp per move; a control no ramp drives is reported to onRefused, not jumped. */
export function startCommandedGlides(
	moves: readonly CommandedMove[],
	onRefused: (id: string) => void
): void {
	for (const move of moves) {
		const target = animateTargetOfKnobId(move.controlId);
		if (target === null) {
			onRefused(move.controlId);
			continue;
		}
		const expr = commandedRampExpr(move);
		const label = `${ANIMATE_CONTROLS[target.control].label}: ${expr}`;
		void runRamp(animateRampPayload(target.deck, target.control, expr), {
			source: 'user-ui',
			label
		})
			.then((result) => {
				if (result.status === 'failed') {
					pushToast(
						`${label} - ${result.error}`,
						result.error?.startsWith('animation stopped:') ? 'info' : 'error'
					);
				}
			})
			.catch((error) => {
				const message = error instanceof Error ? error.message : String(error);
				pushToast(`${label} - ${message}`, 'error');
			});
	}
}
