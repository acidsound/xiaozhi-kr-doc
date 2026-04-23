from __future__ import annotations

import argparse
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .feishu import FeishuError, extract_wiki_token


TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SOURCE_RE = re.compile(r"^(?:Source|원문):\s*(\S+)\s*$", re.MULTILINE)
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((<[^>]+>|[^)]+)\)")
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((<[^>]+>|[^)]+)\)")


@dataclass(frozen=True)
class WikiPage:
    language: str
    source_path: Path
    output_name: str
    page_title: str
    source_url: str | None


def build_github_wiki(
    markdown_dir: Path,
    markdown_kr_dir: Path,
    assets_dir: Path,
    out_dir: Path,
) -> list[Path]:
    pages = _collect_pages(markdown_dir, "ZH") + _collect_pages(markdown_kr_dir, "KO")
    page_map = {page.source_path.name: page for page in pages}
    asset_map = _copy_assets(assets_dir, out_dir / "images")

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for page in pages:
        text = page.source_path.read_text(encoding="utf-8")
        rewritten = _rewrite_markdown(text, page_map, asset_map)
        path = out_dir / page.output_name
        path.write_text(rewritten, encoding="utf-8")
        written.append(path)

    written.extend(_write_home_pages(out_dir, pages))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a GitHub Wiki export from local archive markdown files.")
    parser.add_argument("--markdown", default="archive/markdown", help="Chinese markdown directory")
    parser.add_argument("--markdown-kr", default="archive/markdown_kr", help="Korean markdown directory")
    parser.add_argument("--assets", default="archive/assets", help="Archive assets directory")
    parser.add_argument("--out", default=".wiki-build", help="Wiki output directory")
    args = parser.parse_args(argv)

    written = build_github_wiki(
        markdown_dir=Path(args.markdown),
        markdown_kr_dir=Path(args.markdown_kr),
        assets_dir=Path(args.assets),
        out_dir=Path(args.out),
    )
    for path in written:
        print(path)
    return 0


