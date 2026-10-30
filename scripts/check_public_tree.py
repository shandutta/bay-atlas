#!/usr/bin/env python3
"""Check a public source export for common private artifacts without echoing values.

This is a conservative release aid, not a complete secret or personal-data audit.
In Git repositories only tracked paths are inspected; otherwise the export tree
is scanned. Reports contain categories and file/line locations, never matches.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

PRIVATE_DIRS = {'private', 'data', 'artifacts', 'backups'}
RUNTIME_DIRS = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', '.pytest_cache', 'coverage'}
PRIVATE_SUFFIXES = {'.csv', '.har', '.log', '.sqlite', '.sqlite3', '.db', '.pem', '.key'}
TEXT_SUFFIXES = {'.py', '.js', '.json', '.md', '.html', '.css', '.txt', '.yml', '.yaml', '.toml', '.sh'}
# Pattern syntax is deliberately generic; no installation-specific values belong here.
PATTERNS = (
    ('personal home-directory path', re.compile(r'/(?:Users|home)/[A-Za-z_][A-Za-z0-9_.-]*(?:/|\b)')),
    ('private network hostname', re.compile(r'\b(?:[a-z0-9-]+\.)+ts\.net\b', re.I)),
    ('email address', re.compile(r'(?<![A-Z0-9._%+:/-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.I)),
    ('payment credential identifier', re.compile(r'\b(?:lsrq|lbctxn)_[A-Za-z0-9]{16,}\b')),
    ('provider credential', re.compile(r'\bapify_api_[A-Za-z0-9]{24,}\b')),
    ('GitHub credential', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    ('private key material', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str


def source_paths(root: Path) -> list[Path]:
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '-z'],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return [root / name.decode('utf-8', errors='surrogateescape')
                for name in result.stdout.split(b'\0') if name]
    return sorted(path for path in root.rglob('*')
                  if path.is_file() and not (set(path.relative_to(root).parts) & RUNTIME_DIRS))


def inspect_file(root: Path, path: Path) -> list[Finding]:
    relative = path.relative_to(root)
    label = relative.as_posix()
    parts = relative.parts
    findings = []
    if path.is_symlink():
        return [Finding(label, 0, 'symlink requires manual review')]
    if set(parts[:-1]) & PRIVATE_DIRS or path.suffix.lower() in PRIVATE_SUFFIXES or path.name.startswith('.env'):
        findings.append(Finding(label, 0, 'private operational file'))
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {'.gitignore', 'LICENSE'}:
        return findings
    try:
        content = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return findings + [Finding(label, 0, 'unreadable source file')]
    third_party = len(parts) >= 3 and parts[:2] == ('public', 'vendor')
    for line_number, line in enumerate(content.splitlines(), 1):
        for category, pattern in PATTERNS:
            if category == 'email address' and third_party:
                continue  # Preserve original third-party attribution and contact details.
            if pattern.search(line):
                findings.append(Finding(label, line_number, category))
    if relative.parent.as_posix() == 'tests/fixtures' and path.suffix.lower() == '.json':
        try:
            payload = json.loads(content)
            if not isinstance(payload, list) or not all(isinstance(row, dict) and row.get('_synthetic_fixture') is True for row in payload):
                findings.append(Finding(label, 0, 'fixture is not explicitly synthetic'))
        except ValueError:
            findings.append(Finding(label, 0, 'fixture is not valid JSON'))
    return findings


def audit(root: Path) -> tuple[int, list[Finding]]:
    paths = source_paths(root)
    return len(paths), [finding for path in paths for finding in inspect_file(root, path)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        count, findings = audit(args.root.resolve())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'ok': False, 'error': 'Could not inventory source files', 'error_type': type(exc).__name__}))
        return 2
    print(json.dumps({'ok': not findings, 'files_scanned': count,
                      'findings': [asdict(finding) for finding in findings]}, indent=2))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
