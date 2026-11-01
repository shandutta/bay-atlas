#!/usr/bin/env python3
"""Validate a plan, or explicitly acquire one target into private staging.

No action publishes data or touches the web application's existing grant/ledger.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targeted_acquisition import AcquisitionError, execute_one, validate_manifest
from apify_source import ApifyError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--execute-target", help="Explicitly run or collect only this target ID")
    parser.add_argument("--grant", type=Path, help="Separate, verified one-time grant JSON")
    parser.add_argument("--credential", type=Path, help="Owner-only JSON with an api_key; no token on command line")
    parser.add_argument("--campaign-dir", type=Path, help="Dedicated private directory for new ledger and outputs")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        summary = validate_manifest(manifest)
        if args.execute_target:
            if not all((args.grant, args.credential, args.campaign_dir)):
                parser.error("Execution requires --grant, --credential and --campaign-dir")
            for path in (args.grant, args.credential):
                if path.stat().st_mode & 0o077:
                    raise AcquisitionError("Grant and credential files must be owner-only.")
            grant = json.loads(args.grant.read_text())
            token = json.loads(args.credential.read_text()).get("api_key")
            summary = execute_one(manifest, grant, token, args.campaign_dir, args.execute_target)
        print(json.dumps(summary, indent=2, allow_nan=False))
    except (AcquisitionError, ApifyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, AttributeError):
        print("A required local input could not be read or validated.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
