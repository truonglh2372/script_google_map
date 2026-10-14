"""Zero-shot CLIP photo filter for restaurant uploads.

Keeps: food, drink, menu, storefront/sign, venue interior.
Drops: people-selfie, child, document/ID, receipt, animal, vehicle, screenshot/TikTok UI, other.
Conservative: reject only when a reject class clearly wins, so real food photos stay.

Env:
  AQD_CLIP=0     disable (tab filter only)
  AQD_CLIP_DEVICE=cpu|cuda|mps  (default cpu)
"""
from __future__ import annotations

import io, os, threading, time, urllib.request
from typing import Optional

ALLOWED = (
    "food", "drink", "menu", "storefront_or_sign", "interior",
)
REJECT = (
    "people_selfie", "child", "document_id", "receipt",
    "animal", "vehicle", "screenshot", "other",
)

# English prompts work better with CLIP ViT-B-32
PROMPTS = {
    "food": "a photo of cooked food or a meal on a plate or table",
    "drink": "a photo of a drink coffee tea juice beer or beverage in a cup or glass",
    "menu": "a photo of a restaurant menu board or printed menu with prices",
    "storefront_or_sign": "a photo of a restaurant storefront exterior facade or shop signboard with the business name",
    "interior": "a photo of a restaurant or cafe interior dining room seating or vibe",
    "people_selfie": "a selfie or close-up photo focused on a person's face or people posing",
    "child": "a photo focused on a child or children",
    "document_id": "a photo of an ID card passport identity document or official paperwork",
    "receipt": "a photo of a paper receipt bill or invoice",
    "animal": "a photo of a pet dog cat or animal",
    "vehicle": "a photo of a car motorcycle or vehicle",
    "screenshot": "a phone screenshot of TikTok Instagram Facebook or a social media app UI",
    "other": "a photo of unrelated products merchandise or something that is not a restaurant",
}

# Reject when top reject class is this confident AND beats best allowed
REJECT_MIN = float(os.environ.get("AQD_CLIP_REJECT_MIN", "0.28"))
# Keep if best allowed is at least this (even if reject is close)
ALLOW_MIN = float(os.environ.get("AQD_CLIP_ALLOW_MIN", "0.18"))
# Margin: reject must beat allow by this much to drop
REJECT_MARGIN = float(os.environ.get("AQD_CLIP_REJECT_MARGIN", "0.06"))

_lock = threading.Lock()
_model = None
_preprocess = None
_tokenizer = None
_text_features = None
_label_order = None
_load_err = None
_load_s = None


def available() -> bool:
    if os.environ.get("AQD_CLIP", "1") in ("0", "false", "no"):
        return False
    _ensure()
    return _model is not None


def _ensure():
    global _model, _preprocess, _tokenizer, _text_features, _label_order, _load_err, _load_s
    if _model is not None or _load_err is not None:
        return
    with _lock:
        if _model is not None or _load_err is not None:
            return
        t0 = time.time()
        try:
            import torch
            import open_clip
            device = os.environ.get("AQD_CLIP_DEVICE") or "cpu"
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="openai"
            )
            model = model.to(device).eval()
            tokenizer = open_clip.get_tokenizer("ViT-B-32")
            labels = list(PROMPTS.keys())
            texts = tokenizer([PROMPTS[k] for k in labels])
            with torch.no_grad():
                tf = model.encode_text(texts.to(device))
                tf = tf / tf.norm(dim=-1, keepdim=True)
            _model, _preprocess, _tokenizer = model, preprocess, tokenizer
            _text_features, _label_order = tf, labels
            _load_s = time.time() - t0
            _device = device
            globals()["_device"] = device
        except Exception as e:
            _load_err = f"{type(e).__name__}: {e}"


def load_info() -> str:
    _ensure()
    if _load_err:
        return f"CLIP unavailable: {_load_err}"
    if _model is None:
        return "CLIP disabled"
    return f"CLIP ViT-B-32 ready device={globals().get('_device','?')} load={_load_s:.1f}s"


def _fetch_image(url: str, timeout: float = 25):
    from PIL import Image
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; aqd-photo-filter/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return Image.open(io.BytesIO(data)).convert("RGB")


def classify_image(img) -> dict:
    """Return {label, scores, keep, reason}."""
    import torch
    _ensure()
    if _model is None:
        return {"label": "unknown", "scores": {}, "keep": True, "reason": "clip_off"}
    device = globals().get("_device", "cpu")
    im = _preprocess(img).unsqueeze(0).to(device)
    with torch.no_grad():
        feat = _model.encode_image(im)
        feat = feat / feat.norm(dim=-1, keepdim=True)
        logits = (100.0 * feat @ _text_features.T).softmax(dim=-1)[0]
    scores = {lab: float(logits[i]) for i, lab in enumerate(_label_order)}
    best = max(scores, key=scores.get)
    best_allow = max((scores[k] for k in ALLOWED), default=0.0)
    best_reject = max((scores[k] for k in REJECT), default=0.0)
    # Conservative keep: keep unless a reject class clearly dominates
    keep = True
    reason = f"keep:{best}={scores[best]:.2f}"
    if best in REJECT and scores[best] >= REJECT_MIN and scores[best] >= best_allow + REJECT_MARGIN:
        keep = False
        reason = f"drop:{best}={scores[best]:.2f}>allow={best_allow:.2f}"
    elif best_reject >= REJECT_MIN and best_reject >= best_allow + REJECT_MARGIN and best_allow < ALLOW_MIN:
        keep = False
        top_r = max(REJECT, key=lambda k: scores[k])
        reason = f"drop:{top_r}={best_reject:.2f}>allow={best_allow:.2f}"
    elif best in ALLOWED or best_allow >= ALLOW_MIN:
        keep = True
        reason = f"keep:{best}={scores[best]:.2f}/allow={best_allow:.2f}"
    return {"label": best, "scores": scores, "keep": keep, "reason": reason}


def classify_url(url: str) -> dict:
    try:
        img = _fetch_image(url)
    except Exception as e:
        # Network failure: keep (conservative — don't drop on fetch error)
        return {"label": "fetch_error", "scores": {}, "keep": True, "reason": f"fetch_error:{type(e).__name__}"}
    return classify_image(img)


def filter_urls(urls, return_details: bool = False):
    """urls: list[str] or list[dict with 'url']. Returns (kept, skipped_count[, details])."""
    if not urls:
        return ([], 0, []) if return_details else ([], 0)
    items = []
    for u in urls:
        if isinstance(u, dict):
            items.append(u)
        else:
            items.append({"url": u})
    if not available():
        return (items, 0, []) if return_details else (items, 0)
    kept, details = [], []
    skipped = 0
    for it in items:
        url = it.get("url") or ""
        # Cheap URL heuristics before CLIP
        low = url.lower()
        if any(x in low for x in ("tiktok.com", "ttlivecdn", "musical.ly", "screenshot")):
            skipped += 1
            details.append({"url": url, "keep": False, "reason": "url_heuristic:tiktok/screenshot", "label": "screenshot"})
            continue
        t0 = time.time()
        # Prefer smaller download for classify
        cu = url
        if "=s" not in cu and "googleusercontent.com" in cu:
            cu = cu.rstrip("=") + "=s512"
        elif "=s1600" in cu:
            cu = cu.replace("=s1600", "=s512")
        res = classify_url(cu)
        res["ms"] = int((time.time() - t0) * 1000)
        res["url"] = url
        details.append(res)
        if res["keep"]:
            kept.append(it)
        else:
            skipped += 1
    if return_details:
        return kept, skipped, details
    return kept, skipped
