/** One remembered pick: `weight` is the decayed pick count as of `at` (epoch ms). */
export interface CommandPick {
	key: string;
	weight: number;
	at: number;
}

/** Raycast-style half-life for command frecency (3 days). */
export const FRECENCY_HALF_LIFE_MS = 3 * 24 * 60 * 60 * 1000;
/** Score points per log2(1 + decayed picks). */
export const FRECENCY_WEIGHT = 6;
/** Remembered keys at most. */
export const PICKS_CAP = 50;

export interface RankItem {
	id: string;
	label: string;
	keywords?: readonly string[];
}

export interface RankOptions<T> {
	/** Stable frecency key; default item.id. */
	key?: (item: T) => string;
	picks?: readonly CommandPick[];
	/** Epoch ms used to decay picks; default Date.now(). */
	now?: number;
}

/** Tuned for palette-queries fixture (word-start 8, hump/boundary 7, run 5, gap 1). */
const WORD_START_BONUS = 8;
const HUMP_BOUNDARY_BONUS = 7;
const CONTIGUOUS_BONUS = 5;
const GAP_PENALTY = 1;
const LEADING_SKIP_CAP = 3;

let _dpScratch = new Float64Array(0);

function _isSeparator(ch: string): boolean {
	return /[\s\-_/.:,()]/.test(ch);
}

function _isDigit(ch: string): boolean {
	return ch >= '0' && ch <= '9';
}

function _isLetter(ch: string): boolean {
	const c = ch.toLowerCase();
	return c >= 'a' && c <= 'z';
}

function _wordStartBonus(j: number, word: string): boolean {
	if (j === 0) return true;
	return _isSeparator(word[j - 1]!);
}

function _camelHumpBonus(j: number, word: string): boolean {
	if (j === 0) return false;
	const cur = word[j]!;
	const prev = word[j - 1]!;
	return cur !== cur.toLowerCase() && prev === prev.toLowerCase();
}

function _digitBoundaryBonus(j: number, word: string): boolean {
	if (j === 0) return false;
	const a = word[j - 1]!;
	const b = word[j]!;
	return (_isDigit(a) && _isLetter(b)) || (_isLetter(a) && _isDigit(b));
}

function _charScore(
	i: number,
	j: number,
	pattern: string,
	word: string,
	prevJ: number | null
): number {
	let score = 1;
	const wordStart = _wordStartBonus(j, word);
	const hump = _camelHumpBonus(j, word);
	const boundary = _digitBoundaryBonus(j, word);
	if (wordStart) score += WORD_START_BONUS;
	else if (hump) score += HUMP_BOUNDARY_BONUS;
	else if (boundary) score += HUMP_BOUNDARY_BONUS;

	if (i > 0 && prevJ === j - 1) score += CONTIGUOUS_BONUS;

	if (i > 0 && prevJ !== null && prevJ !== j - 1) {
		const exempt = wordStart || hump || boundary;
		if (!exempt) score -= GAP_PENALTY;
	}

	if (i === 0 && !wordStart && !hump) {
		score -= Math.min(j, LEADING_SKIP_CAP);
	}

	return score;
}

function _subsequence(lowerPattern: string, lowerWord: string): boolean {
	let pi = 0;
	for (let wi = 0; wi < lowerWord.length && pi < lowerPattern.length; wi++) {
		if (lowerWord[wi] === lowerPattern[pi]) pi++;
	}
	return pi === lowerPattern.length;
}

/**
 * VS Code-style subsequence fuzzy score; null when `pattern` is not a case-insensitive subsequence of `word`.
 */
