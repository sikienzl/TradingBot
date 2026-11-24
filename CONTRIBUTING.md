# Contributing

Thanks for your interest in improving this project.

## Before You Start

- Read the main project documentation in `README.md`.
- For security and disclosure guidance, read `SECURITY.md`.
- Do not commit secrets. `.env` and local test env files must stay untracked.

## Development Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running Tests

```sh
.venv/bin/python -m pytest tests/ -v
```

To generate a coverage report for SonarQube, run from the repository root:

```sh
.venv/bin/python -m pytest tests/ --cov=src --cov=scripts --cov-report=term &&
.venv/bin/python -m coverage xml -o coverage.xml &&
pysonar \
  --sonar-host-url=http://localhost:9000 \
  --sonar-project-key=TradingBot \
  -Dsonar.python.coverage.reportPaths=coverage.xml
```

Set `SONAR_TOKEN` securely in your shell before running the scanner. The XML
report is generated locally and is not committed.

## Coding Guidelines

- Keep changes focused and minimal.
- Preserve existing behavior unless your PR intentionally changes it.
- Add or update tests when changing behavior.
- Keep logs and error messages actionable.

## Trading-Specific Safety

- Default to safe behavior in examples (`DRY_RUN=true`).
- Clearly document risks of any strategy/risk-control changes.
- Never hardcode API keys, secrets, or account data.

## Commit and Pull Request Guidelines

- Use clear commit messages describing intent and scope.
- One logical change per pull request when possible.
- In the PR description include:
  - What changed
  - Why it changed
  - How it was tested
  - Any operational risk or migration notes

## Reporting Bugs and Requesting Features

Please use the issue templates. Include enough detail to reproduce problems, including environment and logs.
