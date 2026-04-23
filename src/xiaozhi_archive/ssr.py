from __future__ import annotations

import base64
import gzip
import http.cookiejar
import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

from .feishu import FeishuError, extract_wiki_token, slugify

try:
    import blackboxprotobuf  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - dependency is optional at import time
    blackboxprotobuf = None


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
CLIENT_VARS_MARKER = "window.DATA = Object.assign({}, window.DATA, { clientVars: Object("
META_MARKER = "window.DATA = { clientVars: undefined, meta: Object("
DOCX_FALLBACK_DOMAINS = ("smvsudqc87.feishu.cn",)
SHEET_MEMBER_ID = 1
SHEET_SCHEMA_VERSION = 9
SHEET_CLIENT_VERSION = "v0.0.1"


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
    last_login_error = ""
    for candidate in _fetch_candidates(url):
        request = Request(candidate, headers=headers)
        try:
            with opener.open(request, timeout=45) as response:
                html = response.read().decode("utf-8", errors="replace")
                response_url = response.geturl()
        except Exception:
            continue
        if "accounts/page/login" in response_url or "suite/passport" in html[:10000]:
            last_login_error = candidate
            continue
        score = html.count('data-block-type="') + html.count("docx-image") * 2 + html.count("<table")
        if score > best_score:
            best_html = html
            best_score = score
            best_url = response_url
        if html.count('data-block-type="image"') or html.count("<table"):
            break
    if not best_html:
        if last_login_error:
            raise FeishuError(f"Feishu returned the login page instead of public SSR HTML: {last_login_error}")
        raise FeishuError("Feishu did not return public SSR HTML.")
    return best_html


def _fetch_candidates(url: str) -> list[str]:
    candidates = [
        url,
        _with_query(url, {"open_in_browser": "true"}),
        _with_query(url, {"login_redirect_times": "0"}),
        _with_query(url, {"ccm_open_type": "lark_wiki_spaceLink"}),
    ]
    candidates.extend(_docx_fallback_candidates(url))
    return list(
        dict.fromkeys(
            candidates
        )
    )


def _docx_fallback_candidates(url: str) -> list[str]:
    try:
        token = extract_wiki_token(url)
    except FeishuError:
        return []
    parsed = urlparse(url)
    hosts = [parsed.netloc, *DOCX_FALLBACK_DOMAINS]
    return [f"https://{host}/docx/{quote(token)}" for host in hosts if host]


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


