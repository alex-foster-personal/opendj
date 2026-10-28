/**
 * Permutation harness for the right-click registry (W28, CTXMENU-04).
 *
 * The context space grows with every new fact a menu keys on, so the full
 * product stops being reviewable fast. This module picks a pairwise covering
 * set instead (every pair of values from any two fields appears in at least
 * one row), resolves the menu for each row, and renders one markdown matrix
 * per target kind. The unit suite compares those matrices to
 * (private path)/<kind>.md, so any menu change is a reviewable diff.
 *
 * Pure and deterministic: same registry, same output, byte for byte.
 */
import {
	MENU_CONTRIBUTIONS,
	menuFor,
	type MenuContextByKind,
	type MenuTarget,
	type MenuTargetKind,
	type ResolvedMenuItem
} from './registry';
import { MENU_COMMANDS, type MenuCommand } from './commands';

type Space<C> = { [F in keyof C]-?: readonly C[F][] };

/** The finite domain of every context field, per kind. A field missing here is
 * a type error, so a new context fact cannot dodge the harness. */
export const MENU_CONTEXT_SPACE: { [K in MenuTargetKind]: Space<MenuContextByKind[K]> } = {
	track: {
		selection: ['one', 'many'],
		source: ['collection', 'playlist', 'usb', 'readonly-import'],
		audio: ['present', 'missing', 'streaming'],
		stems: ['none', 'ready'],
		workspace: ['performance', 'library']
	},
	'track-chip': { audio: ['present', 'missing', 'streaming'] },
	'playlists-root': {},
	playlist: { forbidDuplicates: [false, true] },
	'playlist-folder': {},
	smartlist: {},
	'tree-readonly': { stick: [false, true] },
	'library-source': {},
	deck: {
		loaded: [false, true],
		playing: [false, true],
		looping: [false, true],
		synced: [false, true],
		master: [false, true]
	},
	'hot-cue': { loaded: [false, true], filled: [false, true], moveLoses: ['nothing', 'color', 'loop'] },
	waveform: { loaded: [false, true], looping: [false, true], vocalHit: [false, true] },
	'mixer-channel': {
		eqFlat: [false, true],
		filterFlat: [false, true],
		cue: [false, true],
		assign: ['A', 'B', 'THRU']
	},
	'perf-empty': {}
};

/** Combinations that cannot happen in the app; the harness never asks for them. */
const IMPOSSIBLE: { [K in MenuTargetKind]?: (ctx: MenuContextByKind[K]) => boolean } = {
	deck: (c) => !c.loaded && (c.playing || c.looping || c.synced || c.master),
	'hot-cue': (c) => (!c.loaded && c.filled) || (!c.filled && c.moveLoses !== 'nothing'),
	waveform: (c) => !c.loaded && (c.looping || c.vocalHit)
};

/** The target each kind is rendered for. Track rows key their label on count. */
export function sampleTarget<K extends MenuTargetKind>(
	kind: K,
	ctx: MenuContextByKind[K]
): Extract<MenuTarget, { kind: K }> {
	const targets: { [T in MenuTargetKind]: (c: MenuContextByKind[T]) => Extract<MenuTarget, { kind: T }> } = {
		track: (c) => ({ kind: 'track', count: c.selection === 'many' ? 3 : 1 }),
		'track-chip': () => ({ kind: 'track-chip' }),
		'playlists-root': () => ({ kind: 'playlists-root' }),
		playlist: () => ({ kind: 'playlist' }),
		'playlist-folder': () => ({ kind: 'playlist-folder' }),
		smartlist: () => ({ kind: 'smartlist' }),
		'tree-readonly': () => ({ kind: 'tree-readonly' }),
		'library-source': () => ({ kind: 'library-source' }),
		deck: () => ({ kind: 'deck', deck: 1 }),
		'hot-cue': () => ({ kind: 'hot-cue', deck: 1, slot: 'A' }),
		waveform: () => ({ kind: 'waveform', deck: 1 }),
		'mixer-channel': () => ({ kind: 'mixer-channel', deck: 1 }),
		'perf-empty': () => ({ kind: 'perf-empty' })
	};
	return (targets[kind] as (c: MenuContextByKind[K]) => Extract<MenuTarget, { kind: K }>)(ctx);
}

function product(fields: string[], space: Record<string, readonly unknown[]>): Record<string, unknown>[] {
	let rows: Record<string, unknown>[] = [{}];
	for (const field of fields) {
		const next: Record<string, unknown>[] = [];
		for (const row of rows) for (const value of space[field] ?? []) next.push({ ...row, [field]: value });
		rows = next;
	}
	return rows;
}

function pairKeys(fields: string[], row: Record<string, unknown>): string[] {
	const keys: string[] = [];
	for (let i = 0; i < fields.length; i += 1) {
		for (let j = i + 1; j < fields.length; j += 1) {
			keys.push(`${fields[i]}=${String(row[fields[i]])}|${fields[j]}=${String(row[fields[j]])}`);
		}
	}
	return keys;
}

/**
 * A pairwise covering set over the kind's valid contexts: greedy, picking the
 * row that covers the most still-uncovered pairs, ties to product order.
 * With fewer than two fields that is simply every valid value.
 */
