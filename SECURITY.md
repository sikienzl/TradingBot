# Security Guidelines

This repository contains trading automation code and should be handled with strict secret hygiene.

## Supported Versions

Only the latest commit on the main branch is considered supported.

## Reporting a Vulnerability

Please open a private security report through GitHub Security Advisories if possible.
If private reporting is not available, contact the maintainer directly and avoid posting exploit details publicly before a fix is available.

## Secret Handling Rules

- Never commit real exchange credentials.
- Keep local credentials only in `.env` (ignored by git).
- Use placeholder values in tracked example files.
- Rotate keys immediately if they are exposed in logs, terminal output, screenshots, chat transcripts, or commits.

## Pre-Public Checklist

1. Rotate all exchange API keys used during development.
2. Verify `.env` is ignored and not tracked.
3. Run a tracked-file secret scan before each release.
4. Confirm no local test env files are staged.
5. Keep API permissions minimal (no withdrawal permission for bot keys).

## Suggested Pre-Push Scan

```sh
git ls-files -z | xargs -0 rg -n --no-heading -i "(api[_-]?key|api[_-]?secret|token|password|private[_-]?key|secret[_-]?key|-----BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36,})" || true
```
