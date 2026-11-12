// The planted retained buffer (--controls). It is scored from the
// before/after-plant dumps alone, so it is released, collected and proven
// gone before any measured phase: left on window it would sit inside every
// later dump, OS reading and heap snapshot as if the app owned it.

export async function plantBuffer(page, mib) {
	await page.evaluate((mib) => {
		window.__perflabPlantedBuffer = new Float32Array((mib * 1048576) / 4).fill(1.5);
		window.__perflabPlantedRef = new WeakRef(window.__perflabPlantedBuffer);
	}, mib);
}

// null once the buffer is collected, else why it is not
export async function releasePlantedBuffer(page, cdp) {
	await page.evaluate(() => { delete window.__perflabPlantedBuffer; });
	await cdp.send('HeapProfiler.collectGarbage');
	const alive = await page.evaluate(() => {
		const ref = window.__perflabPlantedRef;
		delete window.__perflabPlantedRef;
		return ref === undefined ? 'nothing was planted' : ref.deref() !== undefined ? 'still reachable after a forced collection' : null;
	});
	return alive === null ? null : `planted buffer ${alive}`;
}
