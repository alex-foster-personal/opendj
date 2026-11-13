// unit-test-seam-check.mjs -- verify every *ForTests seam called from
// tests/unit/ exists as a callable export on the resolved production module.
//
// Each call's callee is resolved through the TypeScript binder, so a name is
// looked up in the lexical scope of the call rather than by its text across
// the file; a binding reassigned to several modules must export the seam on
// every one. A call the guard cannot follow to a module, or to something the
// test file built itself, fails as unverifiable instead of being dropped.
//
// A call is a seam call when its callee is spelled `...ForTests` OR when the
// callee resolves to a `...ForTests` member under another local name (an
// aliased import or destructuring, `const reset = mod._xForTests`), so a
// rename cannot hide a missing export. Values may also come through a
// destructuring assignment, an object literal's properties, a module path held
// in a const string, or a helper function imported from a test-side module.
// An import from a test-side module follows that module's exports, including
// named, namespace and `export *` re-exports, to the production module under
// the ORIGINAL export name; a re-export cycle fails as unverifiable. A seam
// invoked as `fn.call(...)`, `fn.apply(...)` or `fn.bind(...)` is checked as
// `fn`. A callee reached through a computed member whose key is a const string
// (`const key = '_xForTests'; mod[key]()`) is also a seam call, using the
// resolved key.
//
// ENV:
//   UNIT_TEST_SEAM_EXTRA - comma-separated extra test file paths (repo-relative
//   or absolute) for hermetic regression without checking out an old commit.
//
// USAGE:
//   node scripts/unit-test-seam-check.mjs
//
// Prints JSON {filesScanned, seamsChecked, localSeamCalls} to stdout on success;
// exits 1 with one line per violation on stderr.

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

import { importBundledSource } from '../tests/unit/import-bundled-source.mjs';
import { bundleTypeScriptModule } from '../tests/unit/load-typescript.mjs';

const SCRIPT_DIR = fileURLToPath(new URL('.', import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, '..');
const TESTS_UNIT = join(FRONTEND_ROOT, 'tests/unit');
const FOR_TESTS_RE = /ForTests$/;

function _fail(message) {
	console.error(`[ERROR] ${message}`);
	process.exit(1);
}

const MODULE_SUFFIXES = ['', '.ts', '.js', '.mjs', '/index.ts', '/index.js'];

/** Resolve a frontend-relative module path to an existing file the way esbuild
 * resolves an extensionless import. Returns null when no candidate exists. */
function _resolveModuleFile(relPath) {
	if (!relPath) return null;
	for (const suffix of MODULE_SUFFIXES) {
		const candidate = `${relPath}${suffix}`;
		const abs = join(FRONTEND_ROOT, candidate);
		if (existsSync(abs) && statSync(abs).isFile()) return candidate;
	}
	return null;
}

/** Frontend-relative path for an import specifier, or null for a bare package
 * (node:*, svelte, ...). Relative specifiers resolve against `fromDirAbs`. */
function _specifierToFrontendPath(spec, fromDirAbs) {
	if (typeof spec !== 'string' || !spec) return null;
	if (spec.startsWith('$lib/')) return `src/lib/${spec.slice('$lib/'.length)}`;
	if (spec.startsWith('./') || spec.startsWith('../')) {
		return relative(FRONTEND_ROOT, resolve(fromDirAbs, spec)).replace(/\\/g, '/');
	}
	return null;
}

/** The path argument of loadTypeScriptModule / ssrLoadModule, which both take
 * a frontend-relative path (ssrLoadModule with a leading slash). */
function _loaderPath(raw) {
	if (typeof raw !== 'string') return null;
	let path = raw.trim();
	while (path.startsWith('/')) path = path.slice(1);
	if (path.startsWith('./')) path = path.slice(2);
	if (path.startsWith('$lib/')) path = `src/lib/${path.slice('$lib/'.length)}`;
	return path || null;
}

function _scriptKind(filePath) {
	return /\.[cm]?ts$/.test(filePath) ? ts.ScriptKind.TS : ts.ScriptKind.JS;
}

function _parseSourceFile(filePath, text) {
	const sourceFile = ts.createSourceFile(
		filePath,
		text,
		ts.ScriptTarget.Latest,
		true,
		_scriptKind(filePath)
	);
	if (sourceFile.parseDiagnostics?.length) {
		const detail = sourceFile.parseDiagnostics
			.map((d) => ts.flattenDiagnosticMessageText(d.messageText, ' '))
			.join('; ');
		_fail(`failed to parse ${filePath}: ${detail}`);
	}
	return sourceFile;
}

/** Parse one test file and bind it with the TypeScript binder, so every
 * identifier resolves to the declaration its lexical scope actually names
 * (two `const mod` in two test callbacks are two symbols, not one name). */
function _bindSourceFile(filePath, text) {
	const sourceFile = _parseSourceFile(filePath, text);
	const options = {
		allowJs: true,
		noEmit: true,
		noLib: true,
		noResolve: true,
		types: [],
		target: ts.ScriptTarget.Latest,
		module: ts.ModuleKind.ESNext
	};
	const host = {
		getSourceFile: (name) => (name === filePath ? sourceFile : undefined),
		getDefaultLibFileName: () => 'lib.d.ts',
		writeFile: () => undefined,
		getCurrentDirectory: () => FRONTEND_ROOT,
		getCanonicalFileName: (name) => name,
		useCaseSensitiveFileNames: () => true,
		getNewLine: () => '\n',
		fileExists: (name) => name === filePath,
		readFile: () => undefined,
		getDirectories: () => []
	};
	const program = ts.createProgram({ rootNames: [filePath], options, host });
	const bound = program.getSourceFile(filePath);
	if (bound !== sourceFile) _fail(`failed to bind ${filePath}`);
	return { sourceFile, checker: program.getTypeChecker() };
}

function _unwrapExpression(node) {
	let current = node;
	while (
		ts.isParenthesizedExpression(current) ||
		ts.isAwaitExpression(current) ||
		ts.isAsExpression(current) ||
		ts.isSatisfiesExpression(current) ||
		ts.isNonNullExpression(current)
	) {
		current = current.expression;
	}
	return current;
}

function _stringLiteral(node) {
	const expr = _unwrapExpression(node);
	return ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr) ? expr.text : null;
}

