# Contributing to music-dj-tools

First off, thanks for taking the time to contribute!

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pyrekordbox download-key  # one-time: cache the master.db decryption key
```

## Testing

Run tests via `make test`. (Note: Test infrastructure is being actively added).

## Development Process

1. **Issues First**: For non-trivial changes, please open an issue first to discuss the proposed change.
2. **Pull Requests**: Open PRs against the `master` branch.
3. **Commit Style**: We follow Conventional Commits (e.g., `feat(X):`, `docs(X):`, `chore:`, `fix:`).
4. **Code Style**: Match the existing code style (formatting tools like ruff/black will be added soon).

## Developer Certificate of Origin (DCO)

We require all contributions to be signed off. By signing off, you certify that you wrote the patch or otherwise have the right to pass it on as an open-source patch.

To sign off on your commits, simply use the `-s` flag when committing:
```bash
git commit -s -m "feat(module): add amazing feature"
```

## Licensing Agreement

By contributing to this project, you agree that your contributions will be licensed under its Apache License 2.0.
