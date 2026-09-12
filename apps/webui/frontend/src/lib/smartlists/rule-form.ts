/**
 * Pure AST <-> form mapping for the smart-playlist rule editor.
 *
 * The rule AST shape (predicate `{field, op, value}` / logical
 * `{op: and|or|not, children}`) mirrors `apps/shared/smartlists/schema.py`
 * and is compiled by `apps/smartlists/evaluator.py`. This module has no
 * Svelte or DOM dependency so it can be unit tested directly with
 * `node --test` (see tests/unit/smartlists-rule-form.test.mjs).
 */
import {
	ALLOWED_FIELDS,
	ALLOWED_OPS_BY_FIELD,
	FIELD_TYPES,
	LOGICAL_OPS,
	isRelativeDateLiteral,
	type FieldName,
	type LogicalOp,
	type Op
} from './rule-schema';

export interface PredicateAst {
	field: FieldName;
	op: Op;
	value: unknown;
}

export interface LogicalAst {
	op: LogicalOp;
	children: RuleAst[];
}

export type RuleAst = LogicalAst | PredicateAst;

/** A rule exactly as the daemon hands it over: `SmartlistSummary.rule` is an
 * open record in the OpenAPI schema, so nothing on the HTTP boundary can hand
 * out a `RuleAst` without asserting one. `astToForm` therefore takes either,
 * which is also an honest statement of what it always did -- it never
 * validated the AST it was given. */
export type WireRule = { [key: string]: unknown };

export interface FormPredicate {
	kind: 'predicate';
	id: string;
	field: FieldName;
	op: Op;
	/** Single-value ops: =, !=, <, <=, >, >=, contains. */
	value: string;
	/** 'between' op, lower/upper bound. */
	valueLo: string;
	valueHi: string;
	/** 'in' op, one editable value per array entry. */
	valueList: string[];
}

export interface FormGroup {
	kind: 'group';
	id: string;
	op: LogicalOp;
	children: FormNode[];
}

export type FormNode = FormGroup | FormPredicate;

export interface ValidationError {
	path: string;
	message: string;
}

let idCounter = 0;

function nextId(): string {
	idCounter += 1;
	return `rule-node-${idCounter}`;
}

/** Reset the id counter. Test-only: keeps generated ids deterministic. */
export function _resetIdCounterForTests(): void {
	idCounter = 0;
}

export function isFormGroup(node: FormNode): node is FormGroup {
	return node.kind === 'group';
}

function isLogicalAst(node: RuleAst | WireRule): node is LogicalAst {
	return (LOGICAL_OPS as string[]).includes((node as LogicalAst).op) && 'children' in node;
}

function stringifyAstScalar(value: unknown): string {
	if (value && typeof value === 'object' && '$relative' in (value as Record<string, unknown>)) {
		return String((value as { $relative: unknown }).$relative);
	}
	return String(value);
}

export function createEmptyPredicate(field: FieldName = ALLOWED_FIELDS[0]): FormPredicate {
	return {
		kind: 'predicate',
		id: nextId(),
		field,
		op: ALLOWED_OPS_BY_FIELD[field][0],
		value: '',
		valueLo: '',
		valueHi: '',
		valueList: []
	};
}

export function createEmptyGroup(op: LogicalOp = 'and'): FormGroup {
	return {
		kind: 'group',
		id: nextId(),
		op,
		children: op === 'not' ? [createEmptyPredicate()] : []
	};
}

/** Change a group's operator only when the existing children remain valid.
 * Returns an explicit error instead of dropping children during a NOT change. */
export function changeGroupOp(group: FormGroup, nextOp: LogicalOp): string | null {
	if (nextOp === 'not' && group.children.length !== 1) {
		return "cannot change to 'not': NOT requires exactly one child; remove children first";
	}
	group.op = nextOp;
	return null;
}

/** Convert a validated (or freshly loaded) rule AST into an editable form tree. */
export function astToForm(rule: RuleAst | WireRule): FormNode {
	if (isLogicalAst(rule)) {
		return {
			kind: 'group',
			id: nextId(),
			op: rule.op,
			children: rule.children.map(astToForm)
		};
	}
	const predicate = rule as PredicateAst;
	const form = createEmptyPredicate(predicate.field);
	form.op = predicate.op;
	if (predicate.op === 'between') {
		const [lo, hi] = predicate.value as [unknown, unknown];
		form.valueLo = stringifyAstScalar(lo);
		form.valueHi = stringifyAstScalar(hi);
	} else if (predicate.op === 'in') {
		form.valueList = (predicate.value as unknown[]).map(stringifyAstScalar);
	} else if (predicate.op === 'missing') {
		form.value = '';
	} else {
		form.value = stringifyAstScalar(predicate.value);
	}
	return form;
}

