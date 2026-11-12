/**
 * STRUCT-05 master switch plus STRUCTUI-12787 display-style prefs (issue #11436).
 * Persisted under /api/v1/ui-prefs; default off for show_sections so existing
 * libraries stay unchanged until the DJ opts in.
 */
export const SECTIONS_MAIN_STYLE_VALUES = ['m1_tint', 'm2_recolor', 'm3_lines', 'm4_base'] as const;
export type SectionsMainStyle = (typeof SECTIONS_MAIN_STYLE_VALUES)[number];

export const SECTIONS_OVERVIEW_STYLE_VALUES = ['o1_strip', 'o2_tint', 'o3_recolor'] as const;
export type SectionsOverviewStyle = (typeof SECTIONS_OVERVIEW_STYLE_VALUES)[number];

export const SECTIONS_UNCERTAIN_VALUES = ['hide', 'show_faded'] as const;
export type SectionsUncertain = (typeof SECTIONS_UNCERTAIN_VALUES)[number];

export interface SectionOverlayPrefsState {
	show_sections: boolean;
	sections_main_style: SectionsMainStyle;
	sections_sticky_title: boolean;
	sections_next_countdown: boolean;
	sections_overview_style: SectionsOverviewStyle;
	sections_library_structure_column: boolean;
	sections_preview_cursor_label: boolean;
	sections_uncertain: SectionsUncertain;
}

export const SECTION_OVERLAY_PREF_DEFAULTS: SectionOverlayPrefsState = {
	show_sections: false,
	sections_main_style: 'm1_tint',
	sections_sticky_title: true,
	sections_next_countdown: true,
	sections_overview_style: 'o1_strip',
	sections_library_structure_column: true,
	sections_preview_cursor_label: true,
	sections_uncertain: 'hide'
};

export function parseSectionsMainStyle(value: unknown): SectionsMainStyle | undefined {
	return SECTIONS_MAIN_STYLE_VALUES.find((v) => v === value);
}

export function requireSectionsMainStyle(value: unknown, field: string): SectionsMainStyle {
	const parsed = parseSectionsMainStyle(value);
	if (parsed === undefined) {
		throw new TypeError(
			`${field} must be ${SECTIONS_MAIN_STYLE_VALUES.join('|')}, got ${String(value)}`
		);
	}
	return parsed;
}

export function parseSectionsOverviewStyle(value: unknown): SectionsOverviewStyle | undefined {
	return SECTIONS_OVERVIEW_STYLE_VALUES.find((v) => v === value);
}

export function requireSectionsOverviewStyle(value: unknown, field: string): SectionsOverviewStyle {
	const parsed = parseSectionsOverviewStyle(value);
	if (parsed === undefined) {
		throw new TypeError(
			`${field} must be ${SECTIONS_OVERVIEW_STYLE_VALUES.join('|')}, got ${String(value)}`
		);
	}
	return parsed;
}

export function parseSectionsUncertain(value: unknown): SectionsUncertain | undefined {
	return SECTIONS_UNCERTAIN_VALUES.find((v) => v === value);
}

export function requireSectionsUncertain(value: unknown, field: string): SectionsUncertain {
	const parsed = parseSectionsUncertain(value);
	if (parsed === undefined) {
		throw new TypeError(
			`${field} must be ${SECTIONS_UNCERTAIN_VALUES.join('|')}, got ${String(value)}`
		);
	}
	return parsed;
}

