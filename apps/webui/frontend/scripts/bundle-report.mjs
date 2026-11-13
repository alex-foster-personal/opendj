// bundle-report.mjs -- the top modules by estimated gzip bytes on one budget surface.
//
// The budget gate (scripts/bundle-budget.mjs) reports per-surface totals only, so a payback note
// ("load X on first use") was chosen by reading imports, not bytes. This joins the per-module rows
// written by vite-bundle-report.ts (BUNDLE_REPORT=1 pnpm build) to the gate's own file-to-surface
// attribution (`bundle-budget.mjs --json --attribution`), and estimates each module's gzip bytes as
// its rendered share of its chunk's gzip size. A module that lands in two chunks is listed once per
// chunk with the chunk named, never summed. Per surface, the estimates add up to the gate's total
// (a chunk with no rows is listed as one `(chunk without module rows)` entry, so nothing is lost).
//
//   node scripts/bundle-report.mjs --surface performance --top 25 [--json] [--root .] [--report <json>]
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const GATE = fileURLToPath(new URL('./bundle-budget.mjs', import.meta.url));

/** Estimate per-module gzip bytes for every attributed file and return the rows for one surface. */
export function surfaceModules(files, rows, surface) {
	const byChunk = new Map();
	for (const row of rows) {
		if (!byChunk.has(row.chunk)) byChunk.set(row.chunk, []);
		byChunk.get(row.chunk).push(row);
	}
	const out = [];
	for (const file of files.filter((f) => f.surface === surface)) {
		const modules = byChunk.get(file.file) ?? [];
		const rendered = modules.reduce((sum, m) => sum + m.rendered_bytes, 0);
		if (rendered === 0) {
			out.push({ chunk: file.file, module: '(chunk without module rows)', gzip_bytes: file.bytes });
			continue;
		}
		for (const m of modules) {
			out.push({
				chunk: file.file,
				module: m.module,
				gzip_bytes: (file.bytes * m.rendered_bytes) / rendered
			});
		}
	}
	return out.sort((a, b) => b.gzip_bytes - a.gzip_bytes || a.module.localeCompare(b.module));
}

function _value(args, flag, fallback) {
	const index = args.indexOf(flag);
	if (index < 0) return fallback;
	const value = args[index + 1];
	if (value === undefined || value.startsWith('--')) {
		console.error(`[ERROR] ${flag} needs a value`);
		process.exit(1);
	}
	return value;
}

/** CLI entry: measure through the gate, join the module report, print the top N. */
function main() {
	const args = process.argv.slice(2);
	const root = _value(args, '--root', '.');
	const surface = _value(args, '--surface', 'performance');
	const top = Number(_value(args, '--top', '25'));
	const reportPath = _value(args, '--report', join(root, '.svelte-kit/bundle-report.json'));
	const budgets = _value(args, '--budgets', null);
	let rows;
	try {
		rows = JSON.parse(readFileSync(reportPath, 'utf8'));
	} catch (error) {
		console.error(
			`[ERROR] ${reportPath}: ${error.message}. Run \`BUNDLE_REPORT=1 pnpm build\` first.`
		);
		process.exit(1);
	}
	// The gate prints its verdict after the JSON, so read the measurement from its --measure file.
	const scratch = mkdtempSync(join(tmpdir(), 'bundle-report-'));
	const measurePath = join(scratch, 'measurement.json');
	const gateArgs = [
		GATE,
		'--root',
		root,
		'--json',
		'--attribution',
		'--report-only',
		'--measure',
		measurePath
	];
	if (budgets) gateArgs.push('--budgets', budgets);
	let measurement;
	try {
		execFileSync(process.execPath, gateArgs, { stdio: ['ignore', 'ignore', 'inherit'] });
		measurement = JSON.parse(readFileSync(measurePath, 'utf8'));
	} finally {
		rmSync(scratch, { recursive: true, force: true });
	}
	if (!(surface in measurement.totals)) {
		console.error(
			`[ERROR] no surface "${surface}"; known: ${Object.keys(measurement.totals).join(', ')}`
		);
		process.exit(1);
	}
	const modules = surfaceModules(measurement.files, rows, surface);
	const total = measurement.totals[surface].bytes;
	const listed = modules.slice(0, top).map((m) => ({ ...m, gzip_bytes: Math.round(m.gzip_bytes) }));
	if (args.includes('--json')) {
		console.log(JSON.stringify({ surface, total_gzip_bytes: total, modules: listed }, null, 2));
		return;
	}
	console.log(
		`${surface}: ${total} bytes gzip; top ${listed.length} modules by estimated gzip bytes:`
	);
	for (const m of listed)
		console.log(`  ${String(m.gzip_bytes).padStart(8)}  ${m.module}  [${m.chunk}]`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
