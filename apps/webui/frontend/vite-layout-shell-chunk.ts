/**
 * Emits what every route loads at boot as two chunks instead of about fifty.
 *
 * Rollup gives a module its own chunk whenever the set of entries that reach
 * it differs from its neighbor's. SvelteKit imports the root layout (route
 * node 0) dynamically, like any other node, so Rollup cannot know that it is
 * loaded on every route: it cut the layout's static closure into about 47
 * chunks, 20 of them under 500 gzip bytes, one per combination of lazy routes
 * that happen to share a module. All of them are downloaded at boot anyway,
 * and each pays for its own import and export lists and restarts the gzip
 * dictionary.
 *
 * Two manual chunks remove that overhead:
 *   boot-runtime  the static closure of SvelteKit's client entry and the
 *                 generated app module (Svelte, the kit client, client hooks)
 *   layout-shell  the static closure of route node 0, minus boot-runtime
 *
 * Measured Thu 1 Oct 2026 on af--preview-mixtour-io 7e14328ea0, gzip under
 * build/_app/immutable/ as scripts/bundle-budget.mjs weighs it:
 *   library      269,310 -> 247,663  (55 -> 9 files)
 *   performance  241,529 -> 239,606  (19 -> 18 files)
 *   other-lazy   292,698 -> 288,815  (65 files, shorter import lists)
 *
 * WHY THIS DOES NOT CHANGE BEHAVIOR
 *   - Both sets are closed under static imports, so layout-shell imports
 *     only boot-runtime and boot-runtime imports nothing: no chunk cycle.
 *   - Evaluation moments are kept. boot-runtime evaluates when the entries
 *     load, layout-shell when node 0 is imported, exactly as their parts did.
 *     The two are kept apart for that reason: one merged chunk would run
 *     every layout module before SvelteKit starts.
 *   - Neither entry module nor node 0 itself is assigned, so SvelteKit's
 *     entry files keep their names and shapes.
 *
 * THE ONE INEXACTNESS, stated so nobody has to rediscover it. Rollup pulls a
 * manual chunk's whole static dependency tree in by import STATEMENT, before
 * tree-shaking decides which imports are used. A side-effect-free module
 * that the layout closure imports but only lazy code actually uses therefore
 * moves from a lazy chunk into these two. On the measured tree that is nine
 * modules, about 7.8 KB before minification (three app helpers and six
 * Svelte block and binding helpers that only /performance uses). They cannot
 * change behavior, because Rollup only drops an import edge to a module with
 * no side effects, but they are boot weight now, and the library figure
 * above already includes them.
 */

const LAYOUT_NODE = /\/generated\/client-optimized\/nodes\/0\.js$/;
const BOOT_ENTRY = /\/runtime\/client\/entry\.js$|\/generated\/client-optimized\/app\.js$/;

export const BOOT_RUNTIME_CHUNK = 'boot-runtime';
export const LAYOUT_SHELL_CHUNK = 'layout-shell';

type ImportedIdsOf = (moduleId: string) => readonly string[];
type BootChunkName = typeof BOOT_RUNTIME_CHUNK | typeof LAYOUT_SHELL_CHUNK;

function _staticClosure(roots: readonly string[], importedIdsOf: ImportedIdsOf): Set<string> {
	const seen = new Set<string>();
	const queue = [...roots];
	while (queue.length > 0) {
		const moduleId = queue.pop() as string;
		if (seen.has(moduleId)) continue;
		seen.add(moduleId);
		queue.push(...importedIdsOf(moduleId));
	}
	return seen;
}

/**
 * Chunk name per module for the two boot chunks. Entry modules and node 0
 * are never named. Empty when the graph has no client layout node, which is
 * the server build.
 */
export function bootChunkByModuleId(
	moduleIds: readonly string[],
	importedIdsOf: ImportedIdsOf
): Map<string, BootChunkName> {
	const chunkByModuleId = new Map<string, BootChunkName>();
	const layoutNodes = moduleIds.filter((moduleId) => LAYOUT_NODE.test(moduleId));
	if (layoutNodes.length === 0) return chunkByModuleId;
	const bootEntries = moduleIds.filter((moduleId) => BOOT_ENTRY.test(moduleId));
	if (bootEntries.length !== 2) {
		throw new Error(
			`boot chunks: expected SvelteKit's client entry and generated app module, found ${bootEntries.length} boot entries (SvelteKit output shape changed)`
		);
	}
	const unnamed = new Set([...bootEntries, ...layoutNodes]);
	const bootClosure = _staticClosure(bootEntries, importedIdsOf);
	for (const moduleId of bootClosure) {
		if (!unnamed.has(moduleId)) chunkByModuleId.set(moduleId, BOOT_RUNTIME_CHUNK);
	}
	for (const moduleId of _staticClosure(layoutNodes, importedIdsOf)) {
		if (!unnamed.has(moduleId) && !bootClosure.has(moduleId)) {
			chunkByModuleId.set(moduleId, LAYOUT_SHELL_CHUNK);
		}
	}
	return chunkByModuleId;
}

interface ManualChunkMeta {
	getModuleIds: () => IterableIterator<string>;
	getModuleInfo: (moduleId: string) => { importedIds: readonly string[] } | null;
}

/** Rollup `output.manualChunks`: the closures are computed once per module graph. */
export function bootManualChunks(): (moduleId: string, meta: ManualChunkMeta) => string | undefined {
	const chunksByGraph = new WeakMap<ManualChunkMeta['getModuleInfo'], Map<string, BootChunkName>>();
	return (moduleId, meta) => {
		let chunkByModuleId = chunksByGraph.get(meta.getModuleInfo);
		if (chunkByModuleId === undefined) {
			chunkByModuleId = bootChunkByModuleId(
				[...meta.getModuleIds()],
				(id) => meta.getModuleInfo(id)?.importedIds ?? []
			);
			chunksByGraph.set(meta.getModuleInfo, chunkByModuleId);
		}
		return chunkByModuleId.get(moduleId);
	};
}
