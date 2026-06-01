import json
import os
import secrets
import string
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from flask import Flask, jsonify, request, render_template, Response, stream_with_context

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"

DEFAULT_CONFIG = {
    "providers": [],
    "model_routes": {},
    "api_keys": [],
}

DEFAULT_STATS = {"total_requests": 0, "requests_by_model": {}, "requests_by_provider": {}}


def load_config():
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
        cfg.setdefault("api_keys", [])
        return cfg
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def load_stats():
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text())
    return dict(DEFAULT_STATS)


def save_stats(stats):
    STATS_FILE.write_text(json.dumps(stats, indent=2))


def generate_sk_key():
    chars = string.ascii_letters + string.digits
    secret = ''.join(secrets.choice(chars) for _ in range(48))
    return f"sk-{secret}"


def resolve_gateway_key(raw_key, provider):
    """If raw_key matches a gateway key, return the real provider key. Otherwise return as-is."""
    cfg = load_config()
    for gk in cfg.get("api_keys", []):
        if gk["key"] == raw_key:
            return provider["api_key"]
    return raw_key


def find_provider_from_key(raw_key):
    """Find allowed models by gateway API key. Returns allowed_models list or None."""
    cfg = load_config()
    for gk in cfg.get("api_keys", []):
        if gk["key"] == raw_key:
            return gk.get("models", [])  # empty = allow all
    return None


def record_stats(model, provider_name):
    stats = load_stats()
    stats["total_requests"] = stats.get("total_requests", 0) + 1
    rbm = stats.get("requests_by_model", {})
    rbm[model] = rbm.get(model, 0) + 1
    stats["requests_by_model"] = rbm
    rbp = stats.get("requests_by_provider", {})
    rbp[provider_name] = rbp.get(provider_name, 0) + 1
    stats["requests_by_provider"] = rbp
    save_stats(stats)


def find_provider_for_model(model):
    cfg = load_config()
    route = cfg.get("model_routes", {}).get(model)
    if route:
        for p in cfg["providers"]:
            if p["name"] == route and p.get("status") == "active":
                return p
    active = [p for p in cfg["providers"] if p.get("status") == "active"]
    if active:
        return sorted(active, key=lambda x: x.get("priority", 99))[0]
    return None


def build_upstream_url(base, path, service_type):
    """Build upstream URL, avoiding double /v1 or adding /v1 when URL already has a version."""
    import re
    base = base.rstrip("/")
    # Strip leading /v1 from path since we handle it here
    sub_path = path[3:] if path.startswith("/v1/") else path  # e.g. /chat/completions

    if service_type == "anthropic":
        if base.endswith("/v1"):
            return f"{base}{sub_path}"
        return f"{base}/v1{sub_path}"
    else:
        # If base already ends with a version suffix like /v1, /v2, /v3, /v4 etc.
        if re.search(r'/v\d+$', base):
            return f"{base}{sub_path}"
        return f"{base}/v1{sub_path}"


# ---- Web GUI ----

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/providers", methods=["GET"])
def list_providers():
    cfg = load_config()
    providers = []
    for p in cfg.get("providers", []):
        masked = {**p}
        if "api_key" in masked:
            key = masked["api_key"]
            masked["api_key_masked"] = key[:8] + "****" + key[-4:] if len(key) > 12 else "****"
            del masked["api_key"]
        providers.append(masked)
    return jsonify(providers)


@app.route("/api/providers", methods=["POST"])
def add_provider():
    data = request.json
    cfg = load_config()
    provider = {
        "name": data.get("name", f"provider-{uuid.uuid4().hex[:6]}"),
        "base_url": data.get("base_url", "").rstrip("/"),
        "api_key": data.get("api_key", ""),
        "service_type": data.get("service_type", "openai"),
        "priority": data.get("priority", 0),
        "status": "active",
        "created_at": datetime.now().isoformat(),
    }
    cfg["providers"].append(provider)
    save_config(cfg)
    return jsonify({"ok": True, "name": provider["name"]})


@app.route("/api/providers/<name>", methods=["PUT"])
def update_provider(name):
    data = request.json
    cfg = load_config()
    for i, p in enumerate(cfg["providers"]):
        if p["name"] == name:
            for key in ["base_url", "api_key", "service_type", "priority", "status"]:
                if key in data:
                    cfg["providers"][i][key] = data[key]
            if "name" in data and data["name"] != name:
                new_name = data["name"]
                cfg["providers"][i]["name"] = new_name
                for model, prov in list(cfg.get("model_routes", {}).items()):
                    if prov == name:
                        cfg["model_routes"][model] = new_name
                # No provider field in keys anymore, skip
            save_config(cfg)
            return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404


