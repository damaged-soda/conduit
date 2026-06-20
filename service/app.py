"""conduit-service：FastAPI + SQLite。管理订阅（URL 拉取 / 文件导入）+ 节点池。core 仍是纯函数。

摄入两种：① **基于链接**：存订阅 URL，按 URL 拉取（GET → normalize → 导入）；② **文件导入**：贴内容。
网络抓取在服务侧（impure，见 service/fetch.py）；解析 / 身份是 core 纯函数。
TODO：tag、render+pull、定时刷新、认证、secret 加密。

跑：`uvicorn --factory service.app:make_app`（DB 路径用 CONDUIT_DB，默认 conduit.db）。
"""

from __future__ import annotations

import os
from typing import Callable

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from conduit.ingest import normalize

from .db import Store
from .fetch import fetch_url


class SubIn(BaseModel):
    id: str
    type: str = "clash"
    note: str = ""
    url: str | None = None


class ImportIn(BaseModel):
    raw: str


def _normalize_and_store(store: Store, sub: dict, raw: str) -> dict:
    try:
        nodes = normalize(raw, sub["type"], sub["id"])
    except (ValueError, yaml.YAMLError):  # sanitized 400：不回显订阅内容 / parser 细节
        raise HTTPException(400, "导入内容解析失败（请确认是合法的 clash 订阅）")
    return {"imported": store.import_nodes(sub["id"], raw, nodes)}


def create_app(db_path: str = ":memory:", fetcher: Callable[[str], str] = fetch_url) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="conduit-service", version="0.0.0")

    @app.post("/api/subscriptions")
    def add_subscription(body: SubIn):
        if body.url and not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "url 必须是 http(s)")  # SSRF 兜底：拒 file:// 等
        try:
            store.add_subscription(body.id, body.type, body.note, body.url)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/api/subscriptions")
    def list_subscriptions():
        return store.list_subscriptions()

    @app.post("/api/subscriptions/{sub_id}/import")
    def import_subscription(sub_id: str, body: ImportIn):
        sub = store.get_subscription(sub_id)
        if not sub:
            raise HTTPException(404, f"未知 subscription: {sub_id}")
        return _normalize_and_store(store, sub, body.raw)

    @app.post("/api/subscriptions/{sub_id}/refresh")
    def refresh_subscription(sub_id: str):
        sub = store.get_subscription(sub_id)
        if not sub:
            raise HTTPException(404, f"未知 subscription: {sub_id}")
        if not sub.get("url"):
            raise HTTPException(400, "该订阅没有 url，请用文件导入")
        try:
            raw = fetcher(sub["url"])
        except Exception:  # 网络/HTTP 失败 —— 不回显 url / 细节
            raise HTTPException(502, "拉取订阅 URL 失败")
        return _normalize_and_store(store, sub, raw)

    @app.get("/api/nodes")
    def list_nodes():
        return store.list_nodes()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _PAGE

    return app


_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>conduit</title>
<style>body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px;text-align:left}
textarea{width:100%;height:120px;box-sizing:border-box}input,button,select{padding:5px;margin:2px 0}
li{margin:4px 0}</style></head>
<body>
<h1>conduit</h1>
<h2>订阅</h2>
<form onsubmit="addSub(event)">
  <input id="sid" placeholder="订阅 id（如 vendor-a）" required>
  <input id="surl" placeholder="订阅 URL（可选，支持基于链接拉取）" size="48">
  <button>添加</button>
</form>
<ul id="subs"></ul>
<h2>文件导入（clash YAML）</h2>
<p>不联网时：在别处把订阅下好，内容贴这里导入。</p>
<select id="impSel"></select>
<textarea id="raw" placeholder="把 clash 订阅内容贴这里"></textarea><br>
<button onclick="doImport()">导入</button> <span id="msg"></span>
<h2>节点池</h2>
<table id="nodes"><thead><tr><th>type</th><th>server</th><th>port</th><th>名</th><th>来源</th></tr></thead><tbody></tbody></table>
<script>
// 全部数据走 textContent / DOM，避免订阅来的 raw_name 等造成 XSS
function el(t,x){const e=document.createElement(t);if(x!=null)e.textContent=x;return e}
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||r.status);return r.json()}
function msg(t){document.getElementById('msg').textContent=t}
async function refresh(){
  const subs=await j('/api/subscriptions');
  document.getElementById('subs').replaceChildren(...subs.map(s=>{
    const li=el('li',`${s.id} (${s.type}) — ${s.node_count} 节点 `);
    if(s.has_url){const b=el('button','🔄 按 URL 刷新');b.onclick=()=>doRefresh(s.id);li.appendChild(b)}
    return li;
  }));
  document.getElementById('impSel').replaceChildren(...subs.map(s=>{const o=el('option',s.id);o.value=s.id;return o}));
  const nodes=await j('/api/nodes');
  document.querySelector('#nodes tbody').replaceChildren(...nodes.map(n=>{const tr=document.createElement('tr');[n.type,n.server,n.port,n.raw_name,n.sub_id||''].forEach(v=>tr.appendChild(el('td',String(v))));return tr}));
}
async function addSub(e){e.preventDefault();const id=document.getElementById('sid'),url=document.getElementById('surl');await j('/api/subscriptions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({id:id.value,url:url.value||null})});id.value='';url.value='';refresh()}
async function doImport(){try{const r=await j('/api/subscriptions/'+encodeURIComponent(document.getElementById('impSel').value)+'/import',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({raw:document.getElementById('raw').value})});msg('导入 '+r.imported+' 节点');refresh()}catch(e){msg('失败: '+e.message)}}
async function doRefresh(id){try{const r=await j('/api/subscriptions/'+encodeURIComponent(id)+'/refresh',{method:'POST'});msg('刷新 '+id+'：导入 '+r.imported+' 节点');refresh()}catch(e){msg('刷新失败: '+e.message)}}
refresh();
</script>
</body></html>
"""


def make_app() -> FastAPI:
    """uvicorn 入口（factory）：避免 import 时就建 DB。`uvicorn --factory service.app:make_app`。"""
    return create_app(os.environ.get("CONDUIT_DB", "conduit.db"))
