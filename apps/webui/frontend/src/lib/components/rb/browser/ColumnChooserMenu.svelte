<script module lang="ts">
	import type { HideableColumn } from '$lib/rb/library-columns';
	import type { KeyNotation } from '$lib/rb/key-notation';
	import type { TagTypeChooserEntry } from '$lib/rb/library-tags.svelte';
	import type { UiSkin } from '$lib/rb/ui-skin';

	/** The ui-prefs state and setters the chooser reads and calls. The table
	 * passes them in (it already imports prefs.svelte.ts), so this menu adds
	 * no importer to that module. */
	export interface ColumnChooserPrefs {
		state: {
			readonly library_hidden_columns: readonly HideableColumn[];
			readonly key_notation: KeyNotation;
			readonly library_density: 'compact' | 'cosy';
			readonly ui_skin: UiSkin;
		};
		toggleColumn: (col: HideableColumn) => void;
		tagTypes: () => TagTypeChooserEntry[];
		toggleTagTypeColumn: (entry: TagTypeChooserEntry) => void;
		toggleTagTypeInTags: (entry: TagTypeChooserEntry) => void;
		showAllColumns: () => void;
		setKeyNotation: (next: KeyNotation) => void;
		setDensity: (next: 'compact' | 'cosy') => void;
		setSkin: (next: UiSkin) => void;
	}
</script>

<script lang="ts">
	// LIBUX-54: the track table header's right-click chooser. One flat menu:
	// a checkbox per hideable column, Show all columns, then Key format,
	// Density and Skin, each through its existing ui-prefs setter. Hosted by
	// TrackContextMenu, which owns the table's right-click menus.
	import type { ComponentProps } from 'svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import { HIDEABLE_COLUMN_LABELS, HIDEABLE_COLUMNS } from '$lib/rb/library-columns';
	import { KEY_NOTATIONS, keyNotationChoiceLabel } from '$lib/rb/key-notation';
	import { UI_SKIN_CHOICES, UI_SKIN_LABELS } from '$lib/rb/ui-skin';
	import { OPTIONAL_COLUMN_LABELS, OPTIONAL_COLUMNS } from '$lib/rb/library-works';
	import {
		reportWorksFailure,
		setClassicalLayout,
		setOptionalColumn,
		worksView
	} from '$lib/rb/works-overlay.svelte';

	type MenuItem = ComponentProps<typeof ContextMenu>['items'][number];

	let {
		x,
		y,
		prefs,
		onclose
	}: { x: number; y: number; prefs: ColumnChooserPrefs; onclose: () => void } = $props();
	const uiPrefs = $derived(prefs.state);

	const hidden = $derived(new Set(uiPrefs.library_hidden_columns));

	/** The shared menu draws no tick (and the chrome bans text-symbol glyphs),
	 * so the label says the state: which action a column item takes, and which
	 * setting is current. aria-checked carries it for assistive tech. */
	function _current(on: boolean, label: string): string {
		return on ? `${label} (current)` : label;
	}

	const items = $derived.by((): MenuItem[] => [
		...HIDEABLE_COLUMNS.map((col) => ({
			id: `col-${col}`,
			label: `${hidden.has(col) ? 'Show' : 'Hide'} column: ${HIDEABLE_COLUMN_LABELS[col]}`,
			checked: !hidden.has(col),
			testId: `column-chooser-${col}`,
			run: () => prefs.toggleColumn(col)
		})),
		...OPTIONAL_COLUMNS.map((col) => {
			const on = worksView.optionalColumns.includes(col);
			return {
				id: `col-${col}`,
				label: `${on ? 'Hide' : 'Show'} column: ${OPTIONAL_COLUMN_LABELS[col]}`,
				checked: on,
				testId: `column-chooser-${col}`,
				run: () =>
					void setOptionalColumn(col, !on).catch((exc: unknown) =>
						reportWorksFailure('column change failed', exc)
					)
			};
		}),
		{
			id: 'classical-layout',
			label: `${worksView.classicalLayout ? 'Turn off' : 'Turn on'} Classical layout`,
			checked: worksView.classicalLayout,
			testId: 'column-chooser-classical-layout',
			run: () =>
				void setClassicalLayout(!worksView.classicalLayout).catch((exc: unknown) =>
					reportWorksFailure('layout change failed', exc)
				)
		},
		hidden.size === 0
			? {
					id: 'col-show-all',
					label: 'Show all columns',
					testId: 'column-chooser-show-all',
					title: 'Every column is already shown'
				}
			: {
					id: 'col-show-all',
					label: 'Show all columns',
					testId: 'column-chooser-show-all',
					run: prefs.showAllColumns
				},
		...prefs.tagTypes().flatMap((entry) => [
			{
				id: `tagtype-col-${entry.id}`,
				label: `${entry.column ? 'Hide' : 'Show'} column: ${entry.label}`,
				checked: entry.column,
				testId: `column-chooser-tagtype-${entry.id}`,
				run: () => prefs.toggleTagTypeColumn(entry)
			},
			{
				id: `tagtype-tags-${entry.id}`,
				label: `${entry.inTags ? 'Leave out of' : 'Also show in'} Tags: ${entry.label}`,
				checked: entry.inTags,
				testId: `column-chooser-tagtype-tags-${entry.id}`,
				run: () => prefs.toggleTagTypeInTags(entry)
			}
		]),
		...KEY_NOTATIONS.map((notation) => ({
			id: `key-${notation}`,
			label: _current(
				uiPrefs.key_notation === notation,
				`Key format: ${keyNotationChoiceLabel(notation)}`
			),
			checked: uiPrefs.key_notation === notation,
			testId: `column-chooser-key-${notation}`,
			run: () => prefs.setKeyNotation(notation)
		})),
		...(['compact', 'cosy'] as const).map((density) => ({
			id: `density-${density}`,
			label: _current(uiPrefs.library_density === density, `Density: ${density === 'compact' ? 'Compact' : 'Cosy'}`),
			checked: uiPrefs.library_density === density,
			testId: `column-chooser-density-${density}`,
			run: () => prefs.setDensity(density)
		})),
		...UI_SKIN_CHOICES.map((skin) => ({
			id: `skin-${skin}`,
			label: _current(uiPrefs.ui_skin === skin, `Skin: ${UI_SKIN_LABELS[skin]}`),
			checked: uiPrefs.ui_skin === skin,
			testId: `column-chooser-skin-${skin}`,
			run: () => prefs.setSkin(skin)
		}))
	]);
</script>

<ContextMenu {items} {x} {y} {onclose} />