function _isLoadTypeScriptModuleCall(expr) {
	if (!ts.isCallExpression(expr)) return false;
	const callee = _unwrapExpression(expr.expression);
	return (
		(ts.isIdentifier(callee) && callee.text === 'loadTypeScriptModule') ||
		(ts.isPropertyAccessExpression(callee) && callee.name.text === 'ssrLoadModule')
	);
}

/** Loaders that take an entry module's SOURCE TEXT (named re-exports) rather
 * than a path: bundleSvelteEntry, loadSvelteSsrModule and loadRuneModule. */
const ENTRY_SOURCE_LOADERS = new Set([
	'bundleSvelteEntry',
	'loadSvelteSsrModule',
	'loadRuneModule'
]);

function _isBundleSvelteEntryCall(expr) {
	if (!ts.isCallExpression(expr)) return false;
	const callee = _unwrapExpression(expr.expression);
	return ts.isIdentifier(callee) && ENTRY_SOURCE_LOADERS.has(callee.text);
}

function _isDynamicImport(expr) {
	return ts.isCallExpression(expr) && expr.expression.kind === ts.SyntaxKind.ImportKeyword;
}

/** Named re-exports of a bundleSvelteEntry source (resolved against the
 * frontend root, as esbuild's stdin resolveDir is). */
function _parseBundleReExports(source) {
	const entry = ts.createSourceFile('bundle-entry.ts', source, ts.ScriptTarget.Latest, true);
	const reExports = new Map();
	let hasStar = false;
	for (const stmt of entry.statements) {
		if (!ts.isExportDeclaration(stmt) || !stmt.moduleSpecifier) continue;
		const spec = _stringLiteral(stmt.moduleSpecifier);
		if (!stmt.exportClause || !ts.isNamedExports(stmt.exportClause)) {
			hasStar = true;
			continue;
		}
		for (const el of stmt.exportClause.elements) {
			reExports.set(el.name.text, {
				spec,
				exportName: (el.propertyName ?? el.name).text
			});
		}
	}
	return { reExports, hasStar };
}

const LOCAL = (why) => ({ kind: 'local', why });
const UNRESOLVED = (why) => ({ kind: 'unresolved', why });
const ASSIGNMENT_OPERATORS = new Set([
	ts.SyntaxKind.EqualsToken,
	ts.SyntaxKind.QuestionQuestionEqualsToken,
	ts.SyntaxKind.BarBarEqualsToken
]);

function _moduleOutcome(rel, chain, why) {
	const file = _resolveModuleFile(rel);
	if (!file) return UNRESOLVED(`${why}: no module file at ${rel}`);
	return { kind: 'module', path: file, chain };
}

function _outcomeKey(outcome) {
	if (outcome.kind === 'module') return `module:${outcome.path}:${outcome.chain.join('.')}`;
	if (outcome.kind === 'bundle') return `bundle:${outcome.id}`;
	if (outcome.kind === 'object') return `object:${outcome.resolver.fileAbs}:${outcome.node.pos}`;
	if (outcome.kind === 'helper') return `helper:${outcome.resolver.fileAbs}`;
	return `${outcome.kind}:${outcome.why}:${outcome.lastKey ?? ''}`;
}

/** The member name an outcome was last reached through, if any: the tail of a
 * module's export chain, or the key walked off a value the guard could not
 * follow. A `...ForTests` tail makes the call a seam call whatever its callee
 * is spelled. */
function _lastKey(outcome) {
	if (outcome.kind === 'module') return outcome.chain.at(-1) ?? null;
	return outcome.lastKey ?? null;
}

/** Test-side modules (helpers and scripts, not src/) whose exported functions
 * the guard follows into, one resolver per file. */
const HELPER_RESOLVERS = new Map();
/** Function nodes whose return values are being resolved, across files. */
const CALLS_IN_FLIGHT = new Set();
/** `file#exportName` pairs whose re-export chains are being followed. */
const REEXPORTS_IN_FLIGHT = new Set();
/** Test-side files the guard parses to follow their exports. */
const HELPER_SCRIPT_RE = /\.[cm]?[jt]s$/;

function _dedupe(outcomes) {
	const seen = new Map();
	for (const outcome of outcomes) {
		const key = _outcomeKey(outcome);
		if (!seen.has(key)) seen.set(key, outcome);
	}
	return [...seen.values()];
}

/** Per `path#name`: whether a production module statically exports `name`
 * (its own declarations, named re-exports, or a star that supplies it). */
const PRODUCTION_EXPORTS = new Map();
const PRODUCTION_RESOLVERS = new Map();

/** true or false when a parseable production module (.ts/.js/.mjs) statically
 * does or does not export `name`; null when the guard cannot read it, so the
 * caller keeps the candidate and the runtime check decides. */
