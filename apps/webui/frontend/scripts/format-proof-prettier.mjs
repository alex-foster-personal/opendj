// Formats each base blob with the frontend's installed prettier, for scripts/format_proof_prettier.py
// (issue #4456, (private path)). Reads {options, files: [{path, source}]} as JSON on stdin and
// writes {version, results: [{path, formatted, reformatted} | {path, error}]} on stdout. `reformatted` is the
// second pass, so the caller can require idempotence. It never reads the working tree's .prettierrc: the
// caller passes the options of the commit under proof, and this process must run from apps/webui/frontend so
// the plugins named there resolve against the pinned node_modules.
import * as prettier from 'prettier';

async function readStdin() {
	const chunks = [];
	for await (const chunk of process.stdin) chunks.push(chunk);
	return Buffer.concat(chunks).toString('utf8');
}

async function formatOne(options, file) {
	try {
		const formatted = await prettier.format(file.source, { ...options, filepath: file.path });
		const reformatted = await prettier.format(formatted, { ...options, filepath: file.path });
		return { path: file.path, formatted, reformatted };
	} catch (error) {
		return { path: file.path, error: String(error && error.message ? error.message : error) };
	}
}

const request = JSON.parse(await readStdin());
const results = [];
for (const file of request.files) results.push(await formatOne(request.options, file));
process.stdout.write(JSON.stringify({ version: prettier.version, results }));
