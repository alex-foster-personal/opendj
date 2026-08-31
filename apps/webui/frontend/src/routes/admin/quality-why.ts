/**
 * Human labels and explanations for the code-quality ratchet panel.
 *
 * Presentation-layer copy for the flat metric keys served by
 * GET /api/v1/admin/quality-ratchet (ops/quality/baseline.json), deliberately
 * NOT in that file: it is a thin, machine-checked allowance table (see
 * scripts/quality_gate.py), and this is prose glued on for the card.
 *
 * scripts/quality_gate.py is a RATCHET, not a standard: every number here is
 * the current allowance, and a merge that raises one fails the gate. Lower is
 * better for every metric in this table, without exception - there is no
 * "higher is better" quality-debt count.
 */

export interface QualityMetricDef {
	label: string;
	unit: string;
	title: string;
	/** True only for the one metric that is hard-gated at zero regardless of
	 * baseline (see ops/quality/README.md), rather than an allowance that can
	 * be raised in a defended diff. */
	hardGated?: boolean;
}

export const QUALITY_METRIC_DEFS: Record<string, QualityMetricDef> = {
	'ruff.total': {
		label: 'Lint findings',
		unit: 'findings',
		title:
			'Every ruff finding across the Python tree, every rule family combined. A ratchet, not a ' +
			'standard: nothing is required to be clean, but a merge that raises this number fails the ' +
			'gate (scripts/quality_gate.py).'
	},
	'ruff.complexity': {
		label: 'Lint: complexity',
		unit: 'findings',
		title: 'Ruff findings tagged as excess complexity (nested branching, nested loops, ...).'
	},
	'ruff.coupling': {
		label: 'Lint: coupling',
		unit: 'findings',
		title: 'Ruff findings tagged as excess coupling between modules.'
	},
	'ruff.safety': {
		label: 'Lint: safety',
		unit: 'findings',
		title:
			'Bare excepts and similar fail-fast violations. A rising number here means an error got ' +
			'swallowed instead of raised, which breaks this repo’s brittle, fail-fast house rule.'
	},
	'ruff.correctness': {
		label: 'Lint: correctness',
		unit: 'findings',
		title: 'Ruff findings that flag a likely bug rather than a style or structure issue.'
	},
	'ruff.style': {
		label: 'Lint: style',
		unit: 'findings',
		title: 'Ruff findings that are pure style: formatting, naming, import order.'
	},
	'complexity.worst_block': {
		label: 'Worst function complexity',
		unit: 'cyclomatic',
		title: 'The single most complex function or method in the Python tree, by cyclomatic complexity (radon).'
	},
	'complexity.blocks_over_limit': {
		label: 'Functions over the complexity limit',
		unit: 'functions',
		title: 'Count of Python functions/methods over the mccabe complexity threshold this repo gates on.'
	},
	'complexity.low_maintainability_files': {
		label: 'Low-maintainability files',
		unit: 'files',
		title: 'Python files radon scores below maintainability rank A.'
	},
	'arch.contracts_broken': {
		label: 'Architecture contract violations',
		unit: 'violations',
		hardGated: true,
		title:
			'Import-linter contracts in .importlinter (e.g. apps.shared must never import apps.webui). ' +
			'HARD-GATED AT ZERO regardless of baseline: an architecture rule with a growing allowance is ' +
			'not a rule. Existing debt is carried as a named, dated list of exact imports in ' +
			'.importlinter instead of a number that is allowed to rise.'
	},
	'python.package_cycles': {
		label: 'Python import cycles',
		unit: 'cycles',
		title: 'Package-level import cycles in the Python tree (A imports B imports A).'
	},
	'frontend.import_cycles': {
		label: 'Frontend import cycles',
		unit: 'cycles',
		title: 'Import cycles across both .ts and .svelte files (madge parses zero imports out of .svelte and reports a false clean, so this repo runs its own graph instead).'
	},
	'frontend.max_fan_in': {
		label: 'Worst frontend fan-in',
		unit: 'importers',
		title: 'The most-imported-from frontend module: how many other files import it.'
	},
	'frontend.max_fan_out': {
		label: 'Worst frontend fan-out',
		unit: 'imports',
		title: 'The frontend file with the most imports of its own: real coupling when every import is used, a refactor target when it is not.'
	},
	'frontend.unused_files': {
		label: 'Orphaned frontend files',
		unit: 'files',
		title: 'Frontend files (knip) that nothing imports any more.'
	},
	'frontend.unused_exports': {
		label: 'Unused frontend exports',
		unit: 'exports',
		title: 'Exported symbols (knip) that no other file imports.'
	},
	'frontend.unused_deps': {
		label: 'Unused frontend dependencies',
		unit: 'packages',
		title: 'package.json dependencies (knip) nothing in the frontend actually imports.'
	},
	'file_size.max_python': {
		label: 'Longest Python file',
		unit: 'lines',
		title: 'The largest Python file in the tree, by line count.'
	},
	'file_size.over_limit_python': {
		label: 'Python files over the size limit',
		unit: 'files',
		title: 'Count of Python files past this repo’s review-size threshold.'
	},
	'file_size.max_frontend': {
		label: 'Longest frontend file',
		unit: 'lines',
		title:
			'The largest frontend file, by line count. api-types.ts (generated from openapi.json via ' +
			'pnpm api:gen) is usually the top offender: its growth tracks the API contract, not authored code.'
	},
	'file_size.over_limit_frontend': {
		label: 'Frontend files over the size limit',
		unit: 'files',
		title: 'Count of frontend files past this repo’s review-size threshold.'
	},
	'duplication.percent': {
		label: 'Duplicated code',
		unit: '%',
		title: 'Percentage of duplicated lines across the tree (jscpd): copy-pasted blocks, not shared code.'
	}
};