@app.route("/api/providers/<name>", methods=["DELETE"])
def delete_provider(name):
    cfg = load_config()
    cfg["providers"] = [p for p in cfg["providers"] if p["name"] != name]
    cfg["model_routes"] = {m: p for m, p in cfg.get("model_routes", {}).items() if p != name}
    save_config(cfg)
    return jsonify({"ok": True})


# ---- Gateway API Keys ----

@app.route("/api/keys", methods=["GET"])
def list_keys():
    cfg = load_config()
    return jsonify(cfg.get("api_keys", []))


@app.route("/api/keys", methods=["POST"])
def create_key():
    data = request.json
    cfg = load_config()
    gk = {
        "key": generate_sk_key(),
        "name": data.get("name", ""),
        "models": data.get("models", []),  # allowed models, empty = all
        "created_at": datetime.now().isoformat(),
    }
    cfg.setdefault("api_keys", []).append(gk)
    save_config(cfg)
    return jsonify({"ok": True, "key": gk["key"], "name": gk["name"]})


@app.route("/api/keys/delete", methods=["POST"])
def delete_key():
    data = request.json
    key_to_delete = data.get("key", "")
    cfg = load_config()
    cfg["api_keys"] = [k for k in cfg.get("api_keys", []) if k["key"] != key_to_delete]
    save_config(cfg)
    return jsonify({"ok": True})


# ---- Model Routes ----

@app.route("/api/routes", methods=["POST"])
def set_route():
    data = request.json
    cfg = load_config()
    cfg.setdefault("model_routes", {})
    cfg["model_routes"][data["model"]] = data["provider"]
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/routes/<model>", methods=["DELETE"])
def delete_route(model):
    cfg = load_config()
    cfg.get("model_routes", {}).pop(model, None)
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/stats", methods=["GET"])
def get_stats():
    return jsonify(load_stats())


# ---- Scan & Batch ----

@app.route("/api/providers/<name>/models", methods=["GET"])
def scan_models(name):
    cfg = load_config()
    provider = next((p for p in cfg["providers"] if p["name"] == name), None)
    if not provider:
        return jsonify({"error": "not found"}), 404
    base = provider["base_url"].rstrip("/")
    svc = provider.get("service_type", "openai")
    headers = {"Authorization": f"Bearer {provider['api_key']}"}

    import re
    has_version = bool(re.search(r'/v\d+$', base))

    if has_version:
        candidates = [f"{base}/models"]
    elif svc == "anthropic":
        candidates = [f"{base}/v1/models"]
    else:
        candidates = [f"{base}/v1/models", f"{base}/models"]

    last_err = None
    for url in candidates:
        try:
            r = httpx.get(url, headers=headers, timeout=15)
            if r.status_code >= 400:
                last_err = f"{r.status_code} from {url}"
                continue
            body = r.json()
            models = []
            for m in body.get("data", body if isinstance(body, list) else []):
                mid = m.get("id", "") if isinstance(m, dict) else str(m)
                if mid:
                    models.append(mid)
            models = sorted(set(models))
            return jsonify({"ok": True, "models": models, "url": url})
        except Exception as e:
            last_err = f"{url}: {e}"
            continue
    return jsonify({"ok": False, "error": last_err or "all endpoints failed"})


@app.route("/api/providers/<name>/routes", methods=["POST"])
def batch_add_routes(name):
    data = request.json
    models = data.get("models", [])
    cfg = load_config()
    cfg.setdefault("model_routes", {})
    for m in models:
        cfg["model_routes"][m] = name
    save_config(cfg)
    return jsonify({"ok": True, "count": len(models)})


