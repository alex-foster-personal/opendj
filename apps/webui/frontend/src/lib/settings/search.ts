import { SETTINGS_CATALOG, type SettingDef, type SettingGroupId } from './catalog';
import { SETTINGS_SYNONYMS } from './synonyms';

export interface FilterOptions {
	/** When true, drop unimplemented (todo) rows. */
	hideTodo: boolean;
	/** Optional LHS category filter; null = all groups. */
	group: SettingGroupId | null;
	/** Extra ids from AI search (appended, no dupes). */
	aiIds?: readonly string[];
	/** The "Show developer pages" pref. `false` drops `devOnly` rows; omitted
	 * keeps every row (the pure-filter default). Callers rendering the UI pass
	 * it explicitly, together with `hideTodo: effectiveHideTodo(...)`. */
	showDev?: boolean;
}

/** Unbuilt (todo) rows are hidden by default for V1 (JIK, Thu 1 Oct 2026):
 * they show only while "Show developer pages" is on AND the developer has not
 * also asked to hide them with the "Hide todo / grayed settings" row. */
export function effectiveHideTodo(prefs: {
	show_dev_ui: boolean;
	hide_todo_settings: boolean;
}): boolean {
	return !prefs.show_dev_ui || prefs.hide_todo_settings;
}

/** Whether a row may appear at all under the given filter options. */
export function settingRowVisible(
	def: SettingDef,
	opts: Pick<FilterOptions, 'hideTodo' | 'showDev'>
): boolean {
	if (opts.hideTodo && !def.implemented) return false;
	if (opts.showDev === false && def.devOnly === true) return false;
	return true;
}

export interface FilterResult {
	/** Keyword / synonym matches (stable catalog order). */
	keyword: SettingDef[];
	/** AI-only extras not already in keyword (catalog order among aiIds). */
	aiExtra: SettingDef[];
	/** keyword + aiExtra for rendering. */
	all: SettingDef[];
}

function _tokens(query: string): string[] {
	return query
		.toLowerCase()
		.split(/[^a-z0-9_.-]+/g)
		.map((t) => t.trim())
		.filter((t) => t.length > 0);
}

function _haystack(s: SettingDef): string {
	return [s.id, s.label, s.group, s.title, s.detail, ...s.keywords].join(' ').toLowerCase();
}

function _expandedTokens(tokens: string[]): Set<string> {
	const out = new Set<string>(tokens);
	for (const t of tokens) {
		const syns = SETTINGS_SYNONYMS[t];
		if (syns) for (const s of syns) out.add(s.toLowerCase());
	}
	return out;
}

function _matches(def: SettingDef, expanded: Set<string>): boolean {
	if (expanded.size === 0) return true;
	const hay = _haystack(def);
	for (const t of expanded) {
		if (hay.includes(t)) return true;
	}
	return false;
}

function _basePool(opts: FilterOptions): SettingDef[] {
	return SETTINGS_CATALOG.filter((s) => {
		if (!settingRowVisible(s, opts)) return false;
		if (opts.group !== null && s.group !== opts.group) return false;
		return true;
	});
}

/** Pure keyword/synonym filter. Empty query returns the full (filtered) pool. */
export function filterSettings(query: string, opts: FilterOptions): FilterResult {
	const pool = _basePool(opts);
	const tokens = _tokens(query);
	const expanded = _expandedTokens(tokens);
	const keyword =
		tokens.length === 0 ? pool.slice() : pool.filter((s) => _matches(s, expanded));
	const keywordIds = new Set(keyword.map((s) => s.id));
	const byId = new Map(SETTINGS_CATALOG.map((s) => [s.id, s]));
	const aiExtra: SettingDef[] = [];
	for (const id of opts.aiIds ?? []) {
		if (keywordIds.has(id)) continue;
		const def = byId.get(id);
		if (!def) continue;
		if (!settingRowVisible(def, opts)) continue;
		if (opts.group !== null && def.group !== opts.group) continue;
		aiExtra.push(def);
	}
	return { keyword, aiExtra, all: [...keyword, ...aiExtra] };
}

/** Groups that still have at least one visible row under current filters. */
export function visibleGroups(
	query: string,
	opts: Omit<FilterOptions, 'group'>
): SettingGroupId[] {
	const { all } = filterSettings(query, { ...opts, group: null });
	const seen = new Set<SettingGroupId>();
	const out: SettingGroupId[] = [];
	for (const s of all) {
		if (seen.has(s.group)) continue;
		seen.add(s.group);
		out.push(s.group);
	}
	return out;
}