function _productionModuleExports(path, name) {
	if (!HELPER_SCRIPT_RE.test(path)) return null;
	const key = `${path}#${name}`;
	if (PRODUCTION_EXPORTS.has(key)) return PRODUCTION_EXPORTS.get(key);
	PRODUCTION_EXPORTS.set(key, null); // a star cycle reads as unknown, never as absent
	const abs = join(FRONTEND_ROOT, path);
	if (!PRODUCTION_RESOLVERS.has(abs))
		PRODUCTION_RESOLVERS.set(abs, new SeamResolver(abs, readFileSync(abs, 'utf8')));
	const exported = PRODUCTION_RESOLVERS.get(abs)._exportOutcomes(name) !== null;
	PRODUCTION_EXPORTS.set(key, exported);
	return exported;
}

/**
 * Resolves the value an expression in one test file can hold to where it came
 * from: a module the test loads (plus the export chain walked off it), a
 * bundleSvelteEntry bundle, something the test file built itself (`local`),
 * or `unresolved` when the guard cannot follow it. Resolution is by binder
 * symbol, so lexical scope is respected; a symbol assigned more than once
 * yields every candidate, and the caller must verify the seam on all of them.
 */
class SeamResolver {
	constructor(fileAbs, text) {
		this.fileAbs = fileAbs;
		this.fileDir = dirname(fileAbs);
		const { sourceFile, checker } = _bindSourceFile(fileAbs, text);
		this.sourceFile = sourceFile;
		this.checker = checker;
		/** @type {Map<ts.Symbol, Array<ts.Expression | {destructure: ts.Expression, keys: string[]} | {unfollowable: string}>>} */
		this.assignments = new Map();
		this.inFlight = new Set();
		this.bundleIds = new Map();
		this._collectAssignments();
	}

	_symbolOf(node) {
		return this.checker.getSymbolAtLocation(node) ?? null;
	}

	_recordSymbolAssignment(sym, value) {
		if (!sym) return;
		if (!this.assignments.has(sym)) this.assignments.set(sym, []);
		this.assignments.get(sym).push(value);
	}

	_recordAssignment(identifier, value) {
		this._recordSymbolAssignment(this._symbolOf(identifier), value);
	}

