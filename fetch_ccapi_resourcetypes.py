#!/usr/bin/env python3
"""Fetch all Volcengine CloudControl resource types and their metadata.

Calls two APIs that currently have no SDK/CLI:
  1. ListResourceTypes  -> enumerate every TypeName (paginated, MaxResults=500)
  2. DescribeResourceType -> fetch full metadata (Schema, etc.) per TypeName

Auth uses Volcengine Signature V4 (HMAC-SHA256), implemented inline with stdlib.

Env:
  ACCESS_KEY  Volcengine access key id
  SECRET_KEY  Volcengine secret access key (the base64-looking string from console)

Outputs:
  ccapi-resourcetypes.json         raw ListResourceTypes TypeList (array)
  ccapi-resourcetype-details.json  { TypeName: DescribeResourceType.Result }
"""

import concurrent.futures
import datetime
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# ---- Constants -------------------------------------------------------------
SERVICE = "cloudcontrol"
REGION = "cn-beijing"
HOST = f"{SERVICE}.{REGION}.volcengineapi.com"
ENDPOINT = "https://" + HOST
VERSION = "2025-06-01"

LIST_OUTPUT = "ccapi-resourcetypes.json"
DETAIL_OUTPUT = "ccapi-resourcetype-details.json"

MAX_RESULTS = 500
DESCRIBE_WORKERS = 8
HTTP_TIMEOUT = 30
MAX_RETRY = 5
RETRY_BACKOFF = 5  # seconds, multiplied by attempt number


# ---- Signature V4 ----------------------------------------------------------
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _canonical_query(params: dict) -> str:
    parts = []
    for k in sorted(params):
        ek = urllib.parse.quote(str(k), safe="-_.~")
        ev = urllib.parse.quote(str(params[k]), safe="-_.~")
        parts.append(ek + "=" + ev)
    return "&".join(parts)


def call_api(access_key: str, secret_key: str, action: str, query: dict) -> dict:
    """Signed GET request for an RPC-style Volcengine OpenAPI action."""
    body = b""
    payload_hash = hashlib.sha256(body).hexdigest()

    now = datetime.datetime.now(datetime.timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]

    q = dict(query)
    q["Action"] = action
    q["Version"] = VERSION
    canonical_query = _canonical_query(q)

    signed_headers = "host;x-content-sha256;x-date"
    canonical_headers = (
        f"host:{HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        "GET",
        "/",
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{short_date}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    k_date = _sign(secret_key.encode("utf-8"), short_date)
    k_region = _sign(k_date, REGION)
    k_service = _sign(k_region, SERVICE)
    k_signing = _sign(k_service, "request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    url = f"{ENDPOINT}/?{canonical_query}"
    req = urllib.request.Request(url, data=None, method="GET")
    req.add_header("Host", HOST)
    req.add_header("X-Date", x_date)
    req.add_header("X-Content-Sha256", payload_hash)
    req.add_header("Authorization", authorization)

    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            last_err = f"HTTP {e.code}: {detail}"
            # Don't retry client-side auth/param errors.
            if e.code in (400, 403, 404):
                raise RuntimeError(f"{action} failed -> {last_err}")
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt < MAX_RETRY:
            time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"{action} failed after {MAX_RETRY} tries -> {last_err}")


# ---- High-level workflow ---------------------------------------------------
def list_all_resource_types(ak: str, sk: str) -> list:
    types = []
    next_token = ""
    page = 0
    while True:
        page += 1
        query = {"MaxResults": MAX_RESULTS}
        if next_token:
            query["NextToken"] = next_token
        resp = call_api(ak, sk, "ListResourceTypes", query)
        result = resp.get("Result", {})
        batch = result.get("TypeList", []) or []
        types.extend(batch)
        next_token = result.get("NextToken", "") or ""
        print(f"  page {page}: +{len(batch)} (total {len(types)})", flush=True)
        if not next_token:
            break
    return types


def describe_one(ak: str, sk: str, type_name: str):
    try:
        resp = call_api(ak, sk, "DescribeResourceType", {"TypeName": type_name})
        return type_name, resp.get("Result", resp), None
    except Exception as e:  # noqa: BLE001
        return type_name, None, str(e)


def main() -> int:
    ak = os.environ.get("ACCESS_KEY", "").strip()
    sk = os.environ.get("SECRET_KEY", "").strip()
    if not ak or not sk:
        print("ERROR: ACCESS_KEY and SECRET_KEY env vars are required.", file=sys.stderr)
        return 1

    print("[1/2] Listing all resource types ...", flush=True)
    types = list_all_resource_types(ak, sk)
    type_names = [t.get("TypeName") for t in types if t.get("TypeName")]
    with open(LIST_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(types, f, ensure_ascii=False, indent=2)
    print(f"  -> {len(type_names)} types written to {LIST_OUTPUT}", flush=True)

    print(f"[2/2] Describing {len(type_names)} resource types ...", flush=True)
    details = {}
    errors = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=DESCRIBE_WORKERS) as pool:
        futures = [pool.submit(describe_one, ak, sk, tn) for tn in type_names]
        for fut in concurrent.futures.as_completed(futures):
            tn, result, err = fut.result()
            done += 1
            if err:
                errors[tn] = err
                print(f"  [{done}/{len(type_names)}] FAIL {tn}: {err}", flush=True)
            else:
                details[tn] = result
                print(f"  [{done}/{len(type_names)}] ok   {tn}", flush=True)

    with open(DETAIL_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2)
    print(f"  -> {len(details)} details written to {DETAIL_OUTPUT}", flush=True)

    if errors:
        print(f"\n{len(errors)} type(s) failed:", flush=True)
        for tn, err in errors.items():
            print(f"  - {tn}: {err}", flush=True)
    print("\nDone.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