export function fuzzyScore(pattern: string, word: string): number | null {
	if (pattern.length === 0) return 0;
	const lowerPattern = pattern.toLowerCase();
	const lowerWord = word.toLowerCase();
	if (!_subsequence(lowerPattern, lowerWord)) return null;

	const m = lowerPattern.length;
	const n = lowerWord.length;
	const need = (m + 1) * (n + 1);
	if (_dpScratch.length < need) _dpScratch = new Float64Array(need);
	const best = _dpScratch;
	best.fill(-Infinity, 0, need);

	for (let j = 0; j < n; j++) {
		if (lowerWord[j] !== lowerPattern[0]) continue;
		const cs = _charScore(0, j, pattern, word, null);
		best[j] = cs;
	}

	for (let i = 1; i < m; i++) {
		const rowOff = i * (n + 1);
		const prevOff = (i - 1) * (n + 1);
		let prefixMax = -Infinity;
		for (let j = 0; j < n; j++) {
			if (lowerWord[j] !== lowerPattern[i]) {
				best[rowOff + j] = -Infinity;
				const prevBest = best[prevOff + j];
				if (prevBest > prefixMax) prefixMax = prevBest;
				continue;
			}
			let top = -Infinity;
			const runPrev = best[prevOff + j - 1];
			if (j > 0 && Number.isFinite(runPrev)) {
				const run = runPrev + _charScore(i, j, pattern, word, j - 1);
				if (run > top) top = run;
			}
			if (Number.isFinite(prefixMax)) {
				const wordStart = _wordStartBonus(j, word);
				const hump = _camelHumpBonus(j, word);
				const boundary = _digitBoundaryBonus(j, word);
				const exempt = wordStart || hump || boundary;
				const scattered =
					prefixMax + _charScore(i, j, pattern, word, null) - (exempt ? 0 : GAP_PENALTY);
				if (scattered > top) top = scattered;
			}
			best[rowOff + j] = top;
			const prevBest = best[prevOff + j];
			if (Number.isFinite(prevBest) && prevBest > prefixMax) prefixMax = prevBest;
		}
	}

	let result = -Infinity;
	const lastOff = (m - 1) * (n + 1);
	for (let j = 0; j < n; j++) {
		const v = best[lastOff + j];
		if (Number.isFinite(v) && v > result) result = v;
	}
	return result === -Infinity ? null : result;
}

/** Decayed pick weight at `now` (clock skew clamped). */
export function frecency(pick: CommandPick | undefined, now: number): number {
	if (!pick) return 0;
	const age = Math.max(0, now - pick.at);
	return pick.weight * 0.5 ** (age / FRECENCY_HALF_LIFE_MS);
}

/** Remember one pick; returns a new array capped at PICKS_CAP. */
export function notePick(picks: readonly CommandPick[], key: string, now: number): CommandPick[] {
	const existing = picks.find((p) => p.key === key);
	const weight = (existing ? frecency(existing, now) : 0) + 1;
	const head: CommandPick = { key, weight, at: now };
	const tail = picks.filter((p) => p.key !== key);
	let merged = [head, ...tail];
	if (merged.length <= PICKS_CAP) return merged;
	let dropIdx = 0;
	let dropW = frecency(merged[0]!, now);
	for (let i = 1; i < merged.length; i++) {
		const w = frecency(merged[i]!, now);
		if (w < dropW) {
			dropW = w;
			dropIdx = i;
		}
	}
	return [...merged.slice(0, dropIdx), ...merged.slice(dropIdx + 1)];
}

function _tokenScore(token: string, item: RankItem): number | null {
	let best: number | null = fuzzyScore(token, item.label);
	const kws = item.keywords;
	if (kws) {
		for (const kw of kws) {
			const s = fuzzyScore(token, kw);
			if (s !== null && (best === null || s > best)) best = s;
		}
	}
	return best;
}

/** Rank palette commands by fuzzy match plus frecency boost. */
export function rankCommands<T extends RankItem>(
	items: readonly T[],
	query: string,
	opts: RankOptions<T> = {}
): T[] {
	const tokens = query.trim().split(/\s+/).filter(Boolean);
	const keyFn = opts.key ?? ((item: T) => item.id);
	const now = opts.now ?? Date.now();
	const picks = opts.picks ?? [];
	const pickMap = new Map<string, CommandPick>();
	for (const p of picks) pickMap.set(p.key, p);

	if (tokens.length === 0) {
		const indexed = items.map((item, at) => ({ item, at }));
		indexed.sort((a, b) => {
			const fa = frecency(pickMap.get(keyFn(a.item)), now);
			const fb = frecency(pickMap.get(keyFn(b.item)), now);
			if (fb !== fa) return fb - fa;
			return a.at - b.at;
		});
		return indexed.map(({ item }) => item);
	}

	const scored: { item: T; total: number; labelLen: number; at: number }[] = [];
	for (let at = 0; at < items.length; at++) {
		const item = items[at]!;
		let fuzzy = 0;
		let ok = true;
		for (const token of tokens) {
			const ts = _tokenScore(token, item);
			if (ts === null) {
				ok = false;
				break;
			}
			fuzzy += ts;
		}
		if (!ok) continue;
		const pick = pickMap.get(keyFn(item));
		const total = fuzzy + FRECENCY_WEIGHT * Math.log2(1 + frecency(pick, now));
		scored.push({ item, total, labelLen: item.label.length, at });
	}
	scored.sort((a, b) => b.total - a.total || a.labelLen - b.labelLen || a.at - b.at);
	return scored.map(({ item }) => item);
}