	/** Record every target of a destructuring ASSIGNMENT (`({ a, b: c } = x)`)
	 * as the member of `source` its key path names. `keys` is null once the
	 * path stops being followable (array positions, rest, computed keys). */
	_recordPatternTargets(node, source, keys, why) {
		const target = (k) =>
			keys == null ? { unfollowable: why } : { destructure: source, keys: [...keys, k] };
		if (ts.isIdentifier(node)) {
			this._recordAssignment(
				node,
				keys == null ? { unfollowable: why } : { destructure: source, keys }
			);
			return;
		}
		if (ts.isShorthandPropertyAssignment(node)) {
			// getSymbolAtLocation(node.name) answers the object literal's PROPERTY,
			// not the variable being assigned; the value symbol is the variable.
			const sym = this.checker.getShorthandAssignmentValueSymbol(node) ?? null;
			this._recordSymbolAssignment(sym, target(node.name.text));
			if (node.objectAssignmentInitializer) {
				this._recordSymbolAssignment(sym, node.objectAssignmentInitializer);
			}
			return;
		}
		if (ts.isPropertyAssignment(node)) {
			const name = node.name;
			const key = ts.isIdentifier(name) || ts.isStringLiteral(name) ? name.text : null;
			this._recordPatternTargets(
				node.initializer,
				source,
				key == null || keys == null ? null : [...keys, key],
				key == null ? 'computed destructuring key' : why
			);
			return;
		}
		if (ts.isSpreadElement(node) || ts.isSpreadAssignment(node)) {
			this._recordPatternTargets(
				node.expression,
				source,
				null,
				'rest element of a destructuring assignment'
			);
			return;
		}
		if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
			this._recordPatternTargets(node.left, source, keys, why);
			const left = _unwrapExpression(node.left);
			if (ts.isIdentifier(left)) this._recordAssignment(left, node.right);
			return;
		}
		if (ts.isObjectLiteralExpression(node)) {
			for (const prop of node.properties) this._recordPatternTargets(prop, source, keys, why);
			return;
		}
		if (ts.isArrayLiteralExpression(node)) {
			for (const el of node.elements) {
				this._recordPatternTargets(el, source, null, 'array destructuring assignment');
			}
		}
	}

	_collectAssignments() {
		const visit = (node) => {
			if (ts.isBinaryExpression(node) && ASSIGNMENT_OPERATORS.has(node.operatorToken.kind)) {
				const left = _unwrapExpression(node.left);
				if (ts.isIdentifier(left)) {
					this._recordAssignment(left, node.right);
				} else if (
					node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
					(ts.isObjectLiteralExpression(left) || ts.isArrayLiteralExpression(left))
				) {
					this._recordPatternTargets(left, node.right, [], null);
				}
			}
			ts.forEachChild(node, visit);
		};
		visit(this.sourceFile);
	}

	/** Every outcome reached by reading member `name` off `outcome`. */
	_member(outcome, name) {
		if (outcome.kind === 'module') {
			return [{ ...outcome, chain: [...outcome.chain, name] }];
		}
		if (outcome.kind === 'bundle') {
			const reexport = outcome.reExports.get(name);
			if (!reexport) {
				return [
					UNRESOLVED(
						outcome.hasStar
							? `bundle entry re-exports \`${name}\` only through export *, which the guard does not follow`
							: `bundle entry does not re-export \`${name}\``
					)
				];
			}
			const rel = _specifierToFrontendPath(reexport.spec, FRONTEND_ROOT);
			if (!rel)
				return [UNRESOLVED(`bundle entry re-exports \`${name}\` from package ${reexport.spec}`)];
			return [_moduleOutcome(rel, [reexport.exportName], `bundle re-export \`${name}\``)];
		}
		if (outcome.kind === 'object') return outcome.resolver._objectMember(outcome.node, name);
		if (outcome.kind === 'helper') {
			const rel = relative(FRONTEND_ROOT, outcome.resolver.fileAbs).replace(/\\/g, '/');
			return (
				outcome.resolver._exportOutcomes(name) ?? [
					{ ...UNRESOLVED(`test-side module ${rel} does not export \`${name}\``), lastKey: name }
				]
			);
		}
		return [{ ...outcome, lastKey: name }];
	}

	/** Property `name` of an object literal built in this file. */
	_objectMember(node, name) {
		const out = [];
		let spread = false;
		for (const prop of node.properties) {
			if (ts.isSpreadAssignment(prop)) {
				spread = true;
				continue;
			}
			const propName = prop.name;
			const key =
				propName &&
				(ts.isIdentifier(propName) || ts.isStringLiteral(propName) || ts.isNumericLiteral(propName))
					? propName.text
					: null;
			if (key !== name) continue;
			if (ts.isPropertyAssignment(prop)) out.push(...this.resolveValue(prop.initializer));
			else if (ts.isShorthandPropertyAssignment(prop)) {
				const sym = this.checker.getShorthandAssignmentValueSymbol(prop);
				out.push(
					...(sym
						? this._resolveSymbol(sym)
						: [UNRESOLVED(`\`${name}\` has no declaration in this file`)])
				);
			} else out.push(LOCAL('method built in the test file'));
		}
		if (out.length > 0) return out;
		if (spread) {
			for (const prop of node.properties) {
				if (!ts.isSpreadAssignment(prop)) continue;
				for (const base of this.resolveValue(prop.expression))
					out.push(...this._member(base, name));
			}
			return out;
		}
		return [{ ...UNRESOLVED(`object literal has no property \`${name}\``), lastKey: name }];
	}

	/** The string a module-path or entry-source argument evaluates to, when it is
	 * a literal or built from literals through consts, `+`, templates and
	 * `[...].join(sep)`. Null when it is not statically known. */
	_constString(node, depth = 0) {
		if (depth > 20) return null;
		const expr = _unwrapExpression(node);
		const literal = _stringLiteral(expr);
		if (literal != null) return literal;
		if (ts.isTemplateExpression(expr)) {
			let text = expr.head.text;
			for (const span of expr.templateSpans) {
				const part = this._constString(span.expression, depth + 1);
				if (part == null) return null;
				text += part + span.literal.text;
			}
			return text;
		}
		if (ts.isBinaryExpression(expr) && expr.operatorToken.kind === ts.SyntaxKind.PlusToken) {
			const left = this._constString(expr.left, depth + 1);
			const right = left == null ? null : this._constString(expr.right, depth + 1);
			return right == null ? null : left + right;
		}
		if (ts.isIdentifier(expr)) {
			const sym = this._symbolOf(expr);
			const decls = sym?.declarations ?? [];
			if (decls.length !== 1 || (this.assignments.get(sym) ?? []).length > 0) return null;
			const decl = decls[0];
			if (
				!ts.isVariableDeclaration(decl) ||
				!decl.initializer ||
				!(ts.getCombinedNodeFlags(decl) & ts.NodeFlags.Const)
			) {
				return null;
			}
			return this._constString(decl.initializer, depth + 1);
		}
		if (
			ts.isCallExpression(expr) &&
			ts.isPropertyAccessExpression(expr.expression) &&
			expr.expression.name.text === 'join' &&
			ts.isArrayLiteralExpression(_unwrapExpression(expr.expression.expression))
		) {
			const sep =
				expr.arguments.length === 0 ? ',' : this._constString(expr.arguments[0], depth + 1);
			if (sep == null) return null;
			const parts = [];
			for (const el of _unwrapExpression(expr.expression.expression).elements) {
				const part = this._constString(el, depth + 1);
				if (part == null) return null;
				parts.push(part);
			}
			return parts.join(sep);
		}
		return null;
	}

	_loaderOutcome(call) {
		const raw = call.arguments.length > 0 ? this._constString(call.arguments[0]) : null;
		const rel = _loaderPath(raw);
		if (!rel) return UNRESOLVED('module loader path is not a statically known string');
		return _moduleOutcome(rel, [], 'loaded module');
	}

	_bundleOutcome(call) {
		const source = call.arguments.length > 0 ? this._constString(call.arguments[0]) : null;
		if (source == null) return UNRESOLVED('entry module source is not a statically known string');
		if (!this.bundleIds.has(call)) this.bundleIds.set(call, this.bundleIds.size);
		return { kind: 'bundle', id: this.bundleIds.get(call), ..._parseBundleReExports(source) };
	}

	/** The namespace object of the module `spec` names, seen from this file: a
	 * production module, a test-side module whose exports the guard follows
	 * (`helper`), or `local` for a package (node:test, svelte, ...). */
	_namespaceOutcome(spec, why) {
		const rel = _specifierToFrontendPath(spec, this.fileDir);
		if (!rel) return LOCAL(`imported from package ${spec}`);
		if (rel.startsWith('src/')) return _moduleOutcome(rel, [], why);
		const helper = this._helperResolver(spec);
		if (!helper)
			return UNRESOLVED(`${why}: test-side module ${spec} is not a script the guard can follow`);
		return {
			kind: 'helper',
			resolver: helper,
			why: `${why}: a test-side module namespace, not a callable`
		};
	}

	_importOutcome(importDecl, chain) {
		const spec = _stringLiteral(importDecl.moduleSpecifier);
		let outcomes = [this._namespaceOutcome(spec, `import from ${spec}`)];
		for (const key of chain) outcomes = outcomes.flatMap((o) => this._member(o, key));
		return outcomes;
	}

	/** Every outcome export `name` of this test-side module resolves to, or
	 * null when the module statically does not export it. A re-export
	 * (`export { a as name } from`, `export * from`, `export * as name from`)
	 * is followed to the module it names under the ORIGINAL export name, so a
	 * production seam re-exported through a helper is still checked on the
	 * production module. A re-export cycle is unverifiable, never dropped. */
	_exportOutcomes(name) {
		const key = `${this.fileAbs}#${name}`;
		if (REEXPORTS_IN_FLIGHT.has(key)) {
			const rel = relative(FRONTEND_ROOT, this.fileAbs).replace(/\\/g, '/');
			return [{ ...UNRESOLVED(`re-export cycle through ${rel} for \`${name}\``), lastKey: name }];
		}
		REEXPORTS_IN_FLIGHT.add(key);
		try {
			return this._exportOutcomesUncached(name);
		} finally {
			REEXPORTS_IN_FLIGHT.delete(key);
		}
	}

	_exportOutcomesUncached(name) {
		const starSpecs = [];
		for (const stmt of this.sourceFile.statements) {
			if (ts.isExportDeclaration(stmt)) {
				if (stmt.isTypeOnly) continue;
				const spec = stmt.moduleSpecifier ? _stringLiteral(stmt.moduleSpecifier) : null;
				if (stmt.moduleSpecifier && spec == null) {
					return [UNRESOLVED(`re-export source of \`${name}\` is not a string literal`)];
				}
				if (!stmt.exportClause) {
					starSpecs.push(spec);
					continue;
				}
				if (ts.isNamespaceExport(stmt.exportClause)) {
					if (stmt.exportClause.name.text === name) {
						return [this._namespaceOutcome(spec, `export * as ${name} from ${spec}`)];
					}
					continue;
				}
				for (const el of stmt.exportClause.elements) {
					if (el.isTypeOnly || el.name.text !== name) continue;
					const original = (el.propertyName ?? el.name).text;
					if (spec != null) {
						return this._member(this._namespaceOutcome(spec, `re-export from ${spec}`), original);
					}
					const sym = this.checker.getExportSpecifierLocalTargetSymbol(el);
					return sym
						? this._resolveSymbol(sym)
						: [UNRESOLVED(`exported \`${original}\` has no declaration in this file`)];
				}
				continue;
			}
			if (ts.isExportAssignment(stmt)) {
				if (name === 'default' && !stmt.isExportEquals) return this.resolveValue(stmt.expression);
				continue;
			}
			const flags = ts.canHaveModifiers(stmt) ? ts.getCombinedModifierFlags(stmt) : 0;
			if (!(flags & ts.ModifierFlags.Export)) continue;
			const isDefault = Boolean(flags & ts.ModifierFlags.Default);
			if (ts.isFunctionDeclaration(stmt) || ts.isClassDeclaration(stmt)) {
				const exportedAs = isDefault ? 'default' : stmt.name?.text;
				if (exportedAs === name) return [LOCAL('declared in a test-side module')];
				continue;
			}
			if (ts.isVariableStatement(stmt)) {
				for (const decl of stmt.declarationList.declarations) {
					const hit = this._bindingNamed(decl.name, name);
					if (hit) {
						const sym = this._symbolOf(hit);
						return sym
							? this._resolveSymbol(sym)
							: [UNRESOLVED(`exported \`${name}\` has no symbol`)];
					}
				}
			}
		}
		// Explicit exports shadow `export *`; a name several stars could supply
		// is ambiguous (and not exported at all at runtime), so it is not guessed.
		// Only a star whose source statically exports `name` is a candidate, as in
		// ESM: a star over a production module that lacks it supplies nothing.
		const candidates = [];
		const absent = [];
		for (const spec of starSpecs) {
			const ns = this._namespaceOutcome(spec, `export * from ${spec}`);
			if (ns.kind === 'helper') {
				const found = ns.resolver._exportOutcomes(name);
				if (found) candidates.push(found);
			} else if (ns.kind === 'module' && _productionModuleExports(ns.path, name) === false) {
				absent.push(this._member(ns, name));
			} else {
				candidates.push(this._member(ns, name));
			}
		}
		// No star supplies it: report it missing on every production module a
		// star names, so the runtime check names the export, not the barrel.
		if (candidates.length === 0 && absent.length > 0) return absent.flat();
		if (candidates.length === 1) return candidates[0];
		if (candidates.length > 1) {
			return [
				{
					...UNRESOLVED(
						`\`${name}\` may come from ${candidates.length} export * sources; the guard cannot tell which`
					),
					lastKey: name
				}
			];
		}
		return null;
	}

	/** The identifier inside a declaration's binding name that binds `name`. */
	_bindingNamed(bindingName, name) {
		if (ts.isIdentifier(bindingName)) return bindingName.text === name ? bindingName : null;
		for (const el of bindingName.elements) {
			if (ts.isOmittedExpression(el)) continue;
			const hit = this._bindingNamed(el.name, name);
			if (hit) return hit;
		}
		return null;
	}

	_returnExpressions(fn) {
		if (!fn.body) return [];
		if (!ts.isBlock(fn.body)) return [fn.body];
		const found = [];
		const visit = (node) => {
			if (ts.isFunctionLike(node) || ts.isClassLike(node)) return;
			if (ts.isReturnStatement(node) && node.expression) found.push(node.expression);
			ts.forEachChild(node, visit);
		};
		ts.forEachChild(fn.body, visit);
		return found;
	}

	_localFunctions(sym) {
		const fns = [];
		for (const decl of sym.declarations ?? []) {
			if (ts.isFunctionDeclaration(decl)) fns.push(decl);
			else if (
				ts.isVariableDeclaration(decl) &&
				decl.initializer &&
				(ts.isArrowFunction(_unwrapExpression(decl.initializer)) ||
					ts.isFunctionExpression(_unwrapExpression(decl.initializer)))
			) {
				fns.push(_unwrapExpression(decl.initializer));
			}
		}
		return fns;
	}

	/** Return values of `fns`, each resolved in the file that declares it. */
	_resolveReturns(fns, resolver, label) {
		const out = [];
		for (const fn of fns) {
			if (CALLS_IN_FLIGHT.has(fn)) continue;
			CALLS_IN_FLIGHT.add(fn);
			try {
				for (const expr of resolver._returnExpressions(fn))
					out.push(...resolver.resolveValue(expr));
			} finally {
				CALLS_IN_FLIGHT.delete(fn);
			}
		}
		return out.length > 0 ? out : [UNRESOLVED(`${label}() returns nothing the guard can follow`)];
	}

	/** The resolver for a test-side module (helper or script, never src/) an
	 * import names, or null when it is a package or a production module. */
	_helperResolver(spec) {
		const rel = _specifierToFrontendPath(spec, this.fileDir);
		if (!rel || rel.startsWith('src/') || rel.startsWith('..')) return null;
		const file = _resolveModuleFile(rel);
		if (!file || !HELPER_SCRIPT_RE.test(file)) return null;
		const abs = join(FRONTEND_ROOT, file);
		if (!HELPER_RESOLVERS.has(abs)) {
			HELPER_RESOLVERS.set(abs, new SeamResolver(abs, readFileSync(abs, 'utf8')));
		}
		return HELPER_RESOLVERS.get(abs);
	}

	/** Functions a test-side module exports under `name`. */
	_exportedFunctions(name) {
		const fns = [];
		for (const stmt of this.sourceFile.statements) {
			const exported = ts.getCombinedModifierFlags(stmt) & ts.ModifierFlags.Export;
			if (!exported) continue;
			if (ts.isFunctionDeclaration(stmt) && stmt.name?.text === name && stmt.body) fns.push(stmt);
			if (ts.isVariableStatement(stmt)) {
				for (const decl of stmt.declarationList.declarations) {
					if (!ts.isIdentifier(decl.name) || decl.name.text !== name) continue;
					const sym = this._symbolOf(decl.name);
					if (sym) fns.push(...this._localFunctions(sym));
				}
			}
		}
		return fns;
	}

	_resolveCallResult(call) {
		const callee = _unwrapExpression(call.expression);
		const label = callee.getText(this.sourceFile);
		if (ts.isIdentifier(callee)) {
			const sym = this._symbolOf(callee);
			const fns = sym ? this._localFunctions(sym) : [];
			if (fns.length > 0 && (this.assignments.get(sym) ?? []).length === 0) {
				return this._resolveReturns(fns, this, label);
			}
			const decl = sym?.declarations?.length === 1 ? sym.declarations[0] : null;
			if (decl && ts.isImportSpecifier(decl) && (this.assignments.get(sym) ?? []).length === 0) {
				const spec = _stringLiteral(decl.parent.parent.parent.moduleSpecifier);
				const helper = this._helperResolver(spec);
				if (helper) {
					const helperFns = helper._exportedFunctions((decl.propertyName ?? decl.name).text);
					if (helperFns.length > 0) return this._resolveReturns(helperFns, helper, label);
				}
			}
		}
		return [UNRESOLVED(`return value of ${label}()`)];
	}

	_resolveBindingElement(el) {
		const pattern = el.parent;
		if (!ts.isObjectBindingPattern(pattern)) {
			return [UNRESOLVED('array destructuring')];
		}
		let key;
		if (el.dotDotDotToken) key = null;
		else {
			const nameNode = el.propertyName ?? el.name;
			if (ts.isIdentifier(nameNode) || ts.isStringLiteral(nameNode)) key = nameNode.text;
			else return [UNRESOLVED('computed destructuring key')];
		}
		const holder = pattern.parent;
		let base;
		if (ts.isVariableDeclaration(holder)) {
			base = holder.initializer
				? this.resolveValue(holder.initializer)
				: [UNRESOLVED('destructuring without an initializer')];
		} else if (ts.isBindingElement(holder)) {
			base = this._resolveBindingElement(holder);
		} else {
			return [UNRESOLVED('destructured parameter')];
		}
		const out = key == null ? base : base.flatMap((outcome) => this._member(outcome, key));
		if (el.initializer) out.push(...this.resolveValue(el.initializer));
		return out;
	}

	_resolveDeclaration(decl) {
		if (ts.isVariableDeclaration(decl)) {
			const statement = decl.parent?.parent;
			if (statement && (ts.isForOfStatement(statement) || ts.isForInStatement(statement))) {
				return [UNRESOLVED('loop variable')];
			}
			return decl.initializer ? this.resolveValue(decl.initializer) : [];
		}
		if (ts.isBindingElement(decl)) return this._resolveBindingElement(decl);
		if (ts.isImportSpecifier(decl)) {
			const name = (decl.propertyName ?? decl.name).text;
			return this._importOutcome(decl.parent.parent.parent, [name]);
		}
		if (ts.isImportClause(decl)) return this._importOutcome(decl.parent, ['default']);
		if (ts.isNamespaceImport(decl)) return this._importOutcome(decl.parent.parent, []);
		if (
			ts.isFunctionDeclaration(decl) ||
			ts.isClassDeclaration(decl) ||
			ts.isMethodDeclaration(decl)
		) {
			return [LOCAL('declared in the test file')];
		}
		if (ts.isParameter(decl)) return [UNRESOLVED('function parameter')];
		return [UNRESOLVED(`${ts.SyntaxKind[decl.kind]} binding`)];
	}

	_resolveSymbol(sym) {
		if (this.inFlight.has(sym)) return [];
		this.inFlight.add(sym);
		try {
			const out = [];
			for (const decl of sym.declarations ?? []) out.push(...this._resolveDeclaration(decl));
			for (const value of this.assignments.get(sym) ?? []) {
				if (value.unfollowable) {
					out.push(UNRESOLVED(`\`${sym.name}\` is assigned by ${value.unfollowable}`));
				} else if (value.destructure) {
					let current = this.resolveValue(value.destructure);
					for (const key of value.keys) current = current.flatMap((o) => this._member(o, key));
					out.push(...current);
				} else {
					out.push(...this.resolveValue(value));
				}
			}
			if (out.length === 0) {
				out.push(UNRESOLVED(`\`${sym.name}\` is never assigned a value the guard can follow`));
			}
			return out;
		} finally {
			this.inFlight.delete(sym);
		}
	}

	resolveValue(node) {
		const expr = _unwrapExpression(node);
		if (_isLoadTypeScriptModuleCall(expr)) return [this._loaderOutcome(expr)];
		if (_isBundleSvelteEntryCall(expr)) return [this._bundleOutcome(expr)];
		if (_isDynamicImport(expr)) {
			const spec = expr.arguments.length > 0 ? this._constString(expr.arguments[0]) : null;
			if (spec == null) return [UNRESOLVED('import() specifier is not a statically known string')];
			return [this._namespaceOutcome(spec, `import(${spec})`)];
		}
		if (ts.isIdentifier(expr)) {
			const sym = this._symbolOf(expr);
			if (!sym) return [UNRESOLVED(`\`${expr.text}\` has no declaration in this file`)];
			return this._resolveSymbol(sym);
		}
		if (ts.isPropertyAccessExpression(expr)) {
			return this.resolveValue(expr.expression).flatMap((o) => this._member(o, expr.name.text));
		}
		if (ts.isElementAccessExpression(expr)) {
			const key = this._constString(expr.argumentExpression);
			if (key == null) return [UNRESOLVED('computed member access')];
			return this.resolveValue(expr.expression).flatMap((o) => this._member(o, key));
		}
		if (ts.isCallExpression(expr)) return this._resolveCallResult(expr);
		if (ts.isConditionalExpression(expr)) {
			return [...this.resolveValue(expr.whenTrue), ...this.resolveValue(expr.whenFalse)];
		}
		if (ts.isBinaryExpression(expr)) {
			const op = expr.operatorToken.kind;
			if (
				op === ts.SyntaxKind.QuestionQuestionToken ||
				op === ts.SyntaxKind.BarBarToken ||
				op === ts.SyntaxKind.QuestionQuestionEqualsToken ||
				op === ts.SyntaxKind.BarBarEqualsToken
			) {
				return [...this.resolveValue(expr.left), ...this.resolveValue(expr.right)];
			}
			if (op === ts.SyntaxKind.EqualsToken) return this.resolveValue(expr.right);
		}
		if (ts.isObjectLiteralExpression(expr)) return [{ kind: 'object', node: expr, resolver: this }];
		if (
			ts.isArrayLiteralExpression(expr) ||
			ts.isArrowFunction(expr) ||
			ts.isFunctionExpression(expr) ||
			ts.isClassExpression(expr)
		) {
			return [LOCAL('value built in the test file')];
		}
		return [UNRESOLVED(`unsupported expression (${ts.SyntaxKind[expr.kind]})`)];
	}
}

