from __future__ import annotations

import os
import re
from pathlib import Path

from .feishu import FeishuError, extract_wiki_token


SOURCE_RE = re.compile(r"^(?:Source|원문):\s*(https://[A-Za-z0-9_-]+\.feishu\.cn/wiki/[A-Za-z0-9_-]+)\s*$", re.MULTILINE)
WIKI_URL_RE = re.compile(r"https://[A-Za-z0-9_-]+\.feishu\.cn/wiki/[A-Za-z0-9_-]+")


def build_source_index(markdown_dir: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in sorted(markdown_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = SOURCE_RE.search(text)
        if not match:
            continue
        try:
            token = extract_wiki_token(match.group(1))
        except FeishuError:
            continue
        index[token] = path
    return index


def rewrite_internal_wiki_links(markdown_dir: Path, paths: list[Path] | None = None) -> int:
    source_index = build_source_index(markdown_dir)
    targets = paths if paths is not None else sorted(markdown_dir.glob("*.md"))
    changed = 0

    for path in targets:
        text = path.read_text(encoding="utf-8")
        rewritten_lines = [_rewrite_line(line, path, source_index) for line in text.splitlines()]
        rewritten = "\n".join(rewritten_lines)
        if text.endswith("\n"):
            rewritten += "\n"
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            changed += 1

    return changed


def _rewrite_line(line: str, current_path: Path, source_index: dict[str, Path]) -> str:
    if line.startswith("Source: ") or line.startswith("원문: "):
        return line

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        try:
            token = extract_wiki_token(url)
        except FeishuError:
            return url
        target_path = source_index.get(token)
        if target_path is None:
            return url
        rel_path = os.path.relpath(target_path, current_path.parent)
        return f"<{Path(rel_path).as_posix()}>"

    return WIKI_URL_RE.sub(replace, line)
