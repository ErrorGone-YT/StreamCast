"""Bridge to the vendored youtube-metadata-translator engine.

The engine lives in vendor/youtube-metadata-translator (a plain copy of
https://github.com/ErrorGone-YT/youtube-metadata-translator, replaceable at
runtime from the admin panel). Only yt_metadata_translator.py is imported —
its webui/pywebview UI stays untouched.

Everything the engine persists goes to <STORAGE_DIR>/yt_translator_data/ (the
engine's DATA_DIR is redirected at import time, so a vendored-folder update
never wipes OAuth tokens, providers or presets).
"""
import json
import os
import re
import sys
import time

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(BASE_DIR, "vendor", "youtube-metadata-translator")
ENGINE_MODULE = "yt_metadata_translator"
GITHUB_REPO = "ErrorGone-YT/youtube-metadata-translator"
GITHUB_BRANCH = "main"
DATA_DIRNAME = "yt_translator_data"

engine = None
load_error = None


def data_dir():
    return os.path.join(config.STORAGE_DIR, DATA_DIRNAME)


def _load():
    global engine, load_error
    if engine is not None:
        return engine
    if load_error is not None:
        return None
    if not os.path.isfile(os.path.join(VENDOR_DIR, ENGINE_MODULE + ".py")):
        load_error = "Engine not vendored (vendor/youtube-metadata-translator is missing)"
        return None
    if VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)
    try:
        import yt_metadata_translator as eng
    except Exception as e:  # ImportError or a dependency blow-up
        load_error = f"Engine import failed: {e}"
        return None
    os.makedirs(data_dir(), exist_ok=True)
    eng.DATA_DIR = data_dir()  # engine resolves data files at call time
    engine = eng
    return engine


def ready():
    """True when the engine can be used (vendored + deps importable)."""
    return _load() is not None


def status():
    """What the admin tile needs: readiness, channel connection, provider."""
    eng = _load()
    if eng is None:
        return {"ready": False, "error": load_error, "channel": None,
                "active_provider": None, "providers": []}
    channel = None
    try:
        profiles = eng.load_channel_profiles().get("profiles", [])
        if profiles:
            p = profiles[0]
            channel = {"id": p.get("channel_id"), "title": p.get("channel_title"),
                       "logo_url": p.get("logo_url", "")}
    except Exception:
        pass
    try:
        reg = eng.load_provider_registry()
        active = next((p for p in reg["providers"] if p["id"] == reg.get("active")), None)
        providers = [{"id": p["id"], "name": p.get("name"), "kind": p.get("kind"),
                      "auth": p.get("auth"), "base_url": p.get("base_url"),
                      "model": p.get("model"),
                      "keys": len(p.get("api_keys", []))} for p in reg["providers"]]
    except Exception:
        active = None
        providers = []
    return {"ready": True, "error": None, "channel": channel,
            "active_provider": active.get("name") if active else None,
            "providers": providers}


def has_secrets():
    return os.path.isfile(os.path.join(data_dir(), "client_secrets.json"))


def save_secrets(raw_bytes):
    """Store an uploaded OAuth client secrets file (Desktop-app JSON)."""
    eng = _load()
    if eng is None:
        raise RuntimeError(load_error)
    data = json.loads(raw_bytes.decode("utf-8-sig"))
    if "installed" not in data and "web" not in data:
        raise ValueError("Not an OAuth client secrets file (no 'installed'/'web' key)")
    path = os.path.join(data_dir(), "client_secrets.json")
    with open(path, "wb") as f:
        f.write(raw_bytes)
    return path


def language_catalog():
    eng = _load()
    if eng is None:
        return {}
    try:
        return eng.available_language_catalog()
    except Exception:
        return {}


