#!/usr/bin/env python3
"""Build coverage-data.json: for every service present in the CloudControl
resource details, list its full OpenAPI surface (grouped) and mark which
actions are referenced by resource-type handlers.

Output shape:
{
  "generatedAt": "...",
  "services": [
    {
      "service": "ECS",
      "serviceCode": "ecs",
      "version": "2020-04-01",
      "resourceTypes": ["Volcengine::ECS::Instance", ...],
      "total": 136, "covered": 56, "notCovered": 80,
      "groups": [
        {"name": "实例",
         "apis": [{"action": "RunInstances", "nameCn": "创建实例",
                    "covered": true, "byTypes": ["Volcengine::ECS::Instance"]}, ...]}
      ],
      "handlerOnly": ["DeleteKeyPair"]   # perms with no matching OpenAPI
    }, ...
  ]
}
"""

import datetime
import json
import sys
import time
import urllib.request

DETAIL_FILE = "ccapi-resourcetype-details.json"
OUTPUT = "coverage-data.json"
EXPLORER = "https://api.volcengine.com/api/common/explorer"

# Segment (from TypeName) -> permission prefix -> candidate API service codes.
# We try candidates in order until the versions endpoint returns data.
# Most service codes equal the lowercase permission prefix; a few differ.
SEG_OVERRIDES = {
    "AutoScaling": ["auto_scaling"],
    "FWCenter": ["fw_center"],
    "CDN": ["CDN", "cdn"],
    "ESCloud": ["ESCloud", "escloud"],
    "FileNAS": ["FileNAS", "filenas"],
    "Kafka": ["Kafka", "kafka"],
    "RabbitMQ": ["RabbitMQ", "rabbitmq"],
    "Redis": ["Redis", "redis"],
    "RocketMQ": ["RocketMQ", "rocketmq"],
    "TLS": ["TLS", "tls"],
    "StorageEBS": ["storage_ebs", "ebs"],
    "CloudIdentity": ["cloudidentity"],
    "CloudMonitor": ["cloudmonitor", "Volc_Stack"],
    "PrivateZone": ["private_zone"],
    "RDSMsSQL": ["rds_mssql"],
    "RDSMySQL": ["rds_mysql"],
    "RDSPostgreSQL": ["rds_postgresql"],
    "VEDBM": ["vedbm"],
    "ID": ["id"],
}


def http_get_json(url: str, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def perm_prefix_for(types_for_seg, details) -> str:
    counts = {}
    for t in types_for_seg:
        for cfg in details[t].get("Schema", {}).get("handlers", {}).values():
            for p in cfg.get("permissions", []):
                if ":" in p:
                    pre = p.split(":", 1)[0]
                    counts[pre] = counts.get(pre, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def covered_actions_for(types_for_seg, details, perm_prefix):
    """action -> [type names]"""
    by_action = {}
    pl = perm_prefix.lower()
    for t in types_for_seg:
        for cfg in details[t].get("Schema", {}).get("handlers", {}).values():
            for p in cfg.get("permissions", []):
                if p.lower().startswith(pl + ":"):
                    act = p.split(":", 1)[1]
                    by_action.setdefault(act, [])
                    if t not in by_action[act]:
                        by_action[act].append(t)
    return by_action


def resolve_service(seg, perm_prefix):
    """Return (serviceCode, version, groups) or (None, None, None)."""
    candidates = list(SEG_OVERRIDES.get(seg, []))
    # always also try the permission prefix and the lowercase segment
    for c in (perm_prefix, perm_prefix.lower(), seg.lower(), seg):
        if c and c not in candidates:
            candidates.append(c)
    for code in candidates:
        try:
            vdata = http_get_json(f"{EXPLORER}/versions?ServiceCode={code}")
            versions = vdata.get("Result", {}).get("Versions", [])
        except Exception:  # noqa: BLE001
            continue
        if not versions:
            continue
        # Try default version first, then remaining versions newest-first,
        # since some default versions have an empty API list.
        default = [v for v in versions if v.get("IsDefault") == 1]
        rest = sorted(
            (v for v in versions if v.get("IsDefault") != 1),
            key=lambda v: v.get("Version", ""),
            reverse=True,
        )
        ordered = default + rest
        for v in ordered:
            version = v["Version"]
            try:
                adata = http_get_json(
                    f"{EXPLORER}/apis?ServiceCode={code}"
                    f"&Version={version}&APIVersion={version}"
                )
                groups = adata.get("Result", {}).get("Groups", [])
            except Exception:  # noqa: BLE001
                continue
            if groups:
                return code, version, groups
    return None, None, None


def main() -> int:
    with open(DETAIL_FILE, encoding="utf-8") as f:
        details = json.load(f)

    seg_types = {}
    for t in details:
        parts = t.split("::")
        if len(parts) >= 3:
            seg_types.setdefault(parts[1], []).append(t)

    services_out = []
    for seg in sorted(seg_types):
        types_for_seg = sorted(seg_types[seg])
        perm_prefix = perm_prefix_for(types_for_seg, details)
        by_action = covered_actions_for(types_for_seg, details, perm_prefix)
        code, version, groups = resolve_service(seg, perm_prefix)
        if not groups:
            print(f"  ! {seg}: could not resolve OpenAPI list (prefix={perm_prefix})",
                  flush=True)
            services_out.append({
                "service": seg, "serviceCode": None, "version": None,
                "resourceTypes": types_for_seg, "resolved": False,
                "total": 0, "covered": 0, "notCovered": 0,
                "groups": [], "handlerOnly": sorted(by_action),
            })
            continue

        all_actions = set()
        groups_out = []
        for g in groups:
            apis_out = []
            for a in g.get("Apis", []):
                action = a["Action"]
                all_actions.add(action)
                apis_out.append({
                    "action": action,
                    "nameCn": a.get("NameCn", ""),
                    "covered": action in by_action,
                    "byTypes": by_action.get(action, []),
                })
            apis_out.sort(key=lambda x: x["action"])
            groups_out.append({"name": g.get("Name", ""), "apis": apis_out})

        covered = sum(1 for a in by_action if a in all_actions)
        total = len(all_actions)
        services_out.append({
            "service": seg,
            "serviceCode": code,
            "version": version,
            "resourceTypes": types_for_seg,
            "resolved": True,
            "total": total,
            "covered": covered,
            "notCovered": total - covered,
            "groups": groups_out,
        })
        print(f"  ok {seg:16} code={code:16} v={version} "
              f"total={total} covered={covered}", flush=True)

    out = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "services": services_out,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    resolved = sum(1 for s in services_out if s.get("resolved"))
    print(f"\nWrote {OUTPUT}: {resolved}/{len(services_out)} services resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
