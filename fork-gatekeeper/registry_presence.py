#!/usr/bin/env python3
"""Ask whether one registry manifest exists without inventing an answer.

Exit 0 means PRESENT, exit 1 means the registry explicitly answered NOT FOUND,
and exit 2 means UNKNOWN (transport, authentication, malformed/no response, or
the short operational request deadline).  Callers may build only on exit 1.
"""
from __future__ import annotations

import argparse
import json
import sys

from daily_release import Presence, _probe_remote_manifest


RC_PRESENT, RC_ABSENT, RC_UNKNOWN = 0, 1, 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reference")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)

    measured = _probe_remote_manifest(args.reference)
    record = {
        "program": "registry_presence",
        "reference": measured.subject,
        "source": measured.source,
        "state": measured.state.value,
        "detail": measured.detail,
    }
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
    print(f"registry_presence: {measured.state.value.upper()} "
          f"{measured.subject}: {measured.detail}")
    if measured.state is Presence.PRESENT:
        return RC_PRESENT
    if measured.state is Presence.ABSENT:
        return RC_ABSENT
    return RC_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
