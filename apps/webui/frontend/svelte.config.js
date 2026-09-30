import { execSync } from 'node:child_process';

import adapter from '@sveltejs/adapter-static';

// The full git sha of the checkout this static build was made FROM, so
// `_app/version.json` becomes the frontend bundle's own, non-proxied word
// about its own identity (Codex P1/BLOCKING, PR #4034,
// discussion_r4132371694: `/api/v1/build-info` is served BY THE ENGINE, and
// vite-dev's `/api` proxy forwards a frontend-side check there transitively,
// so it can never verify the frontend bundle itself -- only this static
// asset, checked by scripts/perf/capture_library_mode.py's
// `_verify_frontend_build_version`, can). No fallback: a build made outside
// a git checkout should fail loudly rather than ship an unstamped bundle
// that would silently pass no identity check at all.
const GIT_SHA_FULL = execSync('git rev-parse HEAD', { cwd: import.meta.dirname })
  .toString()
  .trim();

// Codex P1/BLOCKING, PR #4034, discussion_r4132707456: kit.version.name
// carried only the sha, with no dirty marker -- unlike the engine's own
// build-info (apps/engine_core/build_info.py's git_dirty), so a frontend
// built from an uncommitted tree matched the expected sha and passed
// unnoticed. Checked repo-wide (`git rev-parse --show-toplevel`), matching
// how the engine defines "dirty" for the same commit. `-dirty` is a suffix
// rather than a second version.json field because kit.version.name is a
// single string; scripts/perf/capture_library_mode.py's
// `_verify_frontend_build_version` looks for exactly this suffix.
const REPO_ROOT = execSync('git rev-parse --show-toplevel', { cwd: import.meta.dirname })
  .toString()
  .trim();
const GIT_DIRTY = execSync('git status --porcelain', { cwd: REPO_ROOT }).toString().trim() !== '';
const FRONTEND_VERSION = GIT_DIRTY ? `${GIT_SHA_FULL}-dirty` : GIT_SHA_FULL;

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    adapter: adapter({
      fallback: 'index.html',
      pages: 'build',
      assets: 'build',
      strict: false,
    }),
    alias: {
      $lib: 'src/lib',
    },
    version: {
      name: FRONTEND_VERSION,
    },
  },
};

export default config;
