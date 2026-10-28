/** Test stub for `$app/navigation` goto(): records the route and, like the
 * real router after a navigation nothing cancelled, moves the location. */
export async function goto(route) {
	if (!Array.isArray(globalThis.__shellNavGotoCalls)) {
		globalThis.__shellNavGotoCalls = [];
	}
	globalThis.__shellNavGotoCalls.push(route);
	if (globalThis.window?.location) globalThis.window.location.pathname = route;
}
