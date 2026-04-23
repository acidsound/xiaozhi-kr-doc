from __future__ import annotations

import http.cookiejar
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

from .feishu import FeishuError, extract_wiki_token, slugify


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


@dataclass
class Block:
    kind: str
    depth: int = 1
    parts: list[str] = field(default_factory=list)
    links: list[tuple[int, str]] = field(default_factory=list)

    def text(self) -> str:
        text = "".join(self.parts)
        text = text.replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


class FeishuSSRParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.blocks: list[Block] = []
        self.stack: list[Block] = []
        self.title_parts: list[str] = []
        self.title_depth = 0
        self.skip_depth = 0
        self.discovered_urls: set[str] = set()
        self.table_depth = 0
        self.table_rows: list[list[str]] = []
        self.table_row: list[str] | None = None
        self.table_cell: list[str] | None = None

    @property
    def title(self) -> str:
        title = "".join(self.title_parts).replace("\u200b", "")
        title = re.sub(r"\s+", " ", title).strip()
        return title or "Feishu Wiki Archive"

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        class_name = attrs.get("class", "")
        if tag in {"script", "style", "svg"}:
            self.skip_depth += 1
            return

        if self.table_depth:
            if self.stack:
                self.stack[-1].depth += 1
            self.table_depth += 1
            if tag == "tr":
                self.table_row = []
            elif tag in {"td", "th"}:
                self.table_cell = []
            elif tag == "br" and self.table_cell is not None:
                self.table_cell.append("\n")
            return

        if tag == "h1" and "page-block-content" in class_name:
            self.title_depth = 1
            return
        if self.title_depth:
            self.title_depth += 1

        block_type = attrs.get("data-block-type")
        if block_type:
            self.stack.append(Block(kind=block_type))
            return

        if not self.stack:
            return

        active = self.stack[-1]
        if active.kind == "table" and tag == "table":
            active.depth += 1
            self.table_depth = 1
            self.table_rows = []
            self.table_row = None
            self.table_cell = None
            return

        active.depth += 1
        if active.kind == "image" and tag == "img":
            src = attrs.get("src") or attrs.get("data-src") or ""
            if src:
                active.parts.append(urljoin(self.base_url, src))
        token = attrs.get("data-token") if "mention-doc" in class_name else ""
        href = attrs.get("href") if tag == "a" else ""
        if token:
            url = f"https://my.feishu.cn/wiki/{quote(token)}"
            self.discovered_urls.add(url)
            active.parts.append("[")
            active.links.append((active.depth, url))
        elif href:
            url = urljoin(self.base_url, href)
            if "/wiki/" in url:
                self.discovered_urls.add(url)
            active.parts.append("[")
            active.links.append((active.depth, url))

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            if tag in {"script", "style", "svg"}:
                self.skip_depth -= 1
            return

        if self.table_depth:
            if tag in {"td", "th"} and self.table_cell is not None and self.table_row is not None:
                self.table_row.append(_clean_table_cell("".join(self.table_cell)))
                self.table_cell = None
            elif tag == "tr" and self.table_row is not None:
                self.table_rows.append(self.table_row)
                self.table_row = None

            if self.stack:
                self.stack[-1].depth -= 1
            self.table_depth -= 1
            if tag == "table":
                if self.stack:
                    self.stack[-1].parts.append(_markdown_table(self.table_rows))
                self.table_rows = []
                self.table_row = None
                self.table_cell = None
            return

        if self.title_depth:
            self.title_depth -= 1
            return

        if not self.stack:
            return

        active = self.stack[-1]
        if active.links and active.links[-1][0] == active.depth:
            _, url = active.links.pop()
            active.parts.append(f"]({url})")
        active.depth -= 1
        if active.depth == 0:
            self.blocks.append(self.stack.pop())

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.table_depth:
            if self.table_cell is not None:
                self.table_cell.append(data.replace("\u200b", ""))
            return
        if self.title_depth:
            self.title_parts.append(data)
            return
        if self.stack:
            self.stack[-1].parts.append(data)


def resolve_public_feishu_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=30) as response:
            return response.geturl()
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            location = exc.headers.get("Location")
            if location and "feishu.cn/" in location:
                redirected = urljoin(url, location)
                query = parse_qs(urlparse(redirected).query)
                redirect_uri = query.get("redirect_uri", [""])[0]
                if redirect_uri and "feishu.cn/" in redirect_uri:
                    return unquote(redirect_uri)
                return redirected
        return url


def fetch_public_feishu_html(url: str) -> str:
    url = resolve_public_feishu_url(url)
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7,zh-CN;q=0.6",
    }
    best_html = ""
    best_score = -1
    best_url = url
    for candidate in _fetch_candidates(url):
        request = Request(candidate, headers=headers)
        with opener.open(request, timeout=45) as response:
            html = response.read().decode("utf-8", errors="replace")
            response_url = response.geturl()
        score = html.count('data-block-type="') + html.count("docx-image") * 2 + html.count("<table")
        if score > best_score:
            best_html = html
            best_score = score
            best_url = response_url
        if html.count('data-block-type="image"') or html.count("<table"):
            break
    if "accounts/page/login" in best_url or "suite/passport" in best_html[:10000]:
        raise FeishuError("Feishu returned the login page instead of public SSR HTML.")
    return best_html


