#!/usr/bin/env python3
"""Local server for the CloudControl coverage UI.

Serves the static page and exposes a refresh endpoint that re-runs the real
data pipeline (no mock data):

  POST /api/refresh   (Server-Sent Events stream)
    step 1: fetch_ccapi_resourcetypes.py  -> calls CloudControl OpenAPI
            (ListResourceTypes + DescribeResourceType) using ACCESS_KEY/SECRET_KEY
    step 2: build_coverage_data.py        -> calls Volcengine API Explorer
            (the volcengine-api skill data source) and recomputes coverage

Requires ACCESS_KEY / SECRET_KEY in the environment.

Usage:
  export ACCESS_KEY=...   export SECRET_KEY=...
  python3 server.py            # http://127.0.0.1:8765
"""

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))

EXPLORER = "https://api.volcengine.com/api/common/explorer"

# Request params that are not resource attributes: pagination, idempotency,
# and other control knobs. Matched case-insensitively against the leaf name.
NON_ATTR_PARAMS = {
    # pagination
    "nexttoken", "maxresults", "maxitems", "pagenumber", "pagesize",
    "pagenum", "pageno", "pagesizes", "limit", "offset", "marker",
    "pageindex", "count",
    # idempotency / control
    "clienttoken", "dryrun", "requestid", "token",
}

# In-memory cache: (serviceCode, version, action) -> result dict
_PARAM_CACHE = {}

PIPELINE = [
    ("fetch", "调用 CloudControl API 获取全部资源类型及元信息",
     [sys.executable, "-u", "fetch_ccapi_resourcetypes.py"]),
    ("build", "调用 API Explorer 拉取各服务全量 OpenAPI 并计算覆盖率",
     [sys.executable, "-u", "build_coverage_data.py"]),
]


# ---- API Explorer swagger -> flat param list -------------------------------
def _http_get_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _deref(schema, comps, seen):
    """Resolve a single $ref against components.schemas, guarding recursion."""
    if isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        if name in seen:
            return {"type": "object", "properties": {}}, seen
        seen = seen | {name}
        target = comps.get(name, {}) or {}
        merged = dict(target)
        for k, v in schema.items():
            if k != "$ref":
                merged.setdefault(k, v)
        return merged, seen
    return schema, seen


def _flatten(name, schema, comps, required, path, out, seen, depth=0):
    """Walk a JSON schema, emitting one entry per leaf (scalar) parameter.

    Nested objects/arrays are expanded; the dotted path records the nesting
    while `name` is the leaf attribute name used for matching.
    """
    if depth > 8 or not isinstance(schema, dict):
        return
    schema, seen = _deref(schema, comps, seen)
    t = schema.get("type")
    if "properties" in schema or t == "object":
        props = schema.get("properties", {}) or {}
        req = set(schema.get("required", []) or [])
        if not props:
            out.append({"name": name, "type": "object", "required": required,
                        "description": schema.get("description", ""),
                        "path": list(path)})
            return
        for pn, ps in props.items():
            _flatten(pn, ps, comps, pn in req, path + [pn], out, seen, depth + 1)
    elif t == "array" or "items" in schema:
        items = schema.get("items", {}) or {}
        _flatten(name, items, comps, required, path, out, seen, depth + 1)
    else:
        out.append({"name": name, "type": t or "string", "required": required,
                    "description": schema.get("description", ""),
                    "path": list(path)})


def _extract_params(api):
    """Return (httpMethod, [leaf param dicts]) from a swagger doc."""
    comps = (api.get("components") or {}).get("schemas", {}) or {}
    paths = api.get("paths") or {}
    if not paths:
        return "?", []
    pkey = next(iter(paths))
    node = paths[pkey] or {}
    method = "post" if "post" in node else "get"
    m = node.get(method, {}) or {}
    out = []
    if method == "get":
        for p in m.get("parameters", []) or []:
            nm = p.get("name")
            sch = p.get("schema", {}) or {}
            _flatten(nm, sch, comps, bool(p.get("required")), [nm], out, set())
    else:
        rb = m.get("requestBody", {}) or {}
        sch = (((rb.get("content") or {}).get("application/json") or {})
               .get("schema")) or {}
        _flatten("", sch, comps, False, [], out, set())
    return method, out