/** Validate stored section-display prefs; absent keys keep defaults. */
export function validateSectionOverlayPrefFields(
	parsed: Record<string, unknown>,
	storageKey: string
): Partial<SectionOverlayPrefsState> {
	const out: Partial<SectionOverlayPrefsState> = {};
	if ('show_sections' in parsed) {
		const value = parsed.show_sections;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (show_sections is not a boolean) - clear the localStorage key to recover`
			);
		}
		out.show_sections = value;
	}
	if ('sections_main_style' in parsed) {
		const value = parseSectionsMainStyle(parsed.sections_main_style);
		if (value === undefined) {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_main_style is not ${SECTIONS_MAIN_STYLE_VALUES.join('|')}) - ` +
					'clear the localStorage key to recover'
			);
		}
		out.sections_main_style = value;
	}
	if ('sections_sticky_title' in parsed) {
		const value = parsed.sections_sticky_title;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_sticky_title is not a boolean)`
			);
		}
		out.sections_sticky_title = value;
	}
	if ('sections_next_countdown' in parsed) {
		const value = parsed.sections_next_countdown;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_next_countdown is not a boolean)`
			);
		}
		out.sections_next_countdown = value;
	}
	if ('sections_overview_style' in parsed) {
		const value = parseSectionsOverviewStyle(parsed.sections_overview_style);
		if (value === undefined) {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_overview_style is not ${SECTIONS_OVERVIEW_STYLE_VALUES.join('|')})`
			);
		}
		out.sections_overview_style = value;
	}
	if ('sections_library_structure_column' in parsed) {
		const value = parsed.sections_library_structure_column;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_library_structure_column is not a boolean)`
			);
		}
		out.sections_library_structure_column = value;
	}
	if ('sections_preview_cursor_label' in parsed) {
		const value = parsed.sections_preview_cursor_label;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_preview_cursor_label is not a boolean)`
			);
		}
		out.sections_preview_cursor_label = value;
	}
	if ('sections_uncertain' in parsed) {
		const value = parseSectionsUncertain(parsed.sections_uncertain);
		if (value === undefined) {
			throw new Error(
				`${storageKey}: malformed prefs blob (sections_uncertain is not ${SECTIONS_UNCERTAIN_VALUES.join('|')})`
			);
		}
		out.sections_uncertain = value;
	}
	return out;
}

export interface SectionOverlaySetters {
	setShowSections(next: boolean): void;
	toggleShowSections(): void;
	setSectionsMainStyle(next: SectionsMainStyle): void;
	setSectionsStickyTitle(next: boolean): void;
	setSectionsNextCountdown(next: boolean): void;
	setSectionsOverviewStyle(next: SectionsOverviewStyle): void;
	setSectionsLibraryStructureColumn(next: boolean): void;
	setSectionsPreviewCursorLabel(next: boolean): void;
	setSectionsUncertain(next: SectionsUncertain): void;
}

/** Every setter writes localStorage AND PUTs /api/v1/ui-prefs (agent parity). */
export function makeSectionOverlaySetters(
	state: SectionOverlayPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<SectionOverlayPrefsState>) => void
): SectionOverlaySetters {
	function setShowSections(next: boolean): void {
		state.show_sections = next;
		persist();
		syncDiskPrefs({ show_sections: next });
	}
	return {
		setShowSections,
		toggleShowSections: () => setShowSections(!state.show_sections),
		setSectionsMainStyle(next: SectionsMainStyle): void {
			const value = requireSectionsMainStyle(next, 'sections_main_style');
			state.sections_main_style = value;
			persist();
			syncDiskPrefs({ sections_main_style: value });
		},
		setSectionsStickyTitle(next: boolean): void {
			state.sections_sticky_title = next;
			persist();
			syncDiskPrefs({ sections_sticky_title: next });
		},
		setSectionsNextCountdown(next: boolean): void {
			state.sections_next_countdown = next;
			persist();
			syncDiskPrefs({ sections_next_countdown: next });
		},
		setSectionsOverviewStyle(next: SectionsOverviewStyle): void {
			const value = requireSectionsOverviewStyle(next, 'sections_overview_style');
			state.sections_overview_style = value;
			persist();
			syncDiskPrefs({ sections_overview_style: value });
		},
		setSectionsLibraryStructureColumn(next: boolean): void {
			state.sections_library_structure_column = next;
			persist();
			syncDiskPrefs({ sections_library_structure_column: next });
		},
		setSectionsPreviewCursorLabel(next: boolean): void {
			state.sections_preview_cursor_label = next;
			persist();
			syncDiskPrefs({ sections_preview_cursor_label: next });
		},
		setSectionsUncertain(next: SectionsUncertain): void {
			const value = requireSectionsUncertain(next, 'sections_uncertain');
			state.sections_uncertain = value;
			persist();
			syncDiskPrefs({ sections_uncertain: value });
		}
	};
}
