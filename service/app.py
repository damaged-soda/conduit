"""conduit-service：FastAPI + SQLite。管理订阅（文件导入）+ 节点池。core 仍是纯函数。

skeleton 能力：建订阅、导入 clash 内容（→ normalize → 存节点）、列订阅/节点、一个简单页面。
TODO：tag（地区/人工）、render + pull（各机拉配置）、health、traffic、认证、secret 处理。

跑：`uvicorn --factory service.app:make_app`（DB 路径用环境变量 CONDUIT_DB，默认 conduit.db）。
"""

from __future__ import annotations

import os

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from conduit.ingest import normalize

from .db import Store


class SubIn(BaseModel):
    id: str
    type: str = "clash"
    note: str = ""


class ImportIn(BaseModel):
    raw: str


def create_app(db_path: str = ":memory:") -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="conduit-service", version="0.0.0")

    @app.post("/api/subscriptions")
    def add_subscription(body: SubIn):
        try:
            store.add_subscription(body.id, body.type, body.note)
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
        try:
            nodes = normalize(body.raw, sub["type"], sub_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except yaml.YAMLError:  # 坏 YAML → sanitized 400，不回显内容/parser 细节
            raise HTTPException(400, "导入内容不是合法的 clash YAML")
        return {"imported": store.import_nodes(sub_id, body.raw, nodes)}

    @app.get("/api/nodes")
    def list_nodes():
        return store.list_nodes()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _PAGE

    return app


_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>conduit</title>
<style>body{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px;text-align:left}
textarea{width:100%;height:120px;box-sizing:border-box}input,button,select{padding:5px}</style></head>
<body>
<h1>conduit</h1>
<h2>订阅</h2>
<form onsubmit="addSub(event)">
  <input id="sid" placeholder="订阅 id（如 vendor-a）" required>
  <button>添加</button>
</form>
<ul id="subs"></ul>
<h2>导入订阅内容（clash YAML）</h2>
<p>在 MBA 把订阅下好，内容贴这里导入（服务不联网）。</p>
<select id="impSel"></select>
<textarea id="raw" placeholder="把 clash 订阅内容贴这里"></textarea><br>
<button onclick="doImport()">导入</button> <span id="impMsg"></span>
<h2>节点池</h2>
<table id="nodes"><thead><tr><th>type</th><th>server</th><th>port</th><th>名</th><th>来源</th></tr></thead><tbody></tbody></table>
<script>
// 全部数据走 textContent（非 innerHTML），避免订阅来的 raw_name 等造成 XSS
function el(t,x){const e=document.createElement(t);if(x!=null)e.textContent=x;return e}
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||r.status);return r.json()}
async function refresh(){
  const subs=await j('/api/subscriptions');
  document.getElementById('subs').replaceChildren(...subs.map(s=>el('li',`${s.id} (${s.type}) — ${s.node_count} 节点`)));
  document.getElementById('impSel').replaceChildren(...subs.map(s=>{const o=el('option',s.id);o.value=s.id;return o}));
  const nodes=await j('/api/nodes');
  document.querySelector('#nodes tbody').replaceChildren(...nodes.map(n=>{const tr=document.createElement('tr');[n.type,n.server,n.port,n.raw_name,n.sub_id||''].forEach(v=>tr.appendChild(el('td',String(v))));return tr}));
}
async function addSub(e){e.preventDefault();const s=document.getElementById('sid');await j('/api/subscriptions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({id:s.value})});s.value='';refresh()}
async function doImport(){const m=document.getElementById('impMsg');try{const id=document.getElementById('impSel').value;const r=await j('/api/subscriptions/'+encodeURIComponent(id)+'/import',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({raw:document.getElementById('raw').value})});m.textContent='导入 '+r.imported+' 节点';refresh()}catch(e){m.textContent='失败: '+e.message}}
refresh();
</script>
</body></html>
"""


def make_app() -> FastAPI:
    """uvicorn 入口（factory）：避免 import 时就建 DB。`uvicorn --factory service.app:make_app`。"""
    return create_app(os.environ.get("CONDUIT_DB", "conduit.db"))