/** Function.prototype members that invoke (or bind) the function they are
 * read off, so `mod._xForTests.call(mod)` reads `_xForTests` first. */
const INVOKE_MEMBERS = new Set(['call', 'apply', 'bind']);

/** The function a call actually reads: the callee itself, or for
 * `fn.call(...)` / `fn.apply(...)` / `fn.bind(...)` the `fn` before them. */
function _invokedFunction(callee) {
	let target = callee;
	for (;;) {
		const member = ts.isPropertyAccessExpression(target)
			? target.name.text
			: ts.isElementAccessExpression(target)
				? _stringLiteral(target.argumentExpression)
				: null;
		if (member == null || !INVOKE_MEMBERS.has(member)) return target;
		target = _unwrapExpression(target.expression);
	}
}

function _seamNameOfCallee(callee) {
	if (ts.isIdentifier(callee)) return callee.text;
	if (ts.isPropertyAccessExpression(callee)) return callee.name.text;
	if (ts.isElementAccessExpression(callee)) return _stringLiteral(callee.argumentExpression);
	return null;
}

/** Every *ForTests call in one test file, each with the dedupe'd set of
 * places its callee can come from. Nothing is dropped: a call the guard
 * cannot follow carries an `unresolved` outcome and becomes a violation. */
function _collectSeamCalls(fileAbs, text, testFileRel) {
	const resolver = new SeamResolver(fileAbs, text);
	const { sourceFile } = resolver;
	const calls = [];
	const visit = (node) => {
		if (ts.isCallExpression(node)) {
			const callee = _unwrapExpression(node.expression);
			const invoked = _invokedFunction(callee);
			const spelled = _seamNameOfCallee(invoked);
			const outcomes = _dedupe(resolver.resolveValue(invoked));
			// An alias (`import { _xForTests as reset }`, `const { _xForTests:
			// reset } = mod`) is still a seam call: the member it resolves
			// through names the seam even though the callee does not. A
			// computed callee (`const key = '_xForTests'; mod[key]()`) has no
			// spelled name; `_lastKey` names the seam once the key is resolved.
			const aliased = outcomes.map(_lastKey).find((key) => key && FOR_TESTS_RE.test(key));
			const seamName = spelled && FOR_TESTS_RE.test(spelled) ? spelled : aliased;
			if (seamName) {
				const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
				calls.push({
					seamName,
					callee: callee.getText(sourceFile),
					outcomes,
					testFile: testFileRel,
					line: line + 1
				});
			}
		}
		ts.forEachChild(node, visit);
	};
	visit(sourceFile);
	return calls;
}

