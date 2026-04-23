from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


OPENAPI_BASE = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


@dataclass(frozen=True)
class WikiNode:
    node_token: str
    obj_token: str
    obj_type: str
    title: str
    space_id: str | None = None


def extract_wiki_token(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"wiki", "docx"}:
            return parts[1]
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", value):
        return value
    raise FeishuError(f"Cannot extract wiki token from: {value}")


def slugify(value: str, fallback: str) -> str:
    value = value.strip() or fallback
    value = re.sub(r"[\\/:*?\"<>|#\x00-\x1f]", "-", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or fallback


class FeishuClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.getenv("FEISHU_ACCESS_TOKEN") or self._tenant_token_from_env()

    def get_node(self, wiki_token: str) -> WikiNode:
        data = self._request_json("GET", "/wiki/v2/spaces/get_node", query={"token": wiki_token})
        payload = _unwrap_data(data)
        node = payload.get("node", payload)
        obj_token = node.get("obj_token") or node.get("objToken")
        obj_type = node.get("obj_type") or node.get("objType")
        if not obj_token or not obj_type:
            raise FeishuError(f"Could not find obj_token/obj_type in get_node response: {json.dumps(data, ensure_ascii=False)[:1000]}")
        return WikiNode(
            node_token=node.get("node_token") or node.get("nodeToken") or wiki_token,
            obj_token=obj_token,
            obj_type=obj_type,
            title=node.get("title") or obj_token,
            space_id=node.get("space_id") or node.get("spaceId"),
        )

    def list_child_nodes(self, space_id: str, parent_node_token: str) -> list[WikiNode]:
        nodes: list[WikiNode] = []
        page_token = ""
        while True:
            query = {"parent_node_token": parent_node_token, "page_size": "50"}
            if page_token:
                query["page_token"] = page_token
            data = self._request_json("GET", f"/wiki/v2/spaces/{quote(space_id)}/nodes", query=query)
            payload = _unwrap_data(data)
            items = payload.get("items") or payload.get("nodes") or []
            for item in items:
                obj_token = item.get("obj_token") or item.get("objToken") or ""
                obj_type = item.get("obj_type") or item.get("objType") or ""
                if obj_token and obj_type:
                    nodes.append(
                        WikiNode(
                            node_token=item.get("node_token") or item.get("nodeToken") or "",
                            obj_token=obj_token,
                            obj_type=obj_type,
                            title=item.get("title") or obj_token,
                            space_id=space_id,
                        )
                    )
            if not payload.get("has_more"):
                break
            page_token = payload.get("page_token") or payload.get("next_page_token") or ""
            if not page_token:
                break
        return nodes

    def get_docx_raw_content(self, document_id: str) -> str:
        data = self._request_json("GET", f"/docx/v1/documents/{quote(document_id)}/raw_content")
        payload = _unwrap_data(data)
        content = payload.get("content") or payload.get("raw_content") or payload.get("text")
        if content is None:
            raise FeishuError(f"Could not find raw content in response: {json.dumps(data, ensure_ascii=False)[:1000]}")
        return str(content)

    def _request_json(self, method: str, path: str, query: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{OPENAPI_BASE}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FeishuError(f"HTTP {exc.code} from {url}: {detail}") from exc
        data = json.loads(raw)
        code = data.get("code", 0)
        if code not in (0, "0"):
            raise FeishuError(f"Feishu API error from {url}: {json.dumps(data, ensure_ascii=False)}")
        return data

    @staticmethod
    def _tenant_token_from_env() -> str:
        app_id = os.getenv("FEISHU_APP_ID")
        app_secret = os.getenv("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            raise FeishuError("Set FEISHU_ACCESS_TOKEN or FEISHU_APP_ID/FEISHU_APP_SECRET.")
        body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
        request = Request(
            f"{OPENAPI_BASE}/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        code = data.get("code", 0)
        if code not in (0, "0"):
            raise FeishuError(f"Cannot get tenant access token: {json.dumps(data, ensure_ascii=False)}")
        token = data.get("tenant_access_token")
        if not token:
            raise FeishuError(f"tenant_access_token missing: {json.dumps(data, ensure_ascii=False)}")
        return token


def archive_node(client: FeishuClient, node: WikiNode, out_dir: Path, raw_dir: Path | None, source_url: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{slugify(node.title, node.obj_token)}.{node.obj_type}.json"
        raw_path.write_text(json.dumps(node.__dict__, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if node.obj_type != "docx":
        md = f"# {node.title}\n\nSource: {source_url}\n\nUnsupported Feishu object type: `{node.obj_type}`.\n"
    else:
        content = client.get_docx_raw_content(node.obj_token)
        md = f"# {node.title}\n\nSource: {source_url}\nArchived: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n\n{content.rstrip()}\n"

    path = out_dir / f"{slugify(node.title, node.obj_token)}.md"
    path.write_text(md, encoding="utf-8")
    return path


def _unwrap_data(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("data")
    if isinstance(payload, dict):
        return payload
    return data