function parseScalar(raw: string, field: FieldName, path: string): unknown {
	const fieldType = FIELD_TYPES[field];
	if (fieldType === 'string' || fieldType === 'list') {
		return raw;
	}
	const trimmed = raw.trim();
	if (trimmed === '') {
		throw new Error(`${path}: value must not be empty`);
	}
	if (fieldType === 'number') {
		const n = Number(trimmed);
		if (!Number.isFinite(n)) {
			throw new Error(`${path}: expected a number, got ${JSON.stringify(raw)}`);
		}
		return n;
	}
	if (fieldType === 'date') {
		if (isRelativeDateLiteral(trimmed)) {
			return { $relative: trimmed };
		}
		if (Number.isNaN(Date.parse(trimmed))) {
			throw new Error(
				`${path}: expected an ISO-8601 date or a relative literal like "-7d", got ${JSON.stringify(raw)}`
			);
		}
		return trimmed;
	}
	return trimmed;
}

function parseList(raw: readonly string[], field: FieldName, path: string): unknown[] {
	if (raw.length === 0) {
		throw new Error(`${path}: expected at least one value`);
	}
	return raw.map((item, i) => parseScalar(item, field, `${path}[${i}]`));
}

/** Convert an editable form tree back into a rule AST. Throws with a
 * dotted path (mirroring `SmartlistRuleError`) on the first unparseable
 * value; call `validateForm` first to collect every error at once. */
export function formToAst(form: FormNode, path = 'root'): RuleAst {
	if (form.kind === 'group') {
		if (form.children.length === 0) {
			throw new Error(`${path}.children: logical op ${JSON.stringify(form.op)} requires at least one child`);
		}
		if (form.op === 'not' && form.children.length !== 1) {
			throw new Error(`${path}.children: 'not' requires exactly one child`);
		}
		return {
			op: form.op,
			children: form.children.map((child, i) => formToAst(child, `${path}.children[${i}]`))
		};
	}
	const allowedOps = ALLOWED_OPS_BY_FIELD[form.field];
	if (!allowedOps.includes(form.op)) {
		throw new Error(
			`${path}.op: op ${JSON.stringify(form.op)} not allowed for field ${JSON.stringify(form.field)}; expected one of ${allowedOps.join(', ')}`
		);
	}
	if (form.op === 'between') {
		return {
			field: form.field,
			op: form.op,
			value: [parseScalar(form.valueLo, form.field, `${path}.value[0]`), parseScalar(form.valueHi, form.field, `${path}.value[1]`)]
		};
	}
	if (form.op === 'in') {
		return { field: form.field, op: form.op, value: parseList(form.valueList, form.field, `${path}.value`) };
	}
	if (form.op === 'missing') {
		return { field: form.field, op: form.op, value: null };
	}
	return { field: form.field, op: form.op, value: parseScalar(form.value, form.field, `${path}.value`) };
}

/** Validate a single node in isolation (its own arity/op/value, never its
 * children). Exported so UI components can show a node's own errors right
 * next to it without re-deriving the same constraints as `validateForm`. */
export function validateNodeOwn(form: FormNode, path = 'root'): ValidationError[] {
	const errors: ValidationError[] = [];
	if (form.kind === 'group') {
		if (form.children.length === 0) {
			errors.push({ path: `${path}.children`, message: `logical op '${form.op}' requires at least one child` });
		}
		if (form.op === 'not' && form.children.length !== 1) {
			errors.push({ path: `${path}.children`, message: "'not' requires exactly one child" });
		}
		return errors;
	}
	if (!ALLOWED_FIELDS.includes(form.field)) {
		errors.push({ path: `${path}.field`, message: `unknown field ${JSON.stringify(form.field)}` });
		return errors;
	}
	const allowedOps = ALLOWED_OPS_BY_FIELD[form.field];
	if (!allowedOps.includes(form.op)) {
		errors.push({
			path: `${path}.op`,
			message: `op ${JSON.stringify(form.op)} not allowed for field ${JSON.stringify(form.field)}; expected one of ${allowedOps.join(', ')}`
		});
		return errors;
	}
	try {
		if (form.op === 'between') {
			parseScalar(form.valueLo, form.field, `${path}.value[0]`);
			parseScalar(form.valueHi, form.field, `${path}.value[1]`);
		} else if (form.op === 'in') {
			parseList(form.valueList, form.field, `${path}.value`);
		} else if (form.op === 'missing') {
			// missing requires no operand
		} else {
			parseScalar(form.value, form.field, `${path}.value`);
		}
	} catch (exc) {
		errors.push({ path: `${path}.value`, message: (exc as Error).message });
	}
	return errors;
}

/** Collect every validation error in the form tree without throwing,
 * matching the constraints `apps/shared/smartlists/schema.py` enforces
 * server-side (allowed ops per field, non-empty `between`/`in` operands,
 * parseable numbers and dates). */
export function validateForm(form: FormNode, path = 'root'): ValidationError[] {
	const errors = validateNodeOwn(form, path);
	if (form.kind === 'group') {
		form.children.forEach((child, i) => {
			errors.push(...validateForm(child, `${path}.children[${i}]`));
		});
	}
	return errors;
}
