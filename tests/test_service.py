"""conduit-service 测试：主从式订阅 CRUD（id 不透明 + name 可改）+ 摄入（URL/文件）+ 节点明细。

经 FastAPI TestClient + 内存 SQLite；URL 拉取注入假 fetcher，不碰真网络。
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # repo root：conduit + service 包

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from service.app import create_app  # noqa: E402

FIXTURE = (HERE / "fixtures" / "sub.clash.yaml").read_text()


def _client() -> TestClient:
    return TestClient(create_app(":memory:"))


def _mksub(c: TestClient, name: str = "vendor-a", url: str | None = None) -> str:
    r = c.post("/api/subscriptions", json={"name": name, "url": url})
    assert r.status_code == 200
    return r.json()["id"]


def test_create_returns_opaque_id_and_lists_name():
    c = _client()
    sid = _mksub(c, "My VPN")
    sub = c.get("/api/subscriptions").json()[0]
    assert sub["id"] == sid and sub["name"] == "My VPN"
    assert sid != "My VPN" and "url" not in sub  # id 不透明、url 不泄露


def test_import_into_subscription_and_detail_nodes():
    c = _client()
    sid = _mksub(c)
    assert c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE}).json()["imported"] == 2
    assert len(c.get(f"/api/subscriptions/{sid}/nodes").json()) == 2
    assert "params" not in c.get(f"/api/subscriptions/{sid}/nodes").json()[0]  # 不泄露凭据


def test_refresh_fetches_url_and_imports():
    c = TestClient(create_app(":memory:", fetcher=lambda url: FIXTURE))
    sid = _mksub(c, "v", "https://example/sub")
    assert c.post(f"/api/subscriptions/{sid}/refresh").json()["imported"] == 2
    sub = c.get("/api/subscriptions").json()[0]
    assert sub["has_url"] == 1 and "url" not in sub


def test_patch_rename():
    c = _client()
    sid = _mksub(c, "old")
    assert c.patch(f"/api/subscriptions/{sid}", json={"name": "new"}).status_code == 200
    assert c.get("/api/subscriptions").json()[0]["name"] == "new"


def test_patch_url_then_refresh():
    c = TestClient(create_app(":memory:", fetcher=lambda url: FIXTURE))
    sid = _mksub(c, "v")  # 先没 url
    c.patch(f"/api/subscriptions/{sid}", json={"url": "https://e/sub"})
    assert c.post(f"/api/subscriptions/{sid}/refresh").json()["imported"] == 2


def test_delete_subscription_removes_nodes():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    assert c.delete(f"/api/subscriptions/{sid}").status_code == 200
    assert c.get("/api/subscriptions").json() == []
    assert c.get("/api/nodes").json() == []


def test_url_scheme_validated():
    c = _client()
    assert c.post("/api/subscriptions", json={"name": "v", "url": "file:///etc/passwd"}).status_code == 400


def test_refresh_without_url_400():
    c = _client()
    sid = _mksub(c)
    assert c.post(f"/api/subscriptions/{sid}/refresh").status_code == 400


def test_refresh_fetch_failure_502():
    def boom(url):
        raise RuntimeError("network down")

    c = TestClient(create_app(":memory:", fetcher=boom))
    sid = _mksub(c, "v", "https://x/sub")
    assert c.post(f"/api/subscriptions/{sid}/refresh").status_code == 502


def test_import_unknown_sub_404():
    assert _client().post("/api/subscriptions/nope/import", json={"raw": "proxies: []"}).status_code == 404


def test_malformed_yaml_import_returns_400():
    c = _client()
    sid = _mksub(c)
    assert c.post(f"/api/subscriptions/{sid}/import", json={"raw": "proxies: 'unterminated"}).status_code == 400


def test_bad_proxy_import_sanitized_400():
    c = _client()
    sid = _mksub(c)
    bad = "proxies:\n  - {name: x, type: ss, server: s.com, port: NOTAPORT, password: p}\n"
    r = c.post(f"/api/subscriptions/{sid}/import", json={"raw": bad})
    assert r.status_code == 400 and "NOTAPORT" not in r.json()["detail"]


def test_migration_adds_name_and_url_to_old_db(tmp_path):
    import sqlite3

    from service.db import Store

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)  # 旧 schema：subscriptions 无 name/url
    conn.execute("CREATE TABLE subscriptions (id TEXT PRIMARY KEY, type TEXT, note TEXT, created_at TEXT)")
    conn.execute("INSERT INTO subscriptions(id, type) VALUES ('westdata', 'clash')")
    conn.commit()
    conn.close()
    sub = Store(str(p)).get_subscription("westdata")  # 迁移补 name(=id) + url
    assert sub["name"] == "westdata" and "url" in sub


def test_sub_clash_requires_token():
    c = _client()
    assert c.get("/sub/clash").status_code == 403
    assert c.get("/sub/clash", params={"token": "wrong"}).status_code == 403


def test_sub_clash_pure_has_proxies_groups_and_creds():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    token = c.get("/api/sub-token").json()["token"]
    r = c.get("/sub/clash", params={"token": token})
    assert r.status_code == 200
    cfg = yaml.safe_load(r.text)
    assert len(cfg["proxies"]) == 2
    assert cfg["proxy-groups"][0]["name"] == "PROXY"
    assert cfg["rules"][-1] == "MATCH,PROXY"
    assert "tun" not in cfg and "dns" not in cfg  # 纯净版不带实例设置
    assert "pass1" in r.text  # 订阅含明文节点凭据（ss password）→ token 保护是对的


def test_sub_clash_has_clash_scaffolding():
    """clash-verge 导入校验需要标准顶层骨架；只给 proxies/groups/rules 会被静默拒绝。"""
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    assert cfg["mode"] == "rule" and cfg.get("mixed-port") and "log-level" in cfg


def test_sub_clash_has_subscription_headers():
    """clash-verge/mihomo 靠这些头把响应当订阅文件处理（否则导入失败/浏览器直接显示）。"""
    c = _client()
    token = c.get("/api/sub-token").json()["token"]
    r = c.get("/sub/clash", params={"token": token})
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.headers.get("access-control-allow-origin") == "*"
    assert r.headers.get("profile-update-interval")


def test_sub_clash_has_private_tailnet_direct_baseline():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    token = c.get("/api/sub-token").json()["token"]
    # 纯净 + full 都应内置 tailnet/私网直连兜底（rule#0 基线），不被"全代理"抓走
    for full in (0, 1):
        cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token, "full": full}).text)
        assert "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve" in cfg["rules"]      # tailscale CGNAT
        assert "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve" in cfg["rules"]     # 私网
    full_cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token, "full": 1}).text)
    assert "100.64.0.0/10" in full_cfg["tun"]["route-exclude-address"]        # full 的 TUN 也排除


def test_sub_clash_full_has_dns_and_tun():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token, "full": 1}).text)
    assert cfg["dns"]["enhanced-mode"] == "fake-ip" and cfg["tun"]["enable"] is True


def test_sub_clash_empty_is_valid_all_direct():
    c = _client()
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    assert cfg["rules"] == ["MATCH,DIRECT"]  # 无节点 → 合法的全直连配置


def test_policy_edit_and_reset():
    c = _client()
    assert c.get("/api/policy").json()["custom"] is False
    pol = {"routes": [{"name": "测试", "to": "DIRECT", "geosite": ["cn"]}], "final": "PROXY"}
    assert c.put("/api/policy", json=pol).status_code == 200
    g = c.get("/api/policy").json()
    assert g["custom"] is True and g["policy"]["routes"][0]["name"] == "测试"
    assert "GEOSITE,cn,DIRECT" in g["rules"]
    assert c.delete("/api/policy").status_code == 200
    assert c.get("/api/policy").json()["custom"] is False  # 恢复默认


def test_policy_put_rejects_bad():
    c = _client()
    assert c.put("/api/policy", json={"routes": [{"to": "X", "rule_set": ["nope"]}]}).status_code == 400
    assert c.put("/api/policy", json={"routes": [{"to": "X", "geosite": ["a/b"]}]}).status_code == 400


def test_policy_edit_reflects_in_sub():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    c.put("/api/policy", json={"routes": [{"name": "国内直连", "to": "DIRECT", "geosite": ["cn"]}], "final": "PROXY"})
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    assert "GEOSITE,cn,DIRECT" in cfg["rules"]
    assert not any(r.startswith("RULE-SET,ai") for r in cfg["rules"])  # 自定义策略不含默认 AI 路由


def test_groups_endpoint():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    t = c.get("/api/groups").json()["targets"]
    assert {"DIRECT", "REJECT", "PROXY", "AUTO"} <= set(t)


def test_ruleset_inspect():
    fake = "# comment\ndomain:openai.com\n+.anthropic.com\nclaude.ai\n"
    c = TestClient(create_app(":memory:", fetcher=lambda url: fake))
    r = c.get("/api/ruleset", params={"kind": "ruleset", "name": "ai"}).json()
    assert r["count"] == 3 and "domain:openai.com" in r["sample"]  # 跳过注释
    assert c.get("/api/ruleset", params={"kind": "ruleset", "name": "../x"}).status_code == 400  # 路径穿越
    assert c.get("/api/ruleset", params={"kind": "ruleset", "name": "nope"}).status_code == 404
    assert c.get("/api/ruleset", params={"kind": "geosite", "name": "cn"}).json()["count"] == 3


def test_policy_endpoint_exposes_rules():
    c = _client()
    r = c.get("/api/policy").json()
    assert "GEOSITE,cn,DIRECT" in r["rules"] and r["rules"][-1] == "MATCH,PROXY"
    assert r["policy"]["final"] == "PROXY"


def test_sub_clash_has_rule_providers_and_category_routes():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    assert "rule-providers" in cfg and "netflix" in cfg["rule-providers"]
    assert any(r.startswith("RULE-SET,ai,") for r in cfg["rules"])  # AI 类别有路由


def test_sub_clash_has_china_direct_rules():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    assert "GEOSITE,cn,DIRECT" in cfg["rules"] and "GEOIP,CN,DIRECT,no-resolve" in cfg["rules"]
    assert cfg["rules"][-1] == "MATCH,PROXY"


def test_node_list_has_region_fields():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    n = c.get(f"/api/subscriptions/{sid}/nodes").json()[0]
    assert {"region", "region_auto", "region_override", "quarantined"} <= set(n)


def test_region_override_reflects_in_nodes_and_grouping():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    aid = c.get(f"/api/subscriptions/{sid}/nodes").json()[0]["access_id"]
    assert c.put(f"/api/nodes/{aid}/tag", json={"region": "JP"}).status_code == 200
    by = {n["access_id"]: n for n in c.get(f"/api/subscriptions/{sid}/nodes").json()}
    assert by[aid]["region_override"] == "JP" and by[aid]["region"] == "JP"
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    gnames = {g["name"] for g in cfg["proxy-groups"]}
    assert {"PROXY", "AUTO", "JP"} <= gnames


def test_quarantine_excludes_from_subscription():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    aid = c.get(f"/api/subscriptions/{sid}/nodes").json()[0]["access_id"]
    c.put(f"/api/nodes/{aid}/tag", json={"quarantined": True})
    token = c.get("/api/sub-token").json()["token"]
    cfg = yaml.safe_load(c.get("/sub/clash", params={"token": token}).text)
    assert len(cfg["proxies"]) == 1  # fixture 2 个，隔离 1 个


def test_tag_survives_reimport():
    """标签按 access_id 存 → 重新导入同一订阅后仍在（两层身份的意义）。"""
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    aid = c.get(f"/api/subscriptions/{sid}/nodes").json()[0]["access_id"]
    c.put(f"/api/nodes/{aid}/tag", json={"region": "US"})
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})  # 重新导入
    by = {n["access_id"]: n for n in c.get(f"/api/subscriptions/{sid}/nodes").json()}
    assert by[aid]["region_override"] == "US"


def test_tag_partial_update_preserves_other_field():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    aid = c.get(f"/api/subscriptions/{sid}/nodes").json()[0]["access_id"]
    c.put(f"/api/nodes/{aid}/tag", json={"quarantined": True})  # 只设隔离
    c.put(f"/api/nodes/{aid}/tag", json={"region": "JP"})  # 只设 region —— 不能清掉隔离
    n = {x["access_id"]: x for x in c.get(f"/api/subscriptions/{sid}/nodes").json()}[aid]
    assert n["region_override"] == "JP" and n["quarantined"] is True


def test_tag_rejects_reserved_or_bad_region():
    c = _client()
    sid = _mksub(c)
    c.post(f"/api/subscriptions/{sid}/import", json={"raw": FIXTURE})
    aid = c.get(f"/api/subscriptions/{sid}/nodes").json()[0]["access_id"]
    assert c.put(f"/api/nodes/{aid}/tag", json={"region": "AUTO"}).status_code == 400
    assert c.put(f"/api/nodes/{aid}/tag", json={"region": "hk,x"}).status_code == 400
    assert c.put(f"/api/nodes/{aid}/tag", json={"region": "hk"}).status_code == 200  # 正常码 → HK


def test_index_page():
    assert _client().get("/").status_code == 200
