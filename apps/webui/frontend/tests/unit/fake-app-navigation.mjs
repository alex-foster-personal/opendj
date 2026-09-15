/** Test stub for `$app/navigation` goto(). */
export async function goto(route) {
	if (!Array.isArray(globalThis.__shellNavGotoCalls)) {
		globalThis.__shellNavGotoCalls = [];
	}
	globalThis.__shellNavGotoCalls.push(route);
}
