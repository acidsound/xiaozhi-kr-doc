from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

from .assets import localize_markdown_images
from .feishu import FeishuClient, FeishuError, archive_node, extract_wiki_token
from .links import rewrite_internal_wiki_links
from .ssr import archive_local_html, archive_public_ssr, archive_public_ssr_recursive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive a Feishu wiki page as Markdown without launching a browser.")
    parser.add_argument("url_or_token", help="Feishu wiki URL or wiki token")
    parser.add_argument("--out", default="archive/markdown", help="Markdown output directory")
    parser.add_argument("--raw", default="archive/raw", help="Raw metadata output directory")
    parser.add_argument("--assets", default="archive/assets", help="Image asset output directory")
    parser.add_argument("--html", help="Convert a local Feishu SSR HTML file instead of fetching from the network")
    parser.add_argument("--fetch-url", help="Fetch SSR HTML from this URL while keeping url_or_token as the Markdown Source URL")
    parser.add_argument("--no-raw", action="store_true", help="Do not write raw HTML/API response files")
    parser.add_argument("--no-assets", action="store_true", help="Keep Markdown image URLs unchanged instead of copying/downloading assets")
    parser.add_argument("--source", choices=["ssr", "api"], default="ssr", help="Fetch source. ssr uses public Feishu HTML; api uses Feishu OpenAPI credentials.")
    parser.add_argument("--recursive", action="store_true", help="Also try to archive child wiki nodes")
    parser.add_argument("--depth", type=int, default=1, help="SSR recursive depth: 0 only the start page, 1 direct links, 2 links of links, -1 unlimited")
    parser.add_argument("--max-pages", type=int, default=100, help="Maximum pages to fetch in SSR recursive mode")
    parser.add_argument("--no-local-links", action="store_true", help="Keep Feishu wiki links absolute instead of rewriting archived links to relative Markdown paths")
    parser.add_argument("--space-id", help="Wiki space id, only needed if recursive listing cannot infer it")
    parser.add_argument("--skipped-log", help="Write skipped SSR pages to this file. Defaults to <out>/../skipped.txt in recursive mode.")
    return parser


def _write_skipped_log(path: Path, skipped: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(skipped)
    if body:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out_dir = Path(args.out)
        raw_dir = None if args.no_raw else Path(args.raw)
        if args.html:
            path, _ = archive_local_html(Path(args.html), args.url_or_token, out_dir)
            paths = [path]
            if not args.no_assets:
                localize_markdown_images(out_dir, Path(args.assets), paths)
            if not args.no_local_links:
                changed = rewrite_internal_wiki_links(out_dir, paths)
                if changed:
                    print(f"rewritten local links in {changed} file(s)", file=sys.stderr)
            print(path)
            return 0

        if args.source == "ssr":
            if args.recursive:
                if args.fetch_url:
                    raise FeishuError("--fetch-url is only supported for a single SSR page, not --recursive.")
                paths, skipped = archive_public_ssr_recursive(
                    args.url_or_token,
                    out_dir,
                    raw_dir,
                    max_pages=args.max_pages,
                    max_depth=args.depth,
                )
            else:
                path, _ = archive_public_ssr(args.url_or_token, out_dir, raw_dir, fetch_url=args.fetch_url)
                paths = [path]
                skipped = []
            for path in paths:
                print(path)
            if not args.no_assets:
                localize_markdown_images(out_dir, Path(args.assets), paths)
            if not args.no_local_links:
                changed = rewrite_internal_wiki_links(out_dir, paths)
                if changed:
                    print(f"rewritten local links in {changed} file(s)", file=sys.stderr)
            for item in skipped:
                print(f"skipped: {item}", file=sys.stderr)
            skipped_log = Path(args.skipped_log) if args.skipped_log else out_dir.parent / "skipped.txt"
            _write_skipped_log(skipped_log, skipped)
            if skipped:
                print(f"wrote skipped log: {skipped_log}", file=sys.stderr)
            return 0

        wiki_token = extract_wiki_token(args.url_or_token)
        client = FeishuClient()
        root = client.get_node(wiki_token)
        written = [archive_node(client, root, out_dir, raw_dir, args.url_or_token)]

        if args.recursive:
            space_id = root.space_id or args.space_id
            if not space_id:
                raise FeishuError("--recursive needs a space id, but get_node did not return one. Pass --space-id.")
            seen = {root.node_token}
            queue = deque([root])
            while queue:
                parent = queue.popleft()
                for child in client.list_child_nodes(space_id, parent.node_token):
                    if not child.node_token or child.node_token in seen:
                        continue
                    seen.add(child.node_token)
                    written.append(archive_node(client, child, out_dir, raw_dir, args.url_or_token))
                    queue.append(child)

        for path in written:
            print(path)
        if not args.no_assets:
            localize_markdown_images(out_dir, Path(args.assets), written)
        if not args.no_local_links:
            changed = rewrite_internal_wiki_links(out_dir, written)
            if changed:
                print(f"rewritten local links in {changed} file(s)", file=sys.stderr)
        return 0
    except FeishuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
