/** Single source of truth for the public GitHub repository URL.
 *
 * Every link in the UI that points at the project's issues or pull requests
 * derives from this constant. It is a constant (not read from a git remote)
 * because the browser cannot read a git remote. Credential-free https form
 * only: `https://host/owner/repo`. */
export const GITHUB_REPO_BASE = 'https://github.com/alex-foster-personal/opendj';
