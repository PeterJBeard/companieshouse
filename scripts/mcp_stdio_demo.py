"""Exercise the server end to end over stdio, as Claude Desktop would.

    python scripts/mcp_stdio_demo.py /path/to/accounts.pdf [out_dir]

Writes each tool response to out_dir (default ./mcp_demo_out) and prints a summary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _texts(res) -> list[dict]:
    return [json.loads(c.text) for c in res.content if c.type == "text"]


def _images(res) -> list[bytes]:
    return [base64.b64decode(c.data) for c in res.content if c.type == "image"]


async def run(pdf: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("START_VIEWER", "0")
    params = StdioServerParameters(command=sys.executable, args=["-m", "ch_accounts_mcp.server"], env=env, cwd=str(Path(__file__).resolve().parent.parent))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            t = time.perf_counter()
            res = await s.call_tool("load_accounts", {"pdf_path": pdf})
            load = _texts(res)[0]
            (out / "load_accounts.json").write_text(json.dumps(load, indent=2, ensure_ascii=False))
            print(f"load_accounts: {time.perf_counter() - t:.1f}s cache_hit={load.get('cache_hit')} type={load.get('document_type')} pages={load.get('page_count')}")
            if "error" in load:
                print(load)
                return
            doc_id = load["doc_id"]

            t = time.perf_counter()
            res = await s.call_tool("get_primary_statements", {"doc_id": doc_id})
            prim = _texts(res)[0]
            imgs = _images(res)
            (out / "get_primary_statements.json").write_text(json.dumps(prim, indent=2, ensure_ascii=False))
            for i, im in enumerate(imgs):
                (out / f"unverified_crop_{i}.png").write_bytes(im)
            print(f"get_primary_statements: {time.perf_counter() - t:.1f}s cache_hit={prim.get('cache_hit')} summary={prim['verification']['summary']} inline_crops={len(imgs)}")

            first = next(v for st in prim["statements"] for r in st["rows"] for v in (r.get("values") or {}).values())
            res = await s.call_tool("get_citation", {"citation_id": first["citation_id"]})
            cit = _texts(res)[0]
            (out / "get_citation.json").write_text(json.dumps(cit, indent=2, ensure_ascii=False))
            for im in _images(res):
                (out / "citation_crop.png").write_bytes(im)
            print(f"get_citation {first['citation_id']}: {cit['label']!r} {cit['raw']} {cit['verification_status']} p.{cit['printed_page']} (PDF {cit['pdf_page']})")

            res = await s.call_tool("get_page_image", {"doc_id": doc_id, "page": cit["pdf_page"], "dpi": 100})
            (out / "page_image.png").write_bytes(_images(res)[0])
            print("get_page_image:", _texts(res)[0]["pdf_page"], "ok")

            for name, args in [
                ("compute", {"doc_id": doc_id, "metric": "net_assets"}),
                ("check_answer", {"doc_id": doc_id, "draft_text": f"{cit['label']} was {cit['raw']} (p.{cit['printed_page']}). Revenue grew to £987,654,321."}),
            ]:
                t = time.perf_counter()
                res = await s.call_tool(name, args)
                body = _texts(res)[0]
                (out / f"{name}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False))
                print(f"{name}: {time.perf_counter() - t:.1f}s", json.dumps(body.get("summary") or body.get("formula") or body.get("error"))[:300])


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "mcp_demo_out")))
