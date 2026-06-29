import os
import re
import json
import time
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests
from markdownify import markdownify as md

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
SPACE_ID = os.environ["FEISHU_WIKI_SPACE_ID"]

OUT_DIR = Path(os.environ.get("FEISHU_OUT_DIR", "data/wiki"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = Path("index.json")

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
WIKI_NODE_URL = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
WIKI_CHILDREN_URL = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node_list"
DOCX_GET_URL = "https://open.feishu.cn/open-apis/docx/v1/documents/{}"
DOC_EXPORT_URL = "https://open.feishu.cn/open-apis/docx/v1/documents/{}/export"

session = requests.Session()


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name[:180] or "untitled"


def digest(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_json(method: str, url: str, token: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    headers = kwargs.pop("headers", {})
    headers.setdefault("Content-Type", "application/json; charset=utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(6):
        r = session.request(method, url, headers=headers, timeout=40, **kwargs)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(2**attempt, 30))
            continue
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise RuntimeError(f"API error {data.get('code')}: {data.get('msg') or data}")
        return data
    r.raise_for_status()
    return {}


def get_token() -> str:
    data = request_json(
        "POST",
        TOKEN_URL,
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    return data["tenant_access_token"]


def get_root_node(token: str) -> Dict[str, Any]:
    data = request_json("GET", WIKI_NODE_URL, token=token, params={"space_id": SPACE_ID})
    return data["data"]["node"]


def get_children(token: str, node_token: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token = None
    while True:
        params = {"parent_node_token": node_token, "page_size": 50}
        if page_token:
            params["page_token"] = page_token
        data = request_json("GET", WIKI_CHILDREN_URL, token=token, params=params)
        payload = data.get("data", {})
        items.extend(payload.get("items", []))
        if not payload.get("has_more"):
            break
        page_token = payload.get("page_token")
        if not page_token:
            break
    return items


def get_doc_basic(token: str, doc_token: str) -> Dict[str, Any]:
    return request_json("GET", DOCX_GET_URL.format(doc_token), token=token)


def try_export_doc(token: str, doc_token: str) -> Optional[str]:
    try:
        data = request_json("POST", DOC_EXPORT_URL.format(doc_token), token=token, json={})
        if "data" in data:
            return json.dumps(data["data"], ensure_ascii=False, indent=2)
    except Exception:
        pass
    return None


def extract_markdown_from_doc(doc_json: Dict[str, Any]) -> str:
    data = doc_json.get("data", doc_json)
    if isinstance(data, dict):
        for key in ("content", "document", "body"):
            if key in data and isinstance(data[key], str):
                return data[key]
        return md(json.dumps(data, ensure_ascii=False, indent=2))
    return md(str(data))


def save_node(node: Dict[str, Any], rel_dir: Path, markdown: str, raw: Dict[str, Any]) -> None:
    title = safe_name(node.get("title", "untitled"))
    rel_dir.mkdir(parents=True, exist_ok=True)
    (rel_dir / f"{title}.md").write_text(markdown, encoding="utf-8")
    (rel_dir / f"{title}.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def node_type(node: Dict[str, Any]) -> str:
    return str(node.get("obj_type") or node.get("node_type") or "").lower()


def is_doc_node(node: Dict[str, Any]) -> bool:
    t = node_type(node)
    return any(x in t for x in ("doc", "document", "sheet", "bitable", "wiki")) or bool(node.get("obj_token"))


def walk(token: str, node: Dict[str, Any], path_parts: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    title = safe_name(node.get("title", "untitled"))
    current_parts = path_parts + (title,)
    rel_dir = OUT_DIR.joinpath(*current_parts)
    touched = []

    if is_doc_node(node) and node.get("obj_token"):
        doc_token = node["obj_token"]
        basic = get_doc_basic(token, doc_token)
        exported = try_export_doc(token, doc_token)
        markdown = exported if exported is not None else extract_markdown_from_doc(basic)
        raw = {"node": node, "doc": basic}
        save_node(node, rel_dir.parent, markdown, raw)
        touched.append(
            {
                "path": "/".join(current_parts),
                "title": title,
                "obj_token": doc_token,
                "hash": digest(raw),
            }
        )

    children = get_children(token, node["node_token"])
    for child in children:
        touched.extend(walk(token, child, current_parts))
    return touched


def main() -> None:
    token = get_token()
    root = get_root_node(token)
    index = {"space_id": SPACE_ID, "root": root, "items": walk(token, root)}
    STATE_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