def _fetch_candidates(url: str) -> list[str]:
    return list(
        dict.fromkeys(
            [
                url,
                _with_query(url, {"open_in_browser": "true"}),
                _with_query(url, {"login_redirect_times": "0"}),
                _with_query(url, {"ccm_open_type": "lark_wiki_spaceLink"}),
            ]
        )
    )


def _with_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in extra.items():
        query[key] = [value]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _clean_table_cell(value: str) -> str:
    value = value.replace("\u200b", "")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value.strip()


def _markdown_table(rows: list[list[str]]) -> str:
    rows = [[cell.replace("|", r"\|") for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    body = rows[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def ssr_html_to_markdown(html: str, source_url: str, base_url: str | None = None) -> tuple[str, str, set[str]]:
    parser = FeishuSSRParser(source_url if base_url is None else base_url)
    parser.feed(html)

    lines = [
        f"# {parser.title}",
        "",
        f"Source: {source_url}",
        f"Archived: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
    ]
    emitted = 0
    for block in parser.blocks:
        text = block.text()
        if not text:
            continue
        if re.fullmatch(r"\d+%?", text):
            continue
        kind = block.kind
        if kind == "page":
            continue
        emitted += 1
        if kind.startswith("heading"):
            suffix = kind.removeprefix("heading")
            level = int(suffix) + 1 if suffix.isdigit() else 2
            lines.extend([f"{'#' * min(level, 6)} {text}", ""])
        elif kind == "ordered":
            lines.append(text if re.match(r"^\d+(\.\d+)*\.?\s+", text) else f"1. {text}")
        elif kind == "bullet":
            lines.append(f"- {text}")
        elif kind == "todo":
            lines.append(f"- [ ] {text}")
        elif kind == "code":
            lines.extend(["```", text, "```", ""])
        elif kind == "image":
            lines.extend([f"![image]({text})", ""])
        elif kind == "file":
            lines.extend([f"[file] {text}", ""])
        elif kind in {"text", "callout"}:
            lines.extend([text, ""])
        elif kind in {"synced_source", "synced_reference"}:
            continue
        elif kind.endswith("table") or kind == "table":
            lines.extend([text, ""])
        else:
            lines.extend([text, ""])

    md = "\n".join(lines)
    md = re.sub(r"\n{3,}", "\n\n", md).rstrip() + "\n"
    if emitted == 0 and parser.title == "Feishu Wiki Archive":
        raise FeishuError("Feishu SSR HTML did not contain readable document blocks.")
    return parser.title, md, parser.discovered_urls


def archive_public_ssr(url: str, out_dir: Path, raw_dir: Path | None, fetch_url: str | None = None) -> tuple[Path, set[str]]:
    token = extract_wiki_token(url)
    html = fetch_public_feishu_html(fetch_url or url)
    title, markdown, links = ssr_html_to_markdown(html, url)
    out_dir.mkdir(parents=True, exist_ok=True)
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{slugify(title, token)}.html"
        raw_path.write_text(html, encoding="utf-8")
    md_path = out_dir / f"{slugify(title, token)}.md"
    md_path.write_text(markdown, encoding="utf-8")
    return md_path, links


def archive_local_html(html_path: Path, source_url: str, out_dir: Path) -> tuple[Path, set[str]]:
    token = extract_wiki_token(source_url)
    html = html_path.read_text(encoding="utf-8", errors="replace")
    title, markdown, links = ssr_html_to_markdown(html, source_url, base_url="")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{slugify(title, token)}.md"
    md_path.write_text(markdown, encoding="utf-8")
    return md_path, links


def archive_public_ssr_recursive(url: str, out_dir: Path, raw_dir: Path | None, max_pages: int = 100, max_depth: int = 1) -> tuple[list[Path], list[str]]:
    queue = [(url, 0)]
    seen: set[str] = set()
    written: list[Path] = []
    skipped: list[str] = []
    while queue and len(seen) < max_pages:
        current, depth = queue.pop(0)
        token = extract_wiki_token(current)
        if token in seen:
            continue
        seen.add(token)
        try:
            path, links = archive_public_ssr(current, out_dir, raw_dir)
        except FeishuError as exc:
            skipped.append(f"{current}: {exc}")
            continue
        written.append(path)
        if max_depth >= 0 and depth >= max_depth:
            continue
        for link in sorted(links):
            try:
                link_token = extract_wiki_token(link)
            except FeishuError:
                continue
            queued_tokens = {extract_wiki_token(queued_url) for queued_url, _ in queue}
            if link_token not in seen and link_token not in queued_tokens:
                queue.append((link, depth + 1))
    return written, skipped