function _listTestFiles() {
	const files = [];
	const walk = (dir) => {
		if (!existsSync(dir)) return;
		for (const entry of readdirSync(dir)) {
			const full = join(dir, entry);
			if (statSync(full).isDirectory()) {
				if (entry === 'fixtures' || entry === '.tmp-seam-guard' || entry.startsWith('.')) {
					continue;
				}
				walk(full);
				continue;
			}
			if (entry.endsWith('.test.mjs')) files.push(full);
		}
	};
	walk(TESTS_UNIT);
	walk(join(TESTS_UNIT, 'slow'));

	const extra = process.env.UNIT_TEST_SEAM_EXTRA?.trim();
	if (extra) {
		for (const part of extra.split(',')) {
			const trimmed = part.trim();
			if (!trimmed) continue;
			const abs = resolve(FRONTEND_ROOT, trimmed);
			if (!existsSync(abs)) {
				_fail(`UNIT_TEST_SEAM_EXTRA path does not exist: ${trimmed}`);
			}
			files.push(abs);
		}
	}
	return [...new Set(files)].sort();
}

async function _loadBundledModule(relativePath, cache) {
	if (cache.has(relativePath)) return cache.get(relativePath);
	const text = await bundleTypeScriptModule(relativePath);
	globalThis.__musicDjToolsTestState = (value) => value;
	// Same rune shim as tests/unit/load-typescript.mjs: a bundled module that calls
	// $state.raw dies on `.raw is not a function` without it.
	globalThis.__musicDjToolsTestState.raw = (value) => value;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	globalThis.__musicDjToolsTestDerived = (value) => value;
	globalThis.__musicDjToolsTestDerived.by = (fn) => (typeof fn === 'function' ? fn() : fn);
	// A temp-file import, not a data: URL: V8 names every stack frame after the
	// module URL, so a data: URL repeats the whole bundle per frame in CI logs.
	const mod = await importBundledSource(text, relativePath);
	cache.set(relativePath, mod);
	return mod;
}

