"""conduit-service：FastAPI + SQLite。订阅管理（主从式 CRUD）+ 节点池。core 仍是纯函数。

订阅 = 命名的节点桶（id 内部不透明、name 可随意改）；两种摄入：① 基于链接（存 URL，按 URL 拉取）
② 文件导入（贴内容）。网络抓取在服务侧（impure，见 service/fetch.py）；解析/身份是 core 纯函数。
TODO：tag、render+pull、定时刷新、认证、secret 加密。

跑：`uvicorn --factory service.app:make_app`（DB 路径用 CONDUIT_DB，默认 conduit.db）。
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
from typing import Callable

import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from conduit.ingest import normalize
from conduit.policy import DEFAULT_POLICY, GEOIP_CATALOG, GEOSITE_CATALOG
from conduit.render import render_subscription, subscription_rules
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


class RouteIn(BaseModel):
    name: str = ""
    to: str
    geosite: list[str] = []
    geoip: list[str] = []
    rule_set: list[str] = []
    domain_suffix: list[str] = []
    domain: list[str] = []
    ip_cidr: list[str] = []
    process_name: list[str] = []
    dst_port: list[str] = []


class PolicyIn(BaseModel):
    routes: list[RouteIn] = []
    final: str = "PROXY"
    dns: dict = {}  # {nameserver_policy: {pattern: server}}（full 模式 tailnet DNS）


_MRS_BASE = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo"
_RULESET_NAME = re.compile(r"^[A-Za-z0-9_!.-]+$")  # 防 ../ 路径穿越
_DOMAINPAT = re.compile(r"[A-Za-z0-9_.*!+-]{1,100}")  # 显式域名匹配（无逗号换行 → 防规则注入）
_PLAINVAL = re.compile(r"[^,\n\r]{1,100}")  # 进程名等（无逗号换行）
_PORTPAT = re.compile(r"\d{1,5}(-\d{1,5})?")


def _validate_matchers(r) -> None:
    """显式匹配值格式校验（用户自填 → 防规则注入 / 坏值）。"""
    for d in [*r.domain_suffix, *r.domain]:
        if not _DOMAINPAT.fullmatch(d):
            raise HTTPException(400, f"非法域名：{d}")
    norm = []
    for c in r.ip_cidr:
        try:
            norm.append(str(ipaddress.ip_network(c, strict=False)))  # 规范化：单 IP → /32，避免不确定的 IP-CIDR
        except ValueError:
            raise HTTPException(400, f"非法 IP/CIDR：{c}")
    r.ip_cidr = norm
    for p in r.process_name:
        if not _PLAINVAL.fullmatch(p):
            raise HTTPException(400, f"非法进程名：{p}")
    for p in r.dst_port:
        if not _PORTPAT.fullmatch(p):
            raise HTTPException(400, f"非法端口：{p}")
        parts = [int(x) for x in p.split("-")]
        if any(x > 65535 for x in parts) or (len(parts) == 2 and parts[0] > parts[1]):
            raise HTTPException(400, f"非法端口：{p}")


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

    def _policy() -> dict:
        # DB 为准；无则回落仓库 DEFAULT。rule_providers 始终服务端控制（不让 PUT 注入任意 URL，防 SSRF）。
        stored = store.get_policy()
        if not stored:
            return DEFAULT_POLICY
        return {
            "rule_providers": DEFAULT_POLICY.get("rule_providers", {}),
            "routes": stored.get("routes", []),
            "final": stored.get("final", "PROXY"),
            "dns": stored.get("dns", {}),
        }

    def _groups() -> list[str]:
        tags = store.get_node_tags()
        regions: list[str] = []
        for n in store.nodes_for_render():
            t = tags.get(n.access_id.value, {})
            if t.get("quarantined"):
                continue
            reg = (t.get("region") or "").strip() or region_of(n.raw_name)
            if reg not in regions:
                regions.append(reg)
        return ["DIRECT", "REJECT", "PROXY", "AUTO", *sorted(regions)]

    @app.get("/api/policy")
    def get_policy():
        pol = _policy()
        return {"policy": pol, "rules": subscription_rules({}, pol), "custom": store.get_policy() is not None}

    @app.get("/api/groups")
    def list_groups():
        return {"targets": _groups()}

    @app.get("/api/categories")
    def list_categories():
        # 编辑器从这里取可选类别（白名单 → 防写出加载失败的坏类别）。
        return {"geosite": GEOSITE_CATALOG, "geoip": GEOIP_CATALOG, "rule_set": list(DEFAULT_POLICY.get("rule_providers", {}))}

    @app.put("/api/policy")
    def put_policy(body: PolicyIn):
        if len(body.routes) > 100:
            raise HTTPException(400, "routes 过多")
        valid_rs = set(DEFAULT_POLICY.get("rule_providers", {}))
        # 服务端白名单校验（前端预检不是安全边界，API 直调也得挡住坏类别）
        for r in body.routes:
            for cat in r.geosite:
                if cat not in GEOSITE_CATALOG:
                    raise HTTPException(400, f"未知 geosite 类别：{cat}")
            for cat in r.geoip:
                if cat not in GEOIP_CATALOG:
                    raise HTTPException(400, f"未知 geoip 类别：{cat}")
            for rs in r.rule_set:
                if rs not in valid_rs:
                    raise HTTPException(400, f"未知规则集：{rs}（仅支持 {sorted(valid_rs)}）")
            _validate_matchers(r)
        nsp = body.dns.get("nameserver_policy", {})
        if not isinstance(nsp, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or "\n" in (k + v) for k, v in nsp.items()
        ):
            raise HTTPException(400, "nameserver_policy 非法")
        store.set_policy({"routes": [r.model_dump() for r in body.routes], "final": body.final, "dns": body.dns})
        return {"ok": True}

    @app.delete("/api/policy")
    def reset_policy():
        store.set_policy(None)  # 恢复仓库默认
        return {"ok": True}

    @app.get("/api/ruleset")
    def inspect_ruleset(kind: str, name: str):
        # 看某个类别/规则集里到底匹配什么（拉 MetaCubeX 可读 .list；服务侧抓取）。
        if not _RULESET_NAME.match(name or ""):
            raise HTTPException(400, "非法名字")
        if kind == "ruleset":
            spec = DEFAULT_POLICY.get("rule_providers", {}).get(name)
            if not spec:
                raise HTTPException(404, "未知规则集")
            url = spec["url"][:-4] + ".list" if spec["url"].endswith(".mrs") else spec["url"]
        elif kind in ("geosite", "geoip"):
            url = f"{_MRS_BASE}/{kind}/{name.lower()}.list"
        else:
            raise HTTPException(400, "kind 只能是 geosite/geoip/ruleset")
        try:
            text = fetcher(url)
        except Exception:
            raise HTTPException(502, "拉取规则集失败")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        return {"name": name, "count": len(lines), "sample": lines[:80], "url": url}

    @app.get("/sub/clash")
    def sub_clash(token: str = "", full: bool = False):
        # 订阅产物含明文节点凭据 → 必须 token（常量时间比较）。私网/tailnet 直连兜底在 render 内置。
        if not secrets.compare_digest(token, store.get_sub_token()):
            raise HTTPException(403, "bad token")
        cfg = render_subscription(
            store.nodes_for_render(), {}, full=full, tags=store.get_node_tags(), policy=_policy()
        )
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
details{margin:3px 0;border:1px solid #e3e3e3;border-radius:6px}
summary{cursor:pointer;font-weight:600;font-size:13px;padding:5px 8px;user-select:none}
.nrow{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:3px 8px 3px 22px;font-size:12px;border-top:1px solid #f0f0f0}
.nrow>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nctl{display:flex;gap:6px;align-items:center;flex:none}
</style></head>
<body>
<h1>conduit</h1>
<div id="sub" class="muted" style="margin-bottom:1rem"></div>
<div id="policy" class="muted" style="margin-bottom:1rem"></div>
<div class="wrap">
  <div class="left">
    <button onclick="newSub()">＋ 新建订阅</button>
    <ul id="subs"></ul>
  </div>
  <div class="right" id="detail"><p class="muted">左边选一条订阅，或点「新建订阅」。</p></div>
</div>
<script>
// 全部数据走 textContent / DOM，避免订阅来的 raw_name 等造成 XSS
let SUBS=[], SEL=null, NOPEN=new Set();  // NOPEN：记住展开的地区，re-render 时保留
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

const STDREGIONS=['HK','TW','JP','SG','US','KR','GB','DE','FR','NL','CA','AU'];  // 下拉常驻地区
async function loadNodes(box){
  const nodes=await j(`/api/subscriptions/${SEL}/nodes`);
  const byR={}, q=[];
  nodes.forEach(n=>{ if(n.quarantined) q.push(n); else (byR[n.region]=byR[n.region]||[]).push(n); });
  // 下拉选项 = 常驻地区 ∪ 本订阅出现过的所有 region/自动 region
  const allR=[...new Set([...STDREGIONS,...nodes.map(n=>n.region),...nodes.map(n=>n.region_auto)])].filter(Boolean).sort();
  const wrap=document.createElement('div');
  Object.keys(byR).sort((a,b)=>byR[b].length-byR[a].length||a.localeCompare(b)).forEach(r=>wrap.append(regionNode(r,byR[r],box,allR)));
  if(q.length) wrap.append(regionNode('🚫 隔离区',q,box,allR));
  const hint=el('div',`共 ${nodes.length} 个 · ${Object.keys(byR).length} 地区 · 隔离 ${q.length}　|　点地区展开；下拉选 region(自动/覆盖)、勾选=隔离`);hint.className='muted';
  box.replaceChildren(hint,wrap);
}
function regionNode(region,list,box,allR){
  const d=document.createElement('details');d.open=NOPEN.has(region);
  d.addEventListener('toggle',()=>{d.open?NOPEN.add(region):NOPEN.delete(region)});
  const s=document.createElement('summary');s.textContent=`${region} · ${list.length}`;d.append(s);
  list.forEach(n=>{
    const row=document.createElement('div');row.className='nrow';
    row.append(el('span',n.raw_name));
    const sel=document.createElement('select');sel.title='region：自动 / 覆盖到某地区';
    const o0=document.createElement('option');o0.value='';o0.textContent='自动·'+n.region_auto;sel.append(o0);
    allR.forEach(r=>{const o=document.createElement('option');o.value=r;o.textContent=r;sel.append(o)});
    sel.value=n.region_override||'';
    sel.onchange=async()=>{await setTag(n.access_id,sel.value,n.quarantined);await loadNodes(box)};
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=!!n.quarantined;cb.title='隔离';
    cb.onchange=async()=>{await setTag(n.access_id,n.region_override,cb.checked);await loadNodes(box)};
    const ctl=document.createElement('span');ctl.className='nctl';ctl.append(sel,cb);
    row.append(ctl);d.append(row);
  });
  return d;
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
let POL=null, TARGETS=[], CATS={}, CUSTOM=false, EDIT=false, RULES=[];
const MKEYS=['domain_suffix','domain','ip_cidr','process_name','dst_port','geosite','geoip','rule_set'];
const MLABEL={geosite:'geosite',geoip:'geoip',rule_set:'规则集',domain_suffix:'域名后缀',domain:'域名',ip_cidr:'IP段',process_name:'进程',dst_port:'端口'};
const CATKINDS=['geosite','geoip','rule_set'];
async function loadPolicy(){
  const d=await j('/api/policy');
  POL={routes:JSON.parse(JSON.stringify(d.policy.routes||[])),final:d.policy.final||'PROXY',dns:JSON.parse(JSON.stringify(d.policy.dns||{}))};
  CUSTOM=d.custom;RULES=d.rules;EDIT=false;
  try{TARGETS=(await j('/api/groups')).targets}catch(e){TARGETS=['DIRECT','REJECT','PROXY','AUTO']}
  try{CATS=await j('/api/categories')}catch(e){CATS={geosite:[],geoip:[],rule_set:[]}}
  renderPolicy();
}
function targetSel(val,fn){const s=document.createElement('select');const opts=TARGETS.slice();if(opts.indexOf(val)<0)opts.push(val);
  opts.forEach(t=>{const o=document.createElement('option');o.value=o.textContent=t;s.append(o)});s.value=val;s.onchange=()=>fn(s.value);return s}
function renderPolicy(){
  const box=document.getElementById('policy');box.replaceChildren();
  const hdr=document.createElement('div');hdr.append(el('b','分流规则'),el('span',CUSTOM?'（已自定义，存于服务）':'（仓库默认）'),' ');
  hdr.append(btn(EDIT?'✕ 取消':'✎ 编辑',()=>{if(EDIT)loadPolicy();else{EDIT=true;renderPolicy()}}));
  box.append(hdr,EDIT?editorView():readonlyView());
}
function readonlyView(){
  const out=document.createElement('div');
  out.append(el('div','匹配 → 目标组；从上到下首命中。点类别看里面匹配什么。'));
  const tbl=document.createElement('table');tbl.style.cssText='max-width:660px;font-size:12px';
  const insp=document.createElement('div');insp.className='muted';insp.style.cssText='margin-top:6px;font-size:11px;word-break:break-all';
  const chip=(kind,nm)=>{const lbl=MLABEL[kind]+':'+nm;
    if(CATKINDS.indexOf(kind)<0){const s=el('span',lbl);s.style.marginRight='10px';return s}
    const a=document.createElement('a');a.href='#';a.textContent=lbl;a.style.marginRight='10px';
    a.onclick=async(e)=>{e.preventDefault();insp.textContent='加载 '+nm+' …';try{const d=await j('/api/ruleset?kind='+(kind==='rule_set'?'ruleset':kind)+'&name='+encodeURIComponent(nm));insp.replaceChildren(el('b',nm+'：'+d.count+' 条匹配'),el('span','　'+d.sample.slice(0,50).join('   ')))}catch(e2){insp.textContent=nm+' 看不了：'+e2.message}};return a};
  const row3=(nm,cs,to)=>{const tr=document.createElement('tr');const c1=el('td',nm);c1.style.fontWeight='600';const c2=document.createElement('td');cs.forEach(e=>c2.append(e));tr.append(c1,c2,el('td','→ '+to));tbl.append(tr)};
  row3('私网 / tailnet',[el('span','rule#0 兜底')],'DIRECT');
  POL.routes.forEach(r=>row3(r.name||'(规则)',MKEYS.flatMap(k=>(r[k]||[]).map(x=>chip(k,x))),r.to));
  const nsp=(POL.dns||{}).nameserver_policy||{};
  if(Object.keys(nsp).length)row3('DNS解析(full)',[el('span',Object.entries(nsp).map(e=>e[0]+'→'+e[1]).join('  '))],'—');
  row3('其余',[el('span','兜底 MATCH')],POL.final);
  out.append(tbl,insp);
  const det=document.createElement('details');const sm=document.createElement('summary');sm.textContent='查看生成的 '+RULES.length+' 条 mihomo 规则';det.append(sm);
  const ul=document.createElement('ul');ul.style.cssText='margin:4px 0;padding-left:18px;font-size:11px';RULES.forEach(r=>{const li=document.createElement('li');li.textContent=r;ul.append(li)});det.append(ul);out.append(det);
  return out;
}
function editorView(){
  const out=document.createElement('div');
  out.append(el('div','每条 = 一组匹配 → 目标；顺序即优先级（↑↓ 调）。改完点保存。'));
  POL.routes.forEach((r,i)=>out.append(routeEditor(r,i)));
  out.append(row(btn('＋ 添加规则',()=>{POL.routes.push({name:'',to:'PROXY',geosite:[],geoip:[],rule_set:[]});renderPolicy()})));
  out.append(row(el('span','其余（兜底）→ '),targetSel(POL.final,v=>POL.final=v)));
  out.append(el('h4','DNS 解析策略（仅 full 模式 · 如 tailnet +.ts.net 走 MagicDNS）'));
  POL.dns=POL.dns||{};const nsp=POL.dns.nameserver_policy=POL.dns.nameserver_policy||{};
  Object.keys(nsp).forEach(pat=>out.append(row(el('span',pat),el('span','→'),el('span',nsp[pat]),btn('×',()=>{delete nsp[pat];renderPolicy()}))));
  const pIn=input('模式 如 +.ts.net');pIn.style.width='130px';const sIn=input('DNS 如 100.100.100.100');sIn.style.width='150px';
  out.append(row(el('span','加 DNS:'),pIn,el('span','→'),sIn,btn('+',()=>{const p=pIn.value.trim(),s=sIn.value.trim();if(p&&s){nsp[p]=s;renderPolicy()}})));
  const save=btn('保存',async()=>{try{const r=await fetch('/api/policy',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify(POL)});if(!r.ok)throw new Error((await r.json()).detail||r.status);await loadPolicy()}catch(e){alert('保存失败: '+e.message)}});
  const reset=btn('恢复默认',async()=>{if(confirm('恢复仓库默认策略？')){await fetch('/api/policy',{method:'DELETE'});await loadPolicy()}});
  out.append(row(save,reset));
  return out;
}
function routeEditor(r,i){
  const d=document.createElement('div');d.style.cssText='border:1px solid #ddd;border-radius:6px;padding:6px;margin:5px 0';
  const nm=input('规则名',r.name);nm.style.width='110px';nm.onchange=()=>r.name=nm.value;
  const up=btn('↑',()=>{if(i>0){const t=POL.routes[i-1];POL.routes[i-1]=POL.routes[i];POL.routes[i]=t;renderPolicy()}});
  const dn=btn('↓',()=>{if(i<POL.routes.length-1){const t=POL.routes[i+1];POL.routes[i+1]=POL.routes[i];POL.routes[i]=t;renderPolicy()}});
  const del=btn('✕ 删',()=>{POL.routes.splice(i,1);renderPolicy()});
  d.append(row(el('b','#'+(i+1)),nm,el('span','→'),targetSel(r.to,v=>r.to=v),up,dn,del));
  const mbox=document.createElement('div');mbox.style.cssText='margin:4px 0';
  MKEYS.forEach(k=>(r[k]||[]).forEach((n,idx)=>{
    const s=document.createElement('span');s.style.cssText='display:inline-block;border:1px solid #bbb;border-radius:10px;padding:1px 4px 1px 8px;margin:2px;font-size:11px';
    s.append(el('span',MLABEL[k]+':'+n));s.append(btn('×',()=>{r[k].splice(idx,1);renderPolicy()}));mbox.append(s)}));
  d.append(mbox);
  const ks=document.createElement('select');[['domain_suffix','域名后缀'],['domain','域名'],['ip_cidr','IP段'],['process_name','进程名'],['dst_port','端口'],['geosite','geosite类别'],['geoip','geoip类别'],['rule_set','规则集']].forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;ks.append(o)});
  const holder=document.createElement('span');let getVal=()=>'';
  const fillVal=()=>{holder.replaceChildren();const k=ks.value;
    if(CATKINDS.indexOf(k)>=0){const s=document.createElement('select');(CATS[k]||[]).forEach(n=>{const o=document.createElement('option');o.value=o.textContent=n;s.append(o)});holder.append(s);getVal=()=>s.value}
    else{const ph=k==='ip_cidr'?'如 100.64.0.0/10':k==='dst_port'?'如 22':k==='process_name'?'如 ssh':'如 tailscale.com';const t=input(ph);t.style.width='150px';holder.append(t);getVal=()=>t.value.trim()}};
  ks.onchange=fillVal;fillVal();
  const add=btn('＋匹配',()=>{const k=ks.value,name=getVal();if(!name)return;r[k]=r[k]||[];if(r[k].indexOf(name)<0)r[k].push(name);renderPolicy()});
  d.append(row(el('span','加匹配:'),ks,holder,add));
  return d;
}
loadSubs(); loadSub(); loadPolicy();
</script>
</body></html>
"""


def make_app() -> FastAPI:
    """uvicorn 入口（factory）：避免 import 时就建 DB。`uvicorn --factory service.app:make_app`。"""
    return create_app(os.environ.get("CONDUIT_DB", "conduit.db"))