def _extract_json_object_after_marker(text: str, marker: str) -> dict | None:
    try:
        start = text.index(marker) + len(marker)
    except ValueError:
        return None
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    in_string = False
    escaped = False
    for end in range(start, len(text)):
        char = text[end]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_text_data(text_data: dict | None, discovered_urls: set[str]) -> str:
    if not isinstance(text_data, dict):
        return ""
    initial = text_data.get("initialAttributedTexts") or {}
    text_map = initial.get("text") or {}
    attrib_map = initial.get("attribs") or {}
    apool = (text_data.get("apool") or {}).get("numToAttrib") or {}
    rendered: list[str] = []
    for key in sorted(text_map, key=lambda value: int(value) if str(value).isdigit() else str(value)):
        segment = text_map.get(key) or ""
        attrs = attrib_map.get(key) or ""
        rendered.append(_render_attributed_segment(segment, attrs, apool, discovered_urls))
    text = "".join(rendered).replace("\u200b", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


def _render_attributed_segment(raw_text: str, attrs: str, apool: dict, discovered_urls: set[str]) -> str:
    refs = re.findall(r"\*(\d+)", attrs or "")
    inline_bits: list[str] = []
    link_url = ""
    for ref in refs:
        attrib = apool.get(ref)
        if not isinstance(attrib, list) or len(attrib) < 2:
            continue
        kind, value = attrib[0], attrib[1]
        if kind == "inline-component":
            inline_bits.append(_render_inline_component(value, discovered_urls))
        elif kind == "link":
            link_url = unquote(str(value))

    visible = raw_text or ""
    if inline_bits:
        suffix = "".join(bit for bit in inline_bits if bit)
        return f"{visible}{suffix}" if visible.strip() else suffix
    if link_url and visible.strip():
        return f"[{visible.strip()}]({link_url})"
    return visible


def _render_inline_component(value: str, discovered_urls: set[str]) -> str:
    try:
        component = json.loads(value)
    except json.JSONDecodeError:
        return ""
    component_type = component.get("type", "")
    data = component.get("data") or {}
    title = str(data.get("title") or component_type or "").strip()
    if component_type == "mention_doc":
        token = str(data.get("token") or "").strip()
        url = str(data.get("raw_url") or "").strip()
        if not url and token:
            url = f"https://my.feishu.cn/wiki/{quote(token)}"
        if url:
            discovered_urls.add(url)
        return f"[{title}]({url})" if title and url else title
    if component_type == "url":
        url = str(data.get("link") or data.get("url") or "").strip()
        return f"[{title or url}]({url})" if url else title
    return title


def _block_text(block_map: dict[str, dict], block_id: str, discovered_urls: set[str], seen: set[str] | None = None) -> str:
    if seen is None:
        seen = set()
    if block_id in seen:
        return ""
    seen.add(block_id)
    block = block_map.get(block_id) or {}
    data = block.get("data") or {}
    pieces: list[str] = []
    text = _extract_text_data(data.get("text"), discovered_urls)
    if text:
        pieces.append(text)
    if data.get("type") == "table_cell" or (not text and data.get("children")):
        for child_id in data.get("children") or []:
            child_text = _block_text(block_map, child_id, discovered_urls, seen)
            if child_text:
                pieces.append(child_text)
    return "\n".join(piece for piece in pieces if piece).strip()


def _collect_descendants(block_map: dict[str, dict], block_id: str, extra_ids: list[str] | None = None) -> set[str]:
    stack = [block_id]
    seen: set[str] = set(extra_ids or [])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        block = block_map.get(current) or {}
        data = block.get("data") or {}
        for child in data.get("children") or []:
            stack.append(child)
    return seen


def _render_table_block(block_map: dict[str, dict], block_id: str, discovered_urls: set[str]) -> str:
    data = (block_map.get(block_id) or {}).get("data") or {}
    rows_id = data.get("rows_id") or []
    columns_id = data.get("columns_id") or []
    cell_set = data.get("cell_set") or {}
    rows: list[list[str]] = []
    for row_id in rows_id:
        row: list[str] = []
        for column_id in columns_id:
            cell = cell_set.get(f"{row_id}{column_id}") or {}
            cell_block_id = cell.get("block_id")
            row.append(_block_text(block_map, cell_block_id, discovered_urls) if cell_block_id else "")
        rows.append(row)
    return _markdown_table(rows)


def _render_image_block(block_id: str, block_data: dict) -> str:
    image = block_data.get("image") or {}
    token = image.get("token")
    if not token:
        return ""
    return (
        "https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/v2/cover/"
        f"{quote(str(token))}/?fallback_source=1&height=1280&mount_node_token={quote(block_id)}"
        "&mount_point=docx_image&policy=equal&width=1280"
    )


def _public_cookie_opener(url: str):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    with opener.open(request, timeout=30) as response:
        response.read(1024)
    return opener


def _sheet_table_markdown(
    source_url: str,
    sheet_token: str,
    cache: dict[tuple[str, str], str | None],
    heading_text: str | None = None,
) -> str:
    cache_key = (source_url, sheet_token)
    if cache_key in cache:
        rendered = cache[cache_key] or ""
    else:
        rendered = _fetch_sheet_table_markdown(source_url, sheet_token)
        cache[cache_key] = rendered or None
    return _normalize_sheet_table(rendered, heading_text)


def _normalize_sheet_table(table: str, heading_text: str | None) -> str:
    if not table or not heading_text:
        return table
    lines = table.splitlines()
    if len(lines) < 2 or not lines[0].startswith("| "):
        return table
    header = [cell.strip() for cell in lines[0].strip().strip("|").split("|")]
    if len(header) < 2 or "开发板" not in header[1]:
        return table
    heading = _normalize_sheet_heading(heading_text)
    if not heading or header[0] == heading:
        return table
    header[0] = heading
    lines[0] = "| " + " | ".join(header) + " |"
    return "\n".join(lines)


def _normalize_sheet_heading(heading_text: str) -> str:
    heading = re.sub(r"^\d+(?:\.\d+)*\s*", "", heading_text).strip()
    heading = re.sub(r"\s*接线(?:表)?(?:（重要）)?\s*$", "", heading).strip()
    return heading


def _fetch_sheet_table_markdown(source_url: str, sheet_token: str) -> str:
    if blackboxprotobuf is None:
        return ""
    doc_token, _, sheet_id = sheet_token.partition("_")
    if not doc_token or not sheet_id:
        return ""

    opener = _public_cookie_opener(source_url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/json",
        "Referer": source_url,
    }
    body = json.dumps(
        {
            "memberId": SHEET_MEMBER_ID,
            "schemaVersion": SHEET_SCHEMA_VERSION,
            "openType": 1,
            "token": doc_token,
            "sheetRange": {"sheetId": sheet_id},
            "clientVersion": SHEET_CLIENT_VERSION,
        }
    ).encode("utf-8")
    request = Request(
        "https://my.feishu.cn/space/api/v3/sheet/client_vars",
        data=body,
        headers=headers,
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") not in (0, "0"):
        raise FeishuError(f"Feishu sheet client_vars failed: {json.dumps(payload, ensure_ascii=False)[:500]}")

    snapshot = ((payload.get("data") or {}).get("snapshot") or {})
    block_meta_raw = snapshot.get("gzipBlockMeta")
    if not block_meta_raw:
        return ""
    block_meta = json.loads(gzip.decompress(base64.b64decode(block_meta_raw)).decode("utf-8"))
    metas = (((block_meta.get(sheet_id) or {}).get("cellBlockMetas")) or [])
    tables: list[str] = []
    for meta in metas:
        block_id = str(meta.get("blockId") or "").strip()
        if not block_id:
            continue
        rows = int(((meta.get("range") or {}).get("rowEnd")) or 0)
        cols = int(((meta.get("range") or {}).get("colEnd")) or 0)
        if rows <= 0 or cols <= 0:
            continue
        block_url = (
            "https://my.feishu.cn/space/api/v3/sheet/block?"
            f"token={quote(doc_token)}&block_token={quote(block_id)}"
            f"&schema_version={SHEET_SCHEMA_VERSION}&blockIds={quote(block_id)}"
        )
        block_request = Request(block_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*", "Referer": source_url})
        with opener.open(block_request, timeout=30) as response:
            block_payload = json.loads(response.read().decode("utf-8"))
        encoded = ((block_payload.get("data") or {}).get("blocks") or {}).get(block_id)
        if not encoded:
            continue
        decoded = gzip.decompress(base64.b64decode(encoded))
        rows_data = _decode_sheet_block_rows(decoded, rows, cols)
        if rows_data:
            tables.append(_markdown_table(rows_data))
    return "\n\n".join(table for table in tables if table)


def _decode_sheet_block_rows(block_bytes: bytes, rows: int, cols: int) -> list[list[str]]:
    if blackboxprotobuf is None:
        return []
    message, _ = blackboxprotobuf.decode_message(block_bytes)
    strings = _sheet_text_cells(message)
    if not strings:
        return []
    columns = _partition_sheet_columns(strings, rows, cols)
    return [[columns[col][row] if row < len(columns[col]) else "" for col in range(cols)] for row in range(rows)]


def _sheet_text_cells(message: dict) -> list[str]:
    try:
        cells = message["1"]["2"]["12"]["2"]["2"]
    except Exception:
        return []
    result: list[str] = []
    for item in cells:
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="ignore")
        elif isinstance(item, dict):
            raw = item.get("__bytes__") or item.get("1") or item.get("2")
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="ignore")
            else:
                text = str(raw or "")
        else:
            text = str(item)
        text = text.strip().strip('"').strip("$&T").strip()
        if not text or text.startswith("rgb("):
            continue
        result.append(text)
    return result


def _partition_sheet_columns(strings: list[str], rows: int, cols: int) -> list[list[str]]:
    columns: list[list[str]] = []
    index = 0
    for col in range(cols):
        remaining_cols = cols - col
        remaining_strings = len(strings) - index
        if remaining_strings <= 0:
            columns.append([""] * rows)
            continue
        if remaining_cols == 1:
            take = remaining_strings
        else:
            take = min(rows, max(1, remaining_strings - (remaining_cols - 1)))
        column = strings[index : index + take]
        index += take
        if len(column) < rows:
            column = column + [""] * (rows - len(column))
        columns.append(column[:rows])
    return columns


def _client_vars_to_markdown(html: str, source_url: str) -> tuple[str, str, set[str]] | None:
    payload = _extract_json_object_after_marker(html, CLIENT_VARS_MARKER)
    meta = _extract_json_object_after_marker(html, META_MARKER) or {}
    if not payload or not isinstance(payload.get("data"), dict):
        return None

    data = payload["data"]
    block_map = data.get("block_map")
    block_sequence = data.get("block_sequence")
    root_id = data.get("id")
    if not isinstance(block_map, dict) or not isinstance(block_sequence, list) or not root_id:
        return None

    title = str(meta.get("title") or "Feishu Wiki Archive").strip() or "Feishu Wiki Archive"
    discovered_urls: set[str] = set()
    lines = [
        f"# {title}",
        "",
        f"Source: {source_url}",
        f"Archived: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
    ]
    skip_ids: set[str] = {str(root_id)}
    emitted = 0
    sheet_cache: dict[tuple[str, str], str | None] = {}
    last_heading_text: str | None = None

    for block_id in block_sequence:
        if block_id in skip_ids or block_id not in block_map:
            continue
        block = block_map[block_id]
        data = block.get("data") or {}
        if data.get("hidden"):
            continue
        kind = data.get("type") or ""
        if kind in {"page", "synced_source", "synced_reference", "grid", "grid_column", "table_cell"}:
            continue

        if kind == "table":
            extra_ids: list[str] = []
            cell_set = data.get("cell_set") or {}
            for cell in cell_set.values():
                cell_block_id = cell.get("block_id")
                if cell_block_id:
                    extra_ids.extend(_collect_descendants(block_map, cell_block_id))
            skip_ids.update(extra_ids)
            text = _render_table_block(block_map, block_id, discovered_urls)
        elif kind == "image":
            text = _render_image_block(block_id, data)
        elif kind == "sheet":
            text = _sheet_table_markdown(
                source_url,
                str(data.get("token") or ""),
                sheet_cache,
                last_heading_text,
            )
        else:
            text = _extract_text_data(data.get("text"), discovered_urls)

        if not text:
            continue
        if re.fullmatch(r"\d+%?", text):
            continue

        emitted += 1
        if kind.startswith("heading"):
            suffix = kind.removeprefix("heading")
            level = int(suffix) + 1 if suffix.isdigit() else 2
            last_heading_text = text
            lines.extend([f"{'#' * min(level, 6)} {text}", ""])
        elif kind == "ordered":
            seq = str(data.get("seq") or "1").strip()
            seq = seq if re.fullmatch(r"\d+(\.\d+)*", seq) else "1"
            lines.append(text if re.match(r"^\d+(\.\d+)*\.?\s+", text) else f"{seq}. {text}")
        elif kind == "bullet":
            lines.append(f"- {text}")
        elif kind == "todo":
            mark = "x" if data.get("done") else " "
            lines.append(f"- [{mark}] {text}")
        elif kind == "code":
            lines.extend(["```", text, "```", ""])
        elif kind == "image":
            lines.extend([f"![image]({text})", ""])
        elif kind == "file":
            lines.extend([f"[file] {text}", ""])
        else:
            lines.extend([text, ""])

    if emitted == 0:
        return None
    markdown = "\n".join(lines)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).rstrip() + "\n"
    return title, markdown, discovered_urls


def ssr_html_to_markdown(html: str, source_url: str, base_url: str | None = None) -> tuple[str, str, set[str]]:
    structured = _client_vars_to_markdown(html, source_url)
    if structured is not None:
        return structured

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