@app.route("/api/test/<name>", methods=["POST"])
def test_provider(name):
    cfg = load_config()
    provider = next((p for p in cfg["providers"] if p["name"] == name), None)
    if not provider:
        return jsonify({"error": "not found"}), 404
    base = provider["base_url"].rstrip("/")
    svc = provider.get("service_type", "openai")
    try:
        if svc == "anthropic":
            url = build_upstream_url(base, "/v1/messages", "anthropic")
            headers = {
                "x-api-key": provider["api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {"model": "claude-sonnet-4-20250514", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        else:
            url = build_upstream_url(base, "/v1/chat/completions", "openai")
            headers = {"Authorization": f"Bearer {provider['api_key']}", "content-type": "application/json"}
            body = {"model": "gpt-4o-mini", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        r = httpx.post(url, json=body, headers=headers, timeout=30)
        return jsonify({"status": r.status_code, "ok": r.status_code < 400, "body": r.text[:500]})
    except Exception as e:
        return jsonify({"status": 0, "ok": False, "error": str(e)})


# ---- API Proxy ----

@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/v1/completions", methods=["POST"])
@app.route("/v1/embeddings", methods=["POST"])
@app.route("/v1/images/generations", methods=["POST"])
@app.route("/v1/audio/transcriptions", methods=["POST"])
@app.route("/v1/audio/translations", methods=["POST"])
def proxy_openai():
    data = request.json
    model = data.get("model", "")

    # Get auth header, try gateway key first then model route
    raw_key = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_key = auth_header[7:]

    allowed_models = find_provider_from_key(raw_key)
    if allowed_models is not None and allowed_models and model not in allowed_models:
        return jsonify({"error": {"message": f"Key not authorized for model: {model}", "type": "forbidden"}}), 403

    provider = find_provider_for_model(model)
    if not provider:
        return jsonify({"error": {"message": f"No provider for model: {model}", "type": "not_found"}}), 404

    real_key = resolve_gateway_key(raw_key, provider)
    url = build_upstream_url(provider["base_url"], request.path, provider.get("service_type", "openai"))
    headers = {
        "Authorization": f"Bearer {real_key}",
        "content-type": "application/json",
    }
    for h in ["openai-organization", "openai-project"]:
        if h in request.headers:
            headers[h] = request.headers[h]

    streaming = data.get("stream", False)
    record_stats(model, provider["name"])

    if streaming:
        def generate():
            with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as client:
                with client.stream("POST", url, json=data, headers=headers) as r:
                    for chunk in r.iter_bytes(8192):
                        yield chunk
        return Response(stream_with_context(generate()), content_type="text/event-stream")
    else:
        r = httpx.post(url, json=data, headers=headers, timeout=120)
        return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type", "application/json"))


@app.route("/v1/models", methods=["GET"])
def list_models():
    cfg = load_config()
    models = []
    seen = set()
    for model, prov_name in cfg.get("model_routes", {}).items():
        if model not in seen:
            models.append({"id": model, "object": "model", "owned_by": prov_name, "provider": prov_name})
            seen.add(model)
    return jsonify({"object": "list", "data": models})


@app.route("/v1/messages", methods=["POST"])
def proxy_anthropic():
    data = request.json
    model = data.get("model", "")

    # Accept key from either x-api-key or Authorization header
    raw_key = request.headers.get("x-api-key", "")
    if not raw_key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            raw_key = auth[7:]

    allowed_models = find_provider_from_key(raw_key)
    if allowed_models is not None and allowed_models and model not in allowed_models:
        return jsonify({"error": {"message": f"Key not authorized for model: {model}", "type": "forbidden"}}), 403

    provider = find_provider_for_model(model)
    if not provider:
        return jsonify({"error": {"message": f"No provider for model: {model}", "type": "not_found"}}), 404

    real_key = resolve_gateway_key(raw_key, provider)
    svc = provider.get("service_type", "openai")

    if svc == "anthropic":
        url = build_upstream_url(provider["base_url"], "/v1/messages", "anthropic")
        # Send both auth formats so it works regardless of upstream
        headers = {
            "x-api-key": real_key,
            "Authorization": f"Bearer {real_key}",
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
            "content-type": "application/json",
        }
    else:
        url = build_upstream_url(provider["base_url"], "/v1/chat/completions", "openai")
        headers = {
            "Authorization": f"Bearer {real_key}",
            "content-type": "application/json",
        }
        messages = []
        for msg in data.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            messages.append({"role": msg["role"], "content": content})
        data = {"model": model, "messages": messages, "max_tokens": data.get("max_tokens", 4096),
                "stream": data.get("stream", False)}

    streaming = data.get("stream", False)
    record_stats(model, provider["name"])

    if streaming:
        def generate():
            with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as client:
                with client.stream("POST", url, json=data, headers=headers) as r:
                    for chunk in r.iter_bytes(8192):
                        yield chunk
        return Response(stream_with_context(generate()), content_type="text/event-stream")
    else:
        r = httpx.post(url, json=data, headers=headers, timeout=120)
        return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type", "application/json"))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=3000, debug=False)
