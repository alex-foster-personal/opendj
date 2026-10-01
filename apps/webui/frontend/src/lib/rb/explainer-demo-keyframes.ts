/**
 * Declared keyframe steps for ControlExplainer SVG demo animations.
 * Unit tests parse the matching @keyframes blocks in ControlExplainer.svelte
 * and assert this catalogue stays in sync and only uses transform/opacity.
 */
export type ExplainerKeyframeStep = {
	offset: number;
	properties: readonly string[];
};

export const EXPLAINER_DEMO_ANIMATIONS: Record<string, readonly ExplainerKeyframeStep[]> = {
	'cue-return': [
		{ offset: 0, properties: ['transform', 'opacity'] },
		{ offset: 0.45, properties: ['transform', 'opacity'] },
		{ offset: 0.7, properties: ['transform', 'opacity'] },
		{ offset: 0.85, properties: ['transform', 'opacity'] },
		{ offset: 1, properties: ['transform', 'opacity'] }
	],
	'slip-loop': [
		{ offset: 0, properties: ['transform'] },
		{ offset: 1, properties: ['transform'] }
	],
	'slip-linear': [
		{ offset: 0, properties: ['transform'] },
		{ offset: 1, properties: ['transform'] }
	],
	'split-browser-pulse': [
		{ offset: 0, properties: ['opacity'] },
		{ offset: 0.5, properties: ['opacity'] },
		{ offset: 1, properties: ['opacity'] }
	],
	'link-beat-pulse': [
		{ offset: 0, properties: ['opacity', 'transform'] },
		{ offset: 0.5, properties: ['opacity', 'transform'] },
		{ offset: 1, properties: ['opacity', 'transform'] }
	],
	'mix-knob-turn': [
		{ offset: 0, properties: ['transform'] },
		{ offset: 0.5, properties: ['transform'] },
		{ offset: 1, properties: ['transform'] }
	],
	'hp-arrow-cue': [
		{ offset: 0, properties: ['transform'] },
		{ offset: 0.5, properties: ['transform'] },
		{ offset: 1, properties: ['transform'] }
	],
	'hp-arrow-master': [
		{ offset: 0, properties: ['transform'] },
		{ offset: 0.5, properties: ['transform'] },
		{ offset: 1, properties: ['transform'] }
	],
	'hp-route-dot': [
		{ offset: 0, properties: ['transform', 'opacity'] },
		{ offset: 0.1, properties: ['transform', 'opacity'] },
		{ offset: 0.3, properties: ['transform', 'opacity'] },
		{ offset: 0.48, properties: ['transform', 'opacity'] },
		{ offset: 0.58, properties: ['transform', 'opacity'] },
		{ offset: 0.75, properties: ['transform', 'opacity'] },
		{ offset: 1, properties: ['transform', 'opacity'] }
	],
	'mode-fade-practice': [
		{ offset: 0, properties: ['opacity'] },
		{ offset: 0.45, properties: ['opacity'] },
		{ offset: 0.5, properties: ['opacity'] },
		{ offset: 0.95, properties: ['opacity'] },
		{ offset: 1, properties: ['opacity'] }
	],
	'mode-fade-split': [
		{ offset: 0, properties: ['opacity'] },
		{ offset: 0.45, properties: ['opacity'] },
		{ offset: 0.5, properties: ['opacity'] },
		{ offset: 0.95, properties: ['opacity'] },
		{ offset: 1, properties: ['opacity'] }
	],
	'cue-pulse': [
		{ offset: 0, properties: ['opacity'] },
		{ offset: 0.5, properties: ['opacity'] },
		{ offset: 1, properties: ['opacity'] }
	],
	'fx-send-a': [
		{ offset: 0, properties: ['transform', 'opacity'] },
		{ offset: 0.5, properties: ['transform', 'opacity'] },
		{ offset: 1, properties: ['transform', 'opacity'] }
	],
	'fx-send-b': [
		{ offset: 0, properties: ['transform', 'opacity'] },
		{ offset: 0.5, properties: ['transform', 'opacity'] },
		{ offset: 1, properties: ['transform', 'opacity'] }
	],
	'two-deck-hide': [
		{ offset: 0, properties: ['transform', 'opacity'] },
		{ offset: 0.35, properties: ['transform', 'opacity'] },
		{ offset: 1, properties: ['transform', 'opacity'] }
	],
	'two-deck-grow': [
		{ offset: 0, properties: ['transform'] },
		{ offset: 0.35, properties: ['transform'] },
		{ offset: 1, properties: ['transform'] }
	]
};

/** Allowed animated properties for explainer demos (performance contract). */
const EXPLAINER_DEMO_ALLOWED_PROPERTIES = new Set(['transform', 'opacity']);