def language_presets():
    """Named language presets saved in the translator's settings UI."""
    path = os.path.join(data_dir(), "ui_settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f).get("language_presets") or {})
    except (OSError, ValueError):
        return {}


def save_preset(name, codes):
    """Create/update a named language preset in ui_settings.json."""
    if not name:
        raise ValueError("Preset name is required")
    path = os.path.join(data_dir(), "ui_settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("language_presets", {})[name] = sorted(set(codes))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def delete_preset(name):
    path = os.path.join(data_dir(), "ui_settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    (data.get("language_presets") or {}).pop(name, None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parallelism():
    eng = _load()
    if eng is None:
        return {"value": "auto", "suggested": 1}
    cfg = eng.load_local_llm_config()
    return {"value": cfg.get("max_parallel_languages", "auto"),
            "suggested": eng.suggested_parallelism(cfg)}


def set_parallelism(value):
    eng = _load()
    if eng is None:
        raise RuntimeError(load_error)
    cfg = eng.load_local_llm_config()
    cfg["max_parallel_languages"] = "auto" if str(value) == "auto" else max(1, int(value))
    eng.save_json_file("local_llm.json", cfg)
    return cfg["max_parallel_languages"]


def provider_registry():
    eng = _load()
    return eng.load_provider_registry() if eng else {"active": None, "providers": []}


def provider_registry_view():
    """Registry with masked keys, safe to send to the browser."""
    reg = provider_registry()
    view = {"active": reg.get("active"), "backup": reg.get("backup"), "providers": []}
    for p in reg.get("providers", []):
        item = dict(p)
        item.pop("api_keys", None)
        item["key_count"] = len(p.get("api_keys", []))
        item["keys_masked"] = [_mask_key(k) for k in p.get("api_keys", [])]
        view["providers"].append(item)
    return view


def _mask_key(key):
    key = str(key)
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:4] + "***" + key[-4:]


def save_provider_registry(reg):
    eng = _load()
    if eng is None:
        raise RuntimeError(load_error)
    eng.save_provider_registry(reg)


def check_provider_keys(provider_id):
    """Validate each API key of a provider (ok / frozen / dead). Ported from
    the translator's web UI so key status is visible in StreamCast too."""
    import requests
    from concurrent.futures import ThreadPoolExecutor
    eng = _load()
    if eng is None:
        raise RuntimeError(load_error)
    reg = eng.load_provider_registry()
    provider = next((p for p in reg["providers"] if p["id"] == provider_id), None)
    if provider is None:
        raise ValueError("provider not found")
    keys = provider.get("api_keys", [])
    base = (provider.get("base_url") or "").rstrip("/")

    def test(key):
        try:
            if provider.get("kind") == "gemini":
                response = requests.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": key}, timeout=10)
            else:
                response = requests.get(f"{base}/models",
                                        headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if response.status_code == 200:
                return "ok", ""
            if response.status_code == 429:
                return "frozen", "HTTP 429"
            return "dead", f"HTTP {response.status_code}"
        except Exception as error:
            return "dead", str(error)[:80]

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(keys)))) as pool:
        verdicts = list(pool.map(test, keys))
    return [{"masked": _mask_key(k), "status": s, "detail": d}
            for k, (s, d) in zip(keys, verdicts)]


_yt_client = None  # cached authorized client (credentials refresh themselves)


def get_youtube_client():
    """Authorized YouTube client for the connected channel, or None."""
    global _yt_client
    if _yt_client is not None:
        return _yt_client
    eng = _load()
    if eng is None:
        return None
    try:
        profiles = eng.load_channel_profiles().get("profiles", [])
        if not profiles:
            return None
        _yt_client = eng.authenticate(profiles[0])
        return _yt_client
    except Exception:
        return None


def fetch_video_meta(video_id):
    """Current title/description of a video (for the 'from video' source)."""
    eng = _load()
    youtube = get_youtube_client()
    if youtube is None:
        raise RuntimeError("YouTube channel is not connected")
    meta = eng.fetch_video_source_metadata(youtube, video_id)
    if meta is None:
        raise ValueError("Video not found")
    return meta


