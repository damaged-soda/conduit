"""conduit-service：FastAPI + SQLite。订阅管理（主从式 CRUD）+ 节点池。core 仍是纯函数。

订阅 = 命名的节点桶（id 内部不透明、name 可随意改）；两种摄入：① 基于链接（存 URL，按 URL 拉取）
② 文件导入（贴内容）。网络抓取在服务侧（impure，见 service/fetch.py）；解析/身份是 core 纯函数。
TODO：tag、render+pull、定时刷新、认证、secret 加密。

跑：`uvicorn --factory service.app:make_app`（DB 路径用 CONDUIT_DB，默认 conduit.db）。
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from conduit.ingest import normalize
from conduit.render import render_subscription
from conduit.tags import normalize_region, region_of

from .db import Store
from .fetch import fetch_url


class SubIn(BaseModel):
    name: str
    type: str = "clash"
    note: str = ""
    url: str | None = None


class SubPatch(BaseModel):
    name: str | None = None
    url: str | None = None


class ImportIn(BaseModel):
    raw: str


class TagIn(BaseModel):
    region: str | None = None  # 留空=清除覆盖（用自动 region）
    quarantined: bool | None = None


def _check_url(url: str | None) -> None:
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url 必须是 http(s)")  # SSRF 兜底：拒 file:// 等


def _normalize_and_store(store: Store, sub: dict, raw: str) -> dict:
    try:
        nodes = normalize(raw, sub["type"], sub["id"])
    except (ValueError, TypeError, yaml.YAMLError):  # sanitized 400：不回显订阅内容 / parser 细节
        raise HTTPException(400, "导入内容解析失败（请确认是合法的 clash 订阅）")
    return {"imported": store.import_nodes(sub["id"], raw, nodes)}


def create_app(db_path: str = ":memory:", fetcher: Callable[[str], str] = fetch_url) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="conduit-service", version="0.0.0")

    def _require(sub_id: str) -> dict:
        sub = store.get_subscription(sub_id)
        if not sub:
            raise HTTPException(404, f"未知 subscription: {sub_id}")
        return sub

    def _with_region(rows: list[dict]) -> list[dict]:
        """给节点行补上 region（自动 + 覆盖 + 生效）+ 隔离状态，供页面展示/打标。"""
        tags = store.get_node_tags()
        out = []
        for n in rows:
            t = tags.get(n["access_id"], {})
            auto = region_of(n.get("raw_name", ""))
            out.append({**n, "region_auto": auto, "region_override": t.get("region"),
                        "region": t.get("region") or auto, "quarantined": t.get("quarantined", False)})
        return out

    @app.post("/api/subscriptions")
    def add_subscription(body: SubIn):
        _check_url(body.url)
        return {"id": store.add_subscription(body.name, body.type, body.note, body.url)}

    @app.get("/api/subscriptions")
    def list_subscriptions():
        return store.list_subscriptions()

    @app.patch("/api/subscriptions/{sub_id}")
    def update_subscription(sub_id: str, body: SubPatch):
        _require(sub_id)
        _check_url(body.url)
        store.update_subscription(sub_id, body.name, body.url)
        return {"ok": True}

    @app.delete("/api/subscriptions/{sub_id}")
    def delete_subscription(sub_id: str):
        _require(sub_id)
        store.delete_subscription(sub_id)
        return {"ok": True}

    @app.get("/api/subscriptions/{sub_id}/nodes")
    def subscription_nodes(sub_id: str):
        _require(sub_id)
        return _with_region(store.list_nodes(sub_id))

    @app.put("/api/nodes/{access_id}/tag")
    def set_node_tag(access_id: str, body: TagIn):
        kwargs: dict = {}  # 只更新本次提供的字段（部分更新；未传的保持不变）
        if "region" in body.model_fields_set:
            try:
                kwargs["region"] = normalize_region(body.region)
            except ValueError as e:
                raise HTTPException(400, str(e))
        if "quarantined" in body.model_fields_set:
            kwargs["quarantined"] = bool(body.quarantined)
        store.set_node_tag(access_id, **kwargs)
        return {"ok": True}

    @app.post("/api/subscriptions/{sub_id}/import")
    def import_subscription(sub_id: str, body: ImportIn):
        return _normalize_and_store(store, _require(sub_id), body.raw)

    @app.post("/api/subscriptions/{sub_id}/refresh")
    def refresh_subscription(sub_id: str):
        sub = _require(sub_id)
        if not sub.get("url"):
            raise HTTPException(400, "该订阅没有 url，请用文件导入")
        try:
            raw = fetcher(sub["url"])
        except Exception:  # 网络/HTTP 失败 —— 不回显 url / 细节
            raise HTTPException(502, "拉取订阅 URL 失败")
        return _normalize_and_store(store, sub, raw)

    @app.get("/api/nodes")
    def list_nodes():
        return _with_region(store.list_nodes())

    @app.get("/api/sub-token")
    def sub_token():
        return {"token": store.get_sub_token()}

    @app.get("/sub/clash")
    def sub_clash(token: str = "", full: bool = False):
        # 订阅产物含明文节点凭据 → 必须 token（常量时间比较）。私网/tailnet 直连兜底在 render 内置。
        if not secrets.compare_digest(token, store.get_sub_token()):
            raise HTTPException(403, "bad token")
        cfg = render_subscription(store.nodes_for_render(), {}, full=full, tags=store.get_node_tags())
        # 标准订阅响应头：让 clash-verge/mihomo 当订阅文件处理（否则浏览器直接显示、客户端导入失败）。
        return Response(
            cfg,
            media_type="text/yaml; charset=utf-8",
            headers={
                "content-disposition": 'attachment; filename="conduit.yaml"',
                "profile-update-interval": "24",  # 小时
                "access-control-allow-origin": "*",  # 防客户端在 webview 里 fetch 被 CORS 拦
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _PAGE

    return app


_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>conduit</title>
<style>
body{font-family:system-ui,sans-serif;max-width:1000px;margin:1.5rem auto;padding:0 1rem}
.wrap{display:flex;gap:1.5rem;align-items:flex-start}
.left{width:260px;flex:none}
.right{flex:1;min-width:0}
ul{list-style:none;padding:0;margin:.5rem 0}
li{padding:6px 8px;border:1px solid #ddd;border-radius:6px;margin:4px 0;cursor:pointer}
li.sel{background:#eef;border-color:#88a}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:3px 7px;font-size:12px;text-align:left}
input,button,select,textarea{padding:5px;margin:2px 0;font-size:13px}
input[type=text]{width:100%;box-sizing:border-box}
textarea{width:100%;height:110px;box-sizing:border-box}
.row{margin:.6rem 0}.muted{color:#888;font-size:12px}.msg{color:#06c;font-size:12px}
</style></head>
<body>
<h1>conduit</h1>
<div id="sub" class="muted" style="margin-bottom:1rem"></div>
<div class="wrap">
  <div class="left">
    <button onclick="newSub()">＋ 新建订阅</button>
    <ul id="subs"></ul>
  </div>
  <div class="right" id="detail"><p class="muted">左边选一条订阅，或点「新建订阅」。</p></div>
</div>
<script>
// 全部数据走 textContent / DOM，避免订阅来的 raw_name 等造成 XSS
let SUBS=[], SEL=null;
function el(t,x){const e=document.createElement(t);if(x!=null)e.textContent=x;return e}
function input(ph,val){const e=document.createElement('input');e.type='text';e.placeholder=ph||'';if(val!=null)e.value=val;return e}
function btn(label,fn){const b=el('button',label);b.onclick=fn;return b}
function row(...kids){const d=document.createElement('div');d.className='row';kids.forEach(k=>d.append(k));return d}
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||r.status);return r.json()}
function jpost(u,body){return j(u,{method:'POST',headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined})}

async function loadSubs(){
  SUBS=await j('/api/subscriptions');
  const ul=document.getElementById('subs');
  ul.replaceChildren(...SUBS.map(s=>{
    const li=el('li',`${s.name||'(未命名)'} · ${s.node_count}`);
    if(s.id===SEL)li.className='sel';
    li.onclick=()=>select(s.id);
    return li;
  }));
}
function setMsg(t){const m=document.getElementById('msg');if(m)m.textContent=t||''}

async function select(id){SEL=id;await loadSubs();await renderDetail();}
function newSub(){SEL=null;loadSubs();renderNew();}

function renderNew(){
  const d=document.getElementById('detail');d.replaceChildren();
  const name=input('订阅名字（随意，可改）'), url=input('订阅 URL（可选，http/https）');
  d.append(el('h2','新建订阅'),
    row(el('label','名字：'),name),
    row(el('label','URL：'),url),
    row(btn('创建',async()=>{try{const r=await jpost('/api/subscriptions',{name:name.value,url:url.value||null});await select(r.id)}catch(e){alert('创建失败: '+e.message)}})),
    el('p',null));
}

async function renderDetail(){
  const sub=SUBS.find(s=>s.id===SEL);
  const d=document.getElementById('detail');d.replaceChildren();
  if(!sub){d.append(el('p','订阅不存在'));return}
  const name=input('名字',sub.name);
  const url=input(sub.has_url?'替换 URL（留空不改）':'设置 URL（http/https）');
  const raw=document.createElement('textarea');raw.placeholder='或：把 clash 订阅内容贴这里';
  const msg=el('span','');msg.className='msg';msg.id='msg';

  d.append(
    el('h2',sub.name||'(未命名)'),
    el('div', `${sub.type} · ${sub.node_count} 节点 · URL ${sub.has_url?'已设置':'未设置'}`),
    row(el('label','名字：'),name,btn('保存名字',async()=>{await patch({name:name.value});setMsg('已改名')})),
    row(el('label','URL：'),url,
        btn('保存 URL',async()=>{if(!url.value){setMsg('URL 留空，未改');return}await patch({url:url.value});setMsg('URL 已更新')}),
        btn('🔄 按 URL 刷新',async()=>{try{const r=await jpost(`/api/subscriptions/${SEL}/refresh`);const n=r.imported;await select(SEL);setMsg('刷新：导入 '+n+' 节点')}catch(e){setMsg('刷新失败: '+e.message)}})),
    el('div','文件导入：'), raw,
    row(btn('导入文件',async()=>{try{const r=await jpost(`/api/subscriptions/${SEL}/import`,{raw:raw.value});const n=r.imported;await select(SEL);setMsg('导入 '+n+' 节点')}catch(e){setMsg('导入失败: '+e.message)}}),
        btn('🗑 删除订阅',async()=>{if(confirm('删除该订阅及其节点？')){await j(`/api/subscriptions/${SEL}`,{method:'DELETE'});SEL=null;await loadSubs();document.getElementById('detail').replaceChildren(el('p','已删除。'))}}),
        msg),
  );
  const nbox=document.createElement('div');
  d.append(el('h3','节点 / 标签'), nbox);
  await loadNodes(nbox);
}

async function loadNodes(box){
  const nodes=await j(`/api/subscriptions/${SEL}/nodes`);
  const tbl=document.createElement('table');
  const head=document.createElement('tr');['名','region','隔离','类型','地址'].forEach(h=>head.append(el('th',h)));
  tbl.append(head);
  nodes.forEach(n=>{
    const tr=document.createElement('tr');
    if(n.quarantined)tr.style.opacity='0.4';
    tr.append(el('td',n.raw_name));
    const ri=input(n.region_auto,n.region_override||'');ri.style.width='60px';
    ri.onchange=async()=>{await setTag(n.access_id,ri.value,n.quarantined);await loadNodes(box)};
    const rtd=document.createElement('td');rtd.append(ri);tr.append(rtd);
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=!!n.quarantined;
    cb.onchange=async()=>{await setTag(n.access_id,n.region_override,cb.checked);await loadNodes(box)};
    const qtd=document.createElement('td');qtd.append(cb);tr.append(qtd);
    tr.append(el('td',n.type),el('td',`${n.server}:${n.port}`));
    tbl.append(tr);
  });
  const hint=el('div',`共 ${nodes.length} · region 留空=自动(占位符)/填了=覆盖；勾选=隔离(不进任何组)`);hint.className='muted';
  box.replaceChildren(hint,tbl);
}
async function setTag(aid,region,quarantined){
  await fetch('/api/nodes/'+encodeURIComponent(aid)+'/tag',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({region:region||null,quarantined:!!quarantined})});
}

async function patch(body){await j(`/api/subscriptions/${SEL}`,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify(body)});await loadSubs();await renderDetail();}

async function loadSub(){
  const r=await j('/api/sub-token');
  const base=location.origin+'/sub/clash?token='+encodeURIComponent(r.token);
  document.getElementById('sub').replaceChildren(
    el('div','clash 订阅（导入 clash-verge / mihomo）：'), el('code',base),
    el('div','带 DNS/TUN：'), el('code',base+'&full=1'));
}
loadSubs(); loadSub();
</script>
</body></html>
"""


def make_app() -> FastAPI:
    """uvicorn 入口（factory）：避免 import 时就建 DB。`uvicorn --factory service.app:make_app`。"""
    return create_app(os.environ.get("CONDUIT_DB", "conduit.db"))