function _walkExportChain(mod, chain) {
	let target = mod;
	for (const key of chain) {
		if (target == null || (typeof target !== 'object' && typeof target !== 'function')) {
			return undefined;
		}
		target = target[key];
	}
	return target;
}

async function main() {
	const testFiles = _listTestFiles();
	const violations = [];
	const bundleCache = new Map();
	let seamsChecked = 0;
	let localSeamCalls = 0;

	for (const absPath of testFiles) {
		const text = readFileSync(absPath, 'utf8');
		const testFileRel = relative(FRONTEND_ROOT, absPath).replace(/\\/g, '/');
		const seamCalls = _collectSeamCalls(absPath, text, testFileRel);

		for (const call of seamCalls) {
			const where = `(referenced from ${call.testFile}:${call.line})`;
			const modules = call.outcomes.filter((o) => o.kind === 'module');
			const unresolved = call.outcomes.filter(
				(o) => o.kind !== 'module' && o.kind !== 'local' && o.kind !== 'object'
			);
			for (const outcome of unresolved) {
				violations.push(
					`[unit-test-seam-check] unverifiable seam call ${call.callee}(): ${outcome.why ?? 'value is a bundle, not a callable'} ${where}`
				);
			}
			if (modules.length === 0) {
				if (unresolved.length === 0) localSeamCalls += 1;
				continue;
			}
			seamsChecked += 1;
			const ambiguity =
				modules.length > 1
					? `; \`${call.callee}\` can hold ${modules.length} different modules here and every one must export it`
					: '';
			for (const outcome of modules) {
				const mod = await _loadBundledModule(outcome.path, bundleCache);
				if (typeof _walkExportChain(mod, outcome.chain) !== 'function') {
					const name = _lastKey(outcome) ?? call.seamName;
					violations.push(
						`[unit-test-seam-check] missing export ${name} on ${outcome.path} as ${outcome.chain.join('.')} ${where}${ambiguity}`
					);
				}
			}
		}
	}

	if (violations.length > 0) {
		for (const line of violations) console.error(line);
		process.exit(1);
	}

	console.log(JSON.stringify({ filesScanned: testFiles.length, seamsChecked, localSeamCalls }));
}

main().catch((err) => {
	_fail(err?.stack || String(err));
});
