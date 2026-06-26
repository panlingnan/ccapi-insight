"""Vercel serverless function: GET /api/apiparams

Proxies the Volcengine API Explorer swagger for one action, flattens nested
params to leaf attributes, and flags pagination/control (non-attribute) params.
Stdlib only; needs no credentials.
"""

import json
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

EXPLORER = "https://api.volcengine.com/api/common/explorer"

NON_ATTR_PARAMS = {
    # pagination
    "nexttoken", "maxresults", "maxitems", "pagenumber", "pagesize",
    "pagenum", "pageno", "pagesizes", "limit", "offset", "marker",
    "pageindex", "count",
    # idempotency / control
    "clienttoken", "dryrun", "requestid", "token",
}


def _http_get_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _deref(schema, comps, seen):
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
    url = (f"{EXPLORER}/api-swagger?ServiceCode={urllib.parse.quote(code)}"
           f"&Version={urllib.parse.quote(version)}"
           f"&APIVersion={urllib.parse.quote(version)}"
           f"&ActionName={urllib.parse.quote(action)}")
    data = _http_get_json(url)
    api = (data.get("Result") or {}).get("Api") or {}
    method, leaves = _extract_params(api)
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
    return {"action": action, "method": method, "params": params}


class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (qs.get("serviceCode") or [""])[0].strip()
        version = (qs.get("version") or [""])[0].strip()
        action = (qs.get("action") or [""])[0].strip()
        if not code or not version or not action:
            self._send({"error": "serviceCode, version, action are required"}, 400)
            return
        try:
            self._send(fetch_api_params(code, version, action))
        except Exception as e:  # noqa: BLE001
            self._send({"error": f"获取 OpenAPI 参数失败：{e}", "action": action}, 502)