def fetch_api_params(code, version, action):
    """Fetch one API's swagger and return flattened params (cached)."""
    cache_key = (code, version, action)
    if cache_key in _PARAM_CACHE:
        return _PARAM_CACHE[cache_key]
    url = (f"{EXPLORER}/api-swagger?ServiceCode={urllib.parse.quote(code)}"
           f"&Version={urllib.parse.quote(version)}"
           f"&APIVersion={urllib.parse.quote(version)}"
           f"&ActionName={urllib.parse.quote(action)}")
    data = _http_get_json(url)
    api = (data.get("Result") or {}).get("Api") or {}
    method, leaves = _extract_params(api)
    # de-dupe by dotted path, keep first occurrence
    seen_paths = set()
    params = []
    for lf in leaves:
        key = ".".join(lf["path"]) or lf["name"]
        if key in seen_paths:
            continue
        seen_paths.add(key)
        nm = lf["name"] or ""
        params.append({
            "name": nm,
            "path": ".".join(lf["path"]) or nm,
            "type": lf["type"],
            "required": lf["required"],
            "description": lf["description"],
            "isMeta": nm.lower() in NON_ATTR_PARAMS,
        })
    result = {"action": action, "method": method, "params": params}
    _PARAM_CACHE[cache_key] = result
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_POST(self):
        if self.path == "/api/refresh":
            self._refresh()
        elif self.path == "/api/exclusions":
            self._save_exclusions()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/apiparams":
            self._api_params(urllib.parse.parse_qs(parsed.query))
        else:
            super().do_GET()

    def _save_exclusions(self):
        """Persist the excluded-API list to a committed JSON file (local admin).

        The deployed site is read-only; this endpoint only exists on the local
        dev server so an admin's exclusions become part of the repo and survive
        deploys.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"[]"
            data = json.loads(raw.decode("utf-8") or "[]")
            if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                self._send_json({"error": "expected a JSON array of strings"}, 400)
                return
            cleaned = sorted(set(data))
            path = os.path.join(BASE, "excluded-apis.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            self._send_json({"ok": True, "count": len(cleaned)})
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"保存排除列表失败：{e}"}, 500)

    def _send_json(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _api_params(self, qs):
        code = (qs.get("serviceCode") or [""])[0].strip()
        version = (qs.get("version") or [""])[0].strip()
        action = (qs.get("action") or [""])[0].strip()
        if not code or not version or not action:
            self._send_json(
                {"error": "serviceCode, version, action are required"}, 400)
            return
        try:
            result = fetch_api_params(code, version, action)
            self._send_json(result)
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"error": f"获取 OpenAPI 参数失败：{e}", "action": action}, 502)

    def _sse_headers(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _emit(self, obj):
        payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _refresh(self):
        self._sse_headers()
        try:
            ak = os.environ.get("ACCESS_KEY", "").strip()
            sk = os.environ.get("SECRET_KEY", "").strip()
            if not ak or not sk:
                self._emit({"phase": "error",
                            "message": "服务器未设置 ACCESS_KEY / SECRET_KEY 环境变量，"
                                       "请在启动 server.py 前 export 这两个变量。"})
                return

            for key, label, cmd in PIPELINE:
                self._emit({"phase": "step", "step": key, "message": label})
                proc = subprocess.Popen(
                    cmd, cwd=BASE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=os.environ, text=True, bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line:
                        self._emit({"phase": "log", "step": key, "line": line})
                proc.wait()
                if proc.returncode != 0:
                    self._emit({"phase": "error", "step": key,
                                "message": f"步骤 {key} 失败（退出码 {proc.returncode}）"})
                    return

            summary = self._summary()
            self._emit({"phase": "done", "message": "刷新完成", "summary": summary})
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-stream
        except Exception as e:  # noqa: BLE001
            try:
                self._emit({"phase": "error", "message": f"服务器异常：{e}"})
            except Exception:  # noqa: BLE001
                pass

    def _summary(self):
        try:
            with open(os.path.join(BASE, "coverage-data.json"), encoding="utf-8") as f:
                data = json.load(f)
            svcs = data.get("services", [])
            total = sum(s.get("total", 0) for s in svcs)
            covered = sum(s.get("covered", 0) for s in svcs)
            return {"services": len(svcs), "totalApis": total,
                    "covered": covered, "generatedAt": data.get("generatedAt")}
        except Exception:  # noqa: BLE001
            return {}


def main():
    os.chdir(BASE)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    have_keys = bool(os.environ.get("ACCESS_KEY") and os.environ.get("SECRET_KEY"))
    print(f"Serving on http://127.0.0.1:{PORT}  (refresh {'ENABLED' if have_keys else 'DISABLED: set ACCESS_KEY/SECRET_KEY'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
