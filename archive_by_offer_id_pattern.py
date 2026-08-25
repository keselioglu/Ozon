"""
One-off cleanup: archives every live Ozon product whose offer_id matches a
given regex pattern — used to remove an entire unrelated brand/product line
from the storefront by naming convention alone.

This is NOT part of daily_run.py — removing an entire product line from the
live store is always a deliberate, by-hand decision. Re-running it later is
safe (already-archived items are skipped) but it never un-archives anything.

Usage:
    python archive_by_offer_id_pattern.py "^IKEA?\\d*-"

Every archived product_id/offer_id is logged to archived_by_pattern_log.jsonl
before the API call, so the exact set touched is always recoverable via
/v1/product/unarchive if this needs to be reversed.
"""
import re
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from archive_helpers import archive_offer_ids, archive_offer_ids_individually

ARCHIVE_LOG = "archived_by_pattern_log.jsonl"


def find_matching_live_offer_ids(pattern):
    """Returns the set of live (non-archived) offer_ids matching pattern
    (a regex applied to the start of the offer_id, e.g. r'^IKEA?\\d*-')."""
    regex = re.compile(pattern, re.IGNORECASE)
    matches = set()
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        for item in items:
            oid = item.get("offer_id", "")
            if regex.match(oid) and not item.get("archived"):
                matches.add(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return matches


def main():
    if len(sys.argv) != 2:
        return print('Usage: python archive_by_offer_id_pattern.py "<regex>"\n'
                      'Example: python archive_by_offer_id_pattern.py "^IKEA?\\\\d*-"')

    pattern = sys.argv[1]
    print(f"Finding live offer_ids matching pattern: {pattern!r}")
    matching = find_matching_live_offer_ids(pattern)
    print(f"{len(matching)} live offer_id(s) matched.\n")

    if not matching:
        return print("Nothing to archive.")

    total_ok, total_failed, already_archived = archive_offer_ids(matching, ARCHIVE_LOG)
    print(f"\nDone. {total_ok} archived, {total_failed} failed (in batches), {already_archived} already archived.")

    if total_failed:
        print(f"\nRetrying the {total_failed} batch-failed item(s) individually "
              f"(a whole batch fails if even one item has FBO stock)...")
        # Re-derive which offer_ids are still unarchived to retry only those.
        still_live = find_matching_live_offer_ids(pattern)
        ok, fbo_blocked, other_failures = archive_offer_ids_individually(still_live, ARCHIVE_LOG)
        print(f"Individual retry: {len(ok)} archived, {len(fbo_blocked)} blocked by FBO stock, "
              f"{len(other_failures)} other failures.")
        if fbo_blocked:
            print("Blocked by FBO stock (left live — Ozon won't archive while stock remains in their warehouse):")
            for oid in fbo_blocked:
                print(f"  {oid}")
        if other_failures:
            print("Other failures:")
            for oid, err in other_failures:
                print(f"  {oid}: {err}")

    print(f"\nFull record in {ARCHIVE_LOG} — every offer_id/product_id touched, for un-archiving if needed.")


if __name__ == "__main__":
    main()
