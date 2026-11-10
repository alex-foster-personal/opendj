/**
 * Pure helpers for the axe-core accessibility e2e scan (issue #10367).
 *
 * No Playwright page object: a unit test can import this module and prove
 * allowlist shape, classify/filter logic, and stale checks without booting a
 * browser. The AxeBuilder call lives in ui-axe-audit.spec.ts.
 */

export const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21aa'] as const;
export const FAILING_IMPACTS = ['serious', 'critical'] as const;
export const SURFACES = [
	'performance-more',
	'performance-less',
	'library-context-menu',
	'settings'
] as const;
export type Surface = (typeof SURFACES)[number];
export const PROBE_ATTR = 'data-axe-probe';

export interface AxeNodeLike {
	target: unknown[];
	html?: string;
	failureSummary?: string;
}

export interface AxeViolationLike {
	id: string;
	impact?: string | null;
	help: string;
	helpUrl?: string;
	nodes: AxeNodeLike[];
}

export interface AllowEntry {
	rule: string;
	selector: string;
	surfaces: Surface[];
	reason: string;
	optional?: boolean;
}

export interface DisabledRule {
	rule: string;
	reason: string;
}

export interface Allowlist {
	disabledRules: DisabledRule[];
	findings: AllowEntry[];
}

export interface AxeFinding {
	surface: Surface;
	rule: string;
	impact: string;
	selector: string;
	help: string;
	html: string;
}

const RULE_RE = /^[a-z0-9-]+$/;
const ISSUE_RE = /#\d{3,}/;
const GENERIC_REASON_RE = /^(todo|tbd|fixme|known issue|pre-existing|wontfix|n\/a)\b/i;

/** The `target` chain flattened: shadow or iframe hops joined with ' >>> '. */
export function selectorOf(node: AxeNodeLike): string {
	const parts: string[] = [];
	for (const hop of node.target) {
		if (typeof hop === 'string') parts.push(hop);
		else if (Array.isArray(hop)) parts.push(hop.filter((x) => typeof x === 'string').join(' >>> '));
		else parts.push(String(hop));
	}
	return parts.join(' >>> ');
}

/** One finding per (rule, selector), carrying the violation's impact. */
export function flatten(surface: Surface, violations: AxeViolationLike[]): AxeFinding[] {
	const out: AxeFinding[] = [];
	const seen = new Set<string>();
	for (const v of violations) {
		const impact = v.impact ?? '';
		for (const node of v.nodes) {
			const selector = selectorOf(node);
			const key = `${v.id}\0${selector}`;
			if (seen.has(key)) continue;
			seen.add(key);
			out.push({
				surface,
				rule: v.id,
				impact,
				selector,
				help: v.help,
				html: node.html ?? ''
			});
		}
	}
	return out;
}

export interface Classified {
	unexpected: AxeFinding[];
	allowed: AxeFinding[];
	info: AxeFinding[];
}

function isFailingImpact(impact: string): boolean {
	return (FAILING_IMPACTS as readonly string[]).includes(impact);
}

function entryMatches(entry: AllowEntry, finding: AxeFinding): boolean {
	return (
		entry.rule === finding.rule &&
		entry.selector === finding.selector &&
		entry.surfaces.includes(finding.surface)
	);
}

/** serious/critical not on the list -> unexpected; on the list for this surface -> allowed; everything else -> info. */
export function classify(findings: AxeFinding[], allow: Allowlist): Classified {
	const unexpected: AxeFinding[] = [];
	const allowed: AxeFinding[] = [];
	const info: AxeFinding[] = [];
	for (const f of findings) {
		if (!isFailingImpact(f.impact)) {
			info.push(f);
			continue;
		}
		const match = allow.findings.find((e) => entryMatches(e, f));
		if (match) allowed.push(f);
		else unexpected.push(f);
	}
	return { unexpected, allowed, info };
}

/** Entries (not optional) with a surface where no allowed finding matched: `${rule} | ${selector} | ${surface}`. */
export function staleEntries(allow: Allowlist, allowed: AxeFinding[]): string[] {
	const stale: string[] = [];
	for (const entry of allow.findings) {
		if (entry.optional) continue;
		for (const surface of entry.surfaces) {
			const hit = allowed.some(
				(f) => f.rule === entry.rule && f.selector === entry.selector && f.surface === surface
			);
			if (!hit) stale.push(`${entry.rule} | ${entry.selector} | ${surface}`);
		}
	}
	return stale;
}

function reasonBodyWithoutIssue(reason: string): string {
	return reason
		.replace(/#\d{3,}/g, '')
		.replace(/[^\w\s]/g, ' ')
		.replace(/\s+/g, ' ')
		.trim();
}

function checkReason(reason: string, label: string): string[] {
	const problems: string[] = [];
	const trimmed = reason.trim();
	if (trimmed.length < 20) problems.push(`${label}: reason shorter than 20 characters`);
	if (!ISSUE_RE.test(trimmed)) problems.push(`${label}: reason must name an issue (#NNN)`);
	const body = reasonBodyWithoutIssue(trimmed);
	if (body.length < 20) problems.push(`${label}: reason too generic once issue reference stripped`);
	if (GENERIC_REASON_RE.test(trimmed)) problems.push(`${label}: generic reason word`);
	return problems;
}

/** Problems with the allowlist itself; [] when it is valid. */
export function allowlistProblems(allow: Allowlist): string[] {
	const problems: string[] = [];
	const pairKeys = new Set<string>();
	const disabled = new Set(allow.disabledRules.map((d) => d.rule));

	for (const d of allow.disabledRules) {
		const label = `disabledRules rule=${d.rule}`;
		if (!d.rule || !RULE_RE.test(d.rule)) problems.push(`${label}: invalid rule id`);
		problems.push(...checkReason(d.reason, label));
	}

	for (const [i, entry] of allow.findings.entries()) {
		const label = `findings[${i}] rule=${entry.rule} selector=${entry.selector}`;
		if (!entry.rule || !RULE_RE.test(entry.rule)) problems.push(`${label}: invalid rule id`);
		if (!entry.selector?.trim()) problems.push(`${label}: empty selector`);
		if (!entry.surfaces?.length) problems.push(`${label}: surfaces empty`);
		const surfaceSet = new Set<Surface>();
		for (const s of entry.surfaces ?? []) {
			if (!(SURFACES as readonly string[]).includes(s))
				problems.push(`${label}: unknown surface ${s}`);
			else if (surfaceSet.has(s)) problems.push(`${label}: duplicate surface ${s}`);
			else surfaceSet.add(s);
		}
		problems.push(...checkReason(entry.reason, label));
		const pair = `${entry.rule}\0${entry.selector}`;
		if (pairKeys.has(pair)) problems.push(`${label}: duplicate (rule, selector)`);
		pairKeys.add(pair);
		if (disabled.has(entry.rule)) problems.push(`${label}: rule also listed in disabledRules`);
	}

	return problems;
}

/** Pipe table lines: `impact | surface | rule | selector | help`. */
export function formatTable(findings: AxeFinding[]): string {
	return findings
		.map((f) => `${f.impact} | ${f.surface} | ${f.rule} | ${f.selector} | ${f.help}`)
		.join('\n');
}
