"""Conservative same-origin URL expansion for rejected directory/service pages."""
from __future__ import annotations
import re
from urllib.parse import urljoin, urlsplit

from industry_packs import industry_anchor_keywords, normalize_intel_text

_MARKDOWN_LINK = re.compile(r"\[([^\]]{0,500})\]\((https?://[^)\s]+)\)")

def expand_links(content: str, parent_url: str, pack: dict):
    parent_host = (urlsplit(parent_url).hostname or "").casefold()
    anchors = [normalize_intel_text(item) for item in industry_anchor_keywords(pack)]
    seen, results = set(), []
    for anchor, url in _MARKDOWN_LINK.findall(content or ""):
        parsed = urlsplit(urljoin(parent_url, url))
        if parsed.scheme not in {"http", "https"} or parsed.hostname.casefold() != parent_host:
            continue
        if any(token in parsed.path.casefold() for token in ("/privacy", "/login", "/search", "/wp-content/")):
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean in seen: continue
        seen.add(clean)
        text = normalize_intel_text(anchor)
        score = 1.0 + sum(3.0 for keyword in anchors if keyword and keyword in text)
        if score >= 4.0:
            results.append({"url": clean, "anchor_text": anchor, "score": score})
    return results
