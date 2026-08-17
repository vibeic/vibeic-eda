#!/usr/bin/env python3
"""Run the exact release smoke under container-aware progress leases."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from daily_release import SMOKE, smoke_image


RC_OK, RC_FAILED, RC_NORECORD = 0, 1, 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)

    failures = smoke_image(args.image)
    no_record = [row for row in failures
                 if row.get("outcome") != "natural_failure"]
    record = {
        "program": "release_candidate_smoke",
        "image": args.image,
        "declared": len(SMOKE),
        "failures": failures,
        "status": ("norecord" if no_record else
                   "failed" if failures else "passed"),
    }
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    for row in failures:
        print(f"{row['tool']}: {row.get('outcome')} rc={row.get('rc')}: "
              f"{row.get('err', '')}", file=sys.stderr)
    if no_record:
        print(f"[NORECORD] {len(no_record)} smoke probe(s) did not complete",
              file=sys.stderr)
        return RC_NORECORD
    if failures:
        print(f"[FAIL] {len(failures)} release smoke probe(s) failed",
              file=sys.stderr)
        return RC_FAILED
    print(f"[PASS] all {len(SMOKE)} release smoke probes completed")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
