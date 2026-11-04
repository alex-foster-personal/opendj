// The one pngjs call the rendered contrast audit makes (PNG.sync.read). The
// package ships no types and @types/pngjs is not a dependency of this repo.
declare module 'pngjs' {
	export const PNG: {
		sync: { read(buffer: Buffer): { width: number; height: number; data: Uint8Array } };
	};
}
