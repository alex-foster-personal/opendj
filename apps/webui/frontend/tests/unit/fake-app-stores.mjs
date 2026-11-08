/** Test stub for `$app/stores` page: `$page.url` comes from `globalThis.__pageUrl`. */
export const page = {
	subscribe(fn) {
		const url =
			globalThis.__pageUrl instanceof URL
				? globalThis.__pageUrl
				: new URL('http://127.0.0.1/cloudsync');
		fn({ url });
		return () => {};
	}
};
