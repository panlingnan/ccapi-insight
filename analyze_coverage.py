#!/usr/bin/env python3
"""Compare CloudControl handler coverage vs. a service's full OpenAPI surface.

For a given service (e.g. ECS), it:
  1. Collects every OpenAPI action referenced in the `handlers.*.permissions`
     of all Volcengine::<Service>::* resource types (from the details file).
  2. Fetches the full OpenAPI action list for that service from API Explorer.
  3. Reports which OpenAPI actions are covered vs. NOT covered by CloudControl.

Usage:
  python3 analyze_coverage.py ECS
  python3 analyze_coverage.py VPC --version 2020-04-01
"""

import argparse
import json
import sys
import urllib.request

DETAIL_FILE = "ccapi-resourcetype-details.json"
EXPLORER = "https://api.volcengine.com/api/common/explorer"


def http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def covered_actions(service: str) -> tuple[set, dict]:
    """Return (action_set, {action: [resource_types...]}) from handler permissions."""
    with open(DETAIL_FILE, encoding="utf-8") as f:
        details = json.load(f)
    prefix = f"Volcengine::{service}::"
    perm_prefix = service.lower() + ":"
    actions = set()
    by_action: dict[str, list] = {}
    for type_name, info in details.items():
        if not type_name.startswith(prefix):
            continue
        handlers = info.get("Schema", {}).get("handlers", {})
        for cfg in handlers.values():
            for perm in cfg.get("permissions", []):
                if perm.lower().startswith(perm_prefix):
                    act = perm.split(":", 1)[1]
                    actions.add(act)
                    by_action.setdefault(act, [])
                    if type_name not in by_action[act]:
                        by_action[act].append(type_name)
    return actions, by_action


def default_version(service_code: str) -> str:
    data = http_get_json(f"{EXPLORER}/versions?ServiceCode={service_code}")
    versions = data.get("Result", data).get("Versions", [])
    for v in versions:
        if v.get("IsDefault") == 1:
            return v["Version"]
    return versions[-1]["Version"] if versions else ""


def all_openapi_actions(service_code: str, version: str) -> dict:
    url = (
        f"{EXPLORER}/apis?ServiceCode={service_code}"
        f"&Version={version}&APIVersion={version}"
    )
    data = http_get_json(url)
    groups = data.get("Result", data).get("Groups", [])
    actions = {}
    for g in groups:
        for a in g.get("Apis", []):
            actions[a["Action"]] = {
                "group": g.get("Name", ""),
                "name_cn": a.get("NameCn", ""),
            }
    return actions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", help="Service name, e.g. ECS, VPC, IAM")
    parser.add_argument("--version", default=None, help="API version override")
    args = parser.parse_args()

    service = args.service
    service_code = service.lower()

    version = args.version or default_version(service_code)
    print(f"Service: {service}  (code={service_code}, version={version})\n")

    covered, by_action = covered_actions(service)
    all_actions = all_openapi_actions(service_code, version)
    all_set = set(all_actions)

    covered_in = sorted(covered & all_set)
    covered_only = sorted(covered - all_set)  # permission not in current API list
    not_covered = sorted(all_set - covered)

    print(f"Total OpenAPI actions:      {len(all_set)}")
    print(f"Referenced by handlers:     {len(covered)}")
    print(f"Covered (match API list):   {len(covered_in)}")
    print(f"NOT covered:                {len(not_covered)}")
    if covered_only:
        print(f"Handler perms w/o matching OpenAPI: {covered_only}")
    pct = 100 * len(covered_in) / len(all_set) if all_set else 0
    print(f"Coverage:                   {pct:.1f}%\n")

    print("=== COVERED ===")
    for a in covered_in:
        meta = all_actions[a]
        print(f"  [{meta['group']}] {a} - {meta['name_cn']}")

    print("\n=== NOT COVERED ===")
    # group by API group for readability
    by_group: dict[str, list] = {}
    for a in not_covered:
        meta = all_actions[a]
        by_group.setdefault(meta["group"], []).append((a, meta["name_cn"]))
    for group in sorted(by_group):
        print(f"  # {group}")
        for a, cn in sorted(by_group[group]):
            print(f"    - {a} - {cn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
