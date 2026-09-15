// SPA mode -- no SSR. The static adapter uses fallback index.html.
import { startLibraryBootHydration } from '$lib/rb/library-boot-hydration';

export const ssr = false;
export const prerender = false;
export const trailingSlash = 'ignore';

if (typeof window !== 'undefined') {
	startLibraryBootHydration();
}