def update_video_localizations(video_id, source_title, source_description,
                               source_language, localizations):
    """Write localized title/description back to the YouTube video."""
    eng = _load()
    youtube = get_youtube_client()
    if youtube is None:
        raise RuntimeError("YouTube channel is not connected")
    merged = dict(localizations)
    if source_language:
        merged[source_language] = {"title": source_title,
                                   "description": source_description}
    return eng.update_video_metadata(youtube, video_id,
                                     source_title, source_description, merged)


def run_translation(source_title, source_description, languages, parts,
                    progress=None):
    """Blocking translation — run from a background thread.

    Unlike the engine's all-or-nothing localize_metadata_via_llm(), this keeps
    every language that completed and returns partial failures instead of
    raising. parts: 'all' | 'title' | 'description'.
    Returns {"localizations": {lang: {title, description}}, "errors": [str]}."""
    eng = _load()
    if eng is None:
        raise RuntimeError(load_error)
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = eng.load_local_llm_config()
    provider = eng.get_active_provider()
    if provider is None:
        raise RuntimeError("No translation provider configured.")
    names = cfg.get("language_names", {})
    # 'en' is the assumed source language: pass the source text through.
    localizations = {"en": {"title": source_title, "description": source_description}} \
        if "en" in languages else {}
    queued = [c for c in dict.fromkeys(languages) if c and c != "en"]
    errors = []

    if queued:
        workers = min(len(queued), eng.resolve_parallelism(cfg))

        def one(idx, code):
            # Stagger starts so all threads don't slam the model at once.
            _time.sleep(idx * 1.5)
            return eng.localize_language_via_llm(
                provider, cfg, code, names.get(code, code),
                source_title, source_description, progress=progress)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, i, c): c for i, c in enumerate(queued)}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    localizations[code] = future.result()
                except Exception as error:
                    errors.append(f"{code}: {str(error).splitlines()[0]}")

    if parts == "title":
        for texts in localizations.values():
            texts["description"] = source_description
    elif parts == "description":
        for texts in localizations.values():
            texts["title"] = source_title

    return {"localizations": localizations, "errors": errors}


def _vendored_marker():
    """Version marker of the vendored copy, if recorded."""
    try:
        with open(os.path.join(data_dir(), "vendored_version.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def vendored_version():
    marker = _vendored_marker()
    if marker:
        return marker
    return {"sha": None, "date": None}


def latest_version(timeout=10):
    """Latest commit (sha short + date) of the translator's main branch."""
    import requests
    response = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}",
        timeout=timeout,
        headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    data = response.json()
    return {"sha": data["sha"][:7],
            "date": data["commit"]["committer"]["date"]}


def update_vendor(progress=None):
    """Replace the vendored folder with the latest main-branch zip. Runtime
    data is untouched (it lives in storage/yt_translator_data)."""
    global engine, load_error
    import io
    import shutil
    import tempfile
    import zipfile
    import requests

    def note(msg):
        if progress:
            progress(msg)

    url = f"https://codeload.github.com/{GITHUB_REPO}/zip/refs/heads/{GITHUB_BRANCH}"
    note(f"Downloading {url}")
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    info = latest_version()

    note("Extracting")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            zf.extractall(tmp)
        extracted = next(
            os.path.join(tmp, name) for name in os.listdir(tmp)
            if name.startswith(GITHUB_REPO.split("/")[1] + "-"))
        parent = os.path.dirname(VENDOR_DIR)
        os.makedirs(parent, exist_ok=True)
        staging = os.path.join(parent, ".vendor-staging")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(extracted, staging)
        shutil.rmtree(VENDOR_DIR, ignore_errors=True)
        os.replace(staging, VENDOR_DIR)

    with open(os.path.join(data_dir(), "vendored_version.json"), "w",
              encoding="utf-8") as f:
        json.dump(info, f)
    note(f"Updated to {info['sha']}")
    return info
