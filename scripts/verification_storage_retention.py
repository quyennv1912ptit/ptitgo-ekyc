"""Inspect retention, or explicitly apply it to the configured verification storage."""

import argparse
import json

from ekyc.config import get_settings
from ekyc.services.document_retention import sweep
from ekyc.services.verification_store import VerificationStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply instead of count-only preview")
    args = parser.parse_args()
    settings = get_settings()
    if settings.verification_db_path is None or not settings.verification_db_path.is_file():
        parser.error("Configure an existing verification database")
    counts = sweep(VerificationStore.from_settings(settings), settings, apply=args.apply)
    print(json.dumps({"applied": args.apply, "counts": counts}, sort_keys=True))
    return int(bool(counts.get("failed")))


if __name__ == "__main__":
    raise SystemExit(main())