def _collect_pages(markdown_dir: Path, language: str) -> list[WikiPage]:
    used: set[str] = set()
    pages: list[WikiPage] = []

    for path in sorted(markdown_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = _extract_title(text, path.stem)
        source_url = _extract_source_url(text)
        base = f"{language}-{_safe_name(title)}"
        if source_url:
            try:
                token = extract_wiki_token(source_url)
            except FeishuError:
                token = ""
            if token:
                base = f"{base}-{token[:8]}"
        name = _dedupe_name(base, used)
        pages.append(
            WikiPage(
                language=language,
                source_path=path,
                output_name=f"{name}.md",
                page_title=title,
                source_url=source_url,
            )
        )

    return pages


def _copy_assets(assets_dir: Path, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    mapping: dict[str, str] = {}

    for path in sorted(assets_dir.glob("*")):
        if not path.is_file():
            continue
        name = _dedupe_name(_safe_name(path.stem), used) + path.suffix.lower()
        shutil.copyfile(path, out_dir / name)
        mapping[path.name] = name

    return mapping


def _rewrite_markdown(text: str, page_map: dict[str, WikiPage], asset_map: dict[str, str]) -> str:
    def replace_image(match: re.Match[str]) -> str:
        alt, raw_target = match.groups()
        target = raw_target.strip("<>")
        if target.startswith("../assets/"):
            asset_name = Path(target).name
            mapped = asset_map.get(asset_name)
            if mapped:
                return f"![{alt}](images/{mapped})"
        return match.group(0)

    def replace_link(match: re.Match[str]) -> str:
        label, raw_target = match.groups()
        target = raw_target.strip("<>")
        if target.startswith("http://") or target.startswith("https://") or target.startswith("mailto:"):
            return match.group(0)
        if target.endswith(".md"):
            mapped = page_map.get(Path(target).name)
            if mapped:
                return f"[{label}]({mapped.output_name})"
        return match.group(0)

    text = IMAGE_RE.sub(replace_image, text)
    text = LINK_RE.sub(replace_link, text)
    return text


def _write_home_pages(out_dir: Path, pages: list[WikiPage]) -> list[Path]:
    zh_pages = [page for page in pages if page.language == "ZH"]
    ko_pages = [page for page in pages if page.language == "KO"]

    zh_root = _first_page(zh_pages, "小智 AI 聊天机器人百科全书")
    ko_root = _first_page(ko_pages, "Xiaozhi AI 챗봇 백과사전")

    written = [
        _write(
            out_dir / "Home.md",
            "\n".join(
                [
                    "# Xiaozhi AI Wiki",
                    "",
                    "이 위키는 Xiaozhi Feishu 문서를 GitHub Wiki에 맞게 정리한 아카이브입니다.",
                    "This wiki is a GitHub Wiki export of the Xiaozhi Feishu document archive.",
                    "",
                    "## Language",
                    "",
                    f"- [한국어 시작 페이지]({ko_root.output_name if ko_root else 'Home-ko.md'})",
                    f"- [中文开始页]({zh_root.output_name if zh_root else 'Home-zh.md'})",
                    "",
                    "## Notes",
                    "",
                    "- 소스 저장소에는 문서 아카이브를 포함하지 않고, 위키 저장소에만 게시합니다.",
                    "- 각 문서 상단의 `Source:` / `원문:` 링크는 원래 Feishu 문서 URL입니다.",
                    "",
                ]
            )
            + "\n",
        ),
        _write(
            out_dir / "Home-ko.md",
            _home_language_page(
                "# 한국어 시작 페이지",
                "한국어로 번역된 Xiaozhi 문서를 모아둔 시작 페이지입니다.",
                ko_root,
                ko_pages,
            ),
        ),
        _write(
            out_dir / "Home-zh.md",
            _home_language_page(
                "# 中文开始页",
                "这里汇总了 Xiaozhi 文档的中文归档版本。",
                zh_root,
                zh_pages,
            ),
        ),
        _write(
            out_dir / "_Sidebar.md",
            _sidebar(ko_root, zh_root, ko_pages, zh_pages),
        ),
    ]
    return written


def _home_language_page(header: str, intro: str, root: WikiPage | None, pages: list[WikiPage]) -> str:
    lines = [header, "", intro, ""]
    if root is not None:
        lines.extend(["## 메인 문서" if header.startswith("# 한국어") else "## 主文档", "", f"- [{root.page_title}]({root.output_name})", ""])
    lines.extend(["## 전체 문서" if header.startswith("# 한국어") else "## 全部文档", ""])
    for page in pages:
        lines.append(f"- [{page.page_title}]({page.output_name})")
    lines.append("")
    return "\n".join(lines)


def _sidebar(
    ko_root: WikiPage | None,
    zh_root: WikiPage | None,
    ko_pages: list[WikiPage],
    zh_pages: list[WikiPage],
) -> str:
    lines = [
        "[Home](Home.md)",
        "",
        "## Korean",
    ]
    if ko_root is not None:
        lines.append(f"- [{ko_root.page_title}]({ko_root.output_name})")
    lines.append("- [한국어 문서 목록](Home-ko.md)")
    lines.extend(["", "## Chinese"])
    if zh_root is not None:
        lines.append(f"- [{zh_root.page_title}]({zh_root.output_name})")
    lines.append("- [中文文档列表](Home-zh.md)")
    lines.append("")
    return "\n".join(lines)


def _first_page(pages: list[WikiPage], needle: str) -> WikiPage | None:
    needle = needle.lower()
    for page in pages:
        if needle in page.page_title.lower():
            return page
    return pages[0] if pages else None


def _extract_title(text: str, fallback: str) -> str:
    match = TITLE_RE.search(text)
    return match.group(1).strip() if match else fallback


def _extract_source_url(text: str) -> str | None:
    match = SOURCE_RE.search(text)
    return match.group(1) if match else None


def _safe_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.strip()
    value = re.sub(r"[\\/:*?\"<>|]", "-", value)
    value = re.sub(r"[()\[\]{}]+", "-", value)
    value = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af.-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-{2,}", "-", value)
    value = value.strip("-.")
    return value[:120] or "page"


def _dedupe_name(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