export function pairwiseContexts<K extends MenuTargetKind>(kind: K): {
	rows: MenuContextByKind[K][];
	validCount: number;
} {
	const space: Record<string, readonly unknown[]> = MENU_CONTEXT_SPACE[kind];
	const fields = Object.keys(space);
	const impossible = IMPOSSIBLE[kind] as ((c: MenuContextByKind[K]) => boolean) | undefined;
	const valid = product(fields, space).filter(
		(row) => impossible === undefined || !impossible(row as MenuContextByKind[K])
	);
	if (fields.length < 2) return { rows: valid as MenuContextByKind[K][], validCount: valid.length };
	const uncovered = new Set(valid.flatMap((row) => pairKeys(fields, row)));
	const chosen: Record<string, unknown>[] = [];
	while (uncovered.size > 0) {
		let best: Record<string, unknown> | null = null;
		let bestGain = 0;
		for (const row of valid) {
			const gain = pairKeys(fields, row).filter((key) => uncovered.has(key)).length;
			if (gain > bestGain) {
				best = row;
				bestGain = gain;
			}
		}
		if (best === null) break;
		chosen.push(best);
		for (const key of pairKeys(fields, best)) uncovered.delete(key);
	}
	return { rows: chosen as MenuContextByKind[K][], validCount: valid.length };
}

export interface MatrixRow<K extends MenuTargetKind> {
	ctx: MenuContextByKind[K];
	items: ResolvedMenuItem[];
}

export function resolveMatrix<K extends MenuTargetKind>(kind: K): {
	rows: MatrixRow<K>[];
	validCount: number;
} {
	const { rows, validCount } = pairwiseContexts(kind);
	return {
		rows: rows.map((ctx) => ({ ctx, items: menuFor<K>(sampleTarget(kind, ctx), ctx) })),
		validCount
	};
}

function commandText(command: MenuCommand): string {
	return command.via === 'ipc' ? `IPC \`${command.type}\`` : `\`${command.method} ${command.path}\``;
}

const LETTERS = 'abcdefghijklmnopqrstuvwxyz';

/** The golden markdown for one kind. */
export function renderMatrix(kind: MenuTargetKind): string {
	const { rows, validCount } = resolveMatrix(kind);
	const contributions = MENU_CONTRIBUTIONS[kind] as { id: string; command: keyof typeof MENU_COMMANDS }[];
	const fields = Object.keys(MENU_CONTEXT_SPACE[kind]);
	const reasons: string[] = [];
	const labels = new Map<string, Set<string>>();
	const reasonCode = (reason: string): string => {
		let index = reasons.indexOf(reason);
		if (index < 0) {
			reasons.push(reason);
			index = reasons.length - 1;
		}
		return LETTERS[index] ?? String(index);
	};

	const out: string[] = [
		`# Right-click menu: \`${kind}\``,
		'',
		'Generated from `apps/webui/frontend/src/lib/context-menu/registry.ts` by',
		'`tests/unit/context-menu-registry.test.mjs`. Do not edit by hand; regenerate with',
		'`UPDATE_CONTEXT_MENU_GOLDENS=1 node --test tests/unit/context-menu-registry.test.mjs`',
		'from `apps/webui/frontend`.',
		''
	];
	if (contributions.length === 0) {
		out.push(
			'**No items.** This target deliberately resolves to an empty menu: the host',
			'suppresses the browser menu and the Quick Draw fallback, and shows nothing.',
			''
		);
		return out.join('\n');
	}
	out.push(
		fields.length < 2
			? `Coverage: every valid context (${rows.length} of ${validCount}).`
			: `Coverage: pairwise over ${fields.length} context fields, ${rows.length} rows covering every value pair of ${validCount} valid combinations.`,
		'',
		'Cells: `on` enabled, `[x]`/`[ ]` a check item and its state, `off (a)` shown disabled for reason (a), `-` hidden.',
		''
	);
	const header = fields.length === 0 ? ['context', ...contributions.map((c) => c.id)] : [...fields, ...contributions.map((c) => c.id)];
	out.push(`| ${header.join(' | ')} |`, `| ${header.map(() => '---').join(' | ')} |`);
	for (const row of rows) {
		const ctxCells = fields.length === 0 ? ['(none)'] : fields.map((f) => String((row.ctx as Record<string, unknown>)[f]));
		const itemCells = contributions.map((c) => {
			const item = row.items.find((i) => i.id === c.id);
			if (item === undefined) return '-';
			const seen = labels.get(c.id) ?? new Set<string>();
			seen.add(item.label);
			labels.set(c.id, seen);
			const check = item.checked === undefined ? '' : item.checked ? '[x] ' : '[ ] ';
			if (!item.enabled) return `${check}off (${reasonCode(item.disabledReason ?? '')})`;
			return `${check}on`;
		});
		out.push(`| ${[...ctxCells, ...itemCells].join(' | ')} |`);
	}
	if (reasons.length > 0) {
		out.push('', '## Disabled reasons', '');
		reasons.forEach((reason, i) => out.push(`- (${LETTERS[i] ?? i}) ${reason}`));
	}
	out.push('', '## Items', '', '| item | labels seen | command |', '| --- | --- | --- |');
	for (const c of contributions) {
		const seen = [...(labels.get(c.id) ?? new Set<string>())].map((l) => `"${l}"`).join(', ');
		out.push(`| ${c.id} | ${seen === '' ? '(never shown)' : seen} | ${commandText(MENU_COMMANDS[c.command])} |`);
	}
	out.push('');
	return out.join('\n');
}

export const MENU_TARGET_KINDS = Object.keys(MENU_CONTEXT_SPACE) as MenuTargetKind[];
