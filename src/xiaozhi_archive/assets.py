from __future__ import annotations

import hashlib
import http.cookiejar
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .feishu import FeishuError, extract_wiki_token, slugify
from .ssr import USER_AGENT, resolve_public_feishu_url


IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
SOURCE_RE = re.compile(r"^(?:Source|원문):\s*(\S+)\s*$", re.MULTILINE)
DOCX_FALLBACK_DOMAIN = "smvsudqc87.feishu.cn"


def localize_markdown_images(markdown_dir: Path, assets_dir: Path, paths: list[Path] | None = None) -> int:
    targets = paths if paths is not None else sorted(markdown_dir.glob("*.md"))
    assets_dir.mkdir(parents=True, exist_ok=True)
    changed = 0

    for path in targets:
        text = path.read_text(encoding="utf-8")
        if not IMAGE_RE.search(text):
            continue
        source_url = _source_url(text)
        opener = None

        def replace(match: re.Match[str]) -> str:
            nonlocal opener
            alt, target = match.groups()
            if target.startswith("../assets/"):
                return match.group(0)
            if urlparse(target).scheme in {"http", "https"} and opener is None:
                opener = _cookie_opener(source_url)
            asset = _materialize_image(target, path, assets_dir, opener)
            if asset is None:
                return match.group(0)
            rel_path = os.path.relpath(asset, path.parent)
            return f"![{alt}]({Path(rel_path).as_posix()})"

        rewritten = IMAGE_RE.sub(replace, text)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            changed += 1

    return changed


def _source_url(text: str) -> str | None:
    match = SOURCE_RE.search(text)
    return match.group(1) if match else None


def _cookie_opener(source_url: str | None):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    if not source_url:
        return opener
    for url in _preload_urls(source_url):
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        try:
            with opener.open(request, timeout=30) as response:
                response.read(1024)
        except Exception:
            continue
    return opener


def _preload_urls(source_url: str) -> list[str]:
    urls = [source_url, resolve_public_feishu_url(source_url)]
    try:
        token = extract_wiki_token(source_url)
    except FeishuError:
        return urls
    urls.append(f"https://{DOCX_FALLBACK_DOMAIN}/docx/{token}")
    return list(dict.fromkeys(urls))


def _materialize_image(target: str, markdown_path: Path, assets_dir: Path, opener) -> Path | None:
    parsed = urlparse(target)
    if parsed.scheme in {"http", "https"}:
        if opener is None:
            return None
        return _download_image(target, markdown_path, assets_dir, opener)
    if parsed.scheme == "blob":
        return None
    return _copy_local_image(target, markdown_path, assets_dir)


def _download_image(url: str, markdown_path: Path, assets_dir: Path, opener) -> Path | None:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://smvsudqc87.feishu.cn/",
        },
    )
    try:
        with opener.open(request, timeout=45) as response:
            data = response.read()
            content_type = response.headers.get("content-type", "")
    except Exception:
        return None
    if not data:
        return None
    ext = _extension_from_content_type(content_type) or _extension_from_url(url) or ".bin"
    asset = assets_dir / _asset_name(markdown_path.stem, url, ext)
    if not asset.exists():
        asset.write_bytes(data)
    return asset


def _copy_local_image(target: str, markdown_path: Path, assets_dir: Path) -> Path | None:
    local_target = unquote(target.strip("<>"))
    source = (markdown_path.parent / local_target).resolve()
    if not source.exists():
        sibling_source = (markdown_path.parent.parent / "markdown" / local_target).resolve()
        if sibling_source.exists():
            source = sibling_source
        else:
            return None
    ext = source.suffix or ".bin"
    asset = assets_dir / _asset_name(markdown_path.stem, str(source), ext)
    if not asset.exists():
        shutil.copyfile(source, asset)
    return asset


def _asset_name(stem: str, identity: str, ext: str) -> str:
    prefix = slugify(stem, "image")[:60]
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}{ext.lower()}"


def _extension_from_content_type(content_type: str) -> str | None:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }.get(content_type)


def _extension_from_url(url: str) -> str | None:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"} else None
