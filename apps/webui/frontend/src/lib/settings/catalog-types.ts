/**
 * Shape of a settings catalog entry. Kept apart from catalog.ts so a setting
 * module the catalog imports can type itself without importing the catalog back.
 */

export type SettingGroupId =
	| 'appearance'
	| 'library'
	| 'performance'
	| 'confirmations'
	| 'sync'
	| 'cloudsync'
	| 'advanced'
	| 'rekordbox'
	| 'djay';

export interface SettingGroup {
	id: SettingGroupId;
	label: string;
}

export type SettingControl =
	| { kind: 'boolean' }
	| { kind: 'enum'; options: ReadonlyArray<{ value: string; label: string }> }
	| {
			kind: 'multi_bool';
			keys: ReadonlyArray<{ id: string; label: string; title: string }>;
	  }
	// Pure-navigation entry: searchable pointer to a full route page. The
	// overlay renders an `<a href>` control and `activateSetting` calls `goto`.
	| { kind: 'link'; href: string }
	// A live numeric row: range slider + value readout + a "default" reset.
	// Bounds come from the module that VALIDATES the value (never a second
	// literal here), so no slider position can be one the setter refuses.
	| {
			kind: 'number';
			min: number;
			max: number;
			step: number;
			/** What "Reset to default" restores, and the value shown as default. */
			defaultValue: number;
			/** Rendered after the readout, e.g. 'x'. */
			unit: string;
	  }
	// One path per line; saved via overlay Apply (async existence check on server).
	| { kind: 'path_lines'; v2Notice: string };

export interface SettingDef {
	id: string;
	label: string;
	group: SettingGroupId;
	keywords: readonly string[];
	/** Short RHS hover tooltip. */
	title: string;
	/** Longer explanation shown on focus/hover. */
	detail: string;
	/** false = grayed inert todo (PARITY-TODO). Hidden unless the
	 * "Show developer pages" pref (show_dev_ui) is on. */
	implemented: boolean;
	/** true = a developer-only row, shown only while show_dev_ui is on. */
	devOnly?: boolean;
	control: SettingControl;
}
