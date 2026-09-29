import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request


def request(method, base, path, auth, body=None, timeout=60):
    data = None
    headers = {"Authorization": auth}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {raw[:1000]}") from exc


def text_request(method, base, path, auth, body=None, timeout=60):
    data = None
    headers = {"Authorization": auth}
    if body is not None:
        data = body.encode("utf-8")
        headers["Content-Type"] = "application/x-ndjson"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {raw[:1000]}") from exc


def make_auth(user, password):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def cleaned_settings(index_settings):
    idx = dict(index_settings.get("index", {}))
    for key in [
        "uuid",
        "version",
        "creation_date",
        "provided_name",
        "routing",
        "store",
        "history",
        "lifecycle",
    ]:
        idx.pop(key, None)
    return {"index": idx}


def ensure_index(source, target, auth, index):
    try:
        request("DELETE", target, f"/{index}", auth)
        print(f"deleted target index {index}")
    except RuntimeError as exc:
        if "404" not in str(exc):
            raise

    info = request("GET", source, f"/{index}", auth)
    meta = info[index]
    body = {
        "settings": cleaned_settings(meta.get("settings", {})),
        "mappings": meta.get("mappings", {}),
    }
    request("PUT", target, f"/{index}", auth, body)
    print(f"created target index {index}")


def bulk_send(target, auth, actions):
    if not actions:
        return 0
    payload = "\n".join(actions) + "\n"
    res = text_request("POST", target, "/_bulk", auth, payload, timeout=180)
    parsed = json.loads(res)
    if parsed.get("errors"):
        failures = [item for item in parsed.get("items", []) if item.get("index", {}).get("error")]
        raise RuntimeError(f"bulk had errors: {json.dumps(failures[:3], ensure_ascii=False)[:1200]}")
    return len(actions) // 2


def copy_index(source, target, auth, index, batch_size):
    ensure_index(source, target, auth, index)

    search_body = {"size": batch_size, "query": {"match_all": {}}, "sort": ["_doc"]}
    page = request("POST", source, f"/{index}/_search?scroll=5m", auth, search_body, timeout=180)
    scroll_id = page.get("_scroll_id")
    total = 0

    while True:
        hits = page.get("hits", {}).get("hits", [])
        if not hits:
            break
        actions = []
        for hit in hits:
            actions.append(json.dumps({"index": {"_index": index, "_id": hit["_id"]}}, ensure_ascii=False))
            actions.append(json.dumps(hit.get("_source", {}), ensure_ascii=False))
        total += bulk_send(target, auth, actions)
        print(f"{index}: copied {total}")
        page = request("POST", source, "/_search/scroll", auth, {"scroll": "5m", "scroll_id": scroll_id}, timeout=180)
        scroll_id = page.get("_scroll_id", scroll_id)

    if scroll_id:
        try:
            request("DELETE", source, "/_search/scroll", auth, {"scroll_id": [scroll_id]})
        except Exception:
            pass
    request("POST", target, f"/{index}/_refresh", auth)
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="http://192.168.1.246:12100")
    parser.add_argument("--target", default="http://192.168.1.246:12102")
    parser.add_argument("--user", default="elastic")
    parser.add_argument("--password", default="infini_rag_flow")
    parser.add_argument("--prefix", default="ragflow")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    auth = make_auth(args.user, args.password)
    source_health = request("GET", args.source, "/", auth)
    target_health = request("GET", args.target, "/", auth)
    print(f"source={source_health.get('version', {}).get('number')} target={target_health.get('version', {}).get('number')}")

    indices = request("GET", args.source, "/_cat/indices?format=json&h=index,docs.count,store.size", auth)
    names = [row["index"] for row in indices if row["index"].startswith(args.prefix)]
    if not names:
        print("no ragflow indices found")
        return 1

    grand_total = 0
    started = time.time()
    for name in sorted(names):
        print(f"copying {name}")
        grand_total += copy_index(args.source, args.target, auth, name, args.batch_size)

    elapsed = time.time() - started
    print(f"done indices={len(names)} docs={grand_total} elapsed={elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
