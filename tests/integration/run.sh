#!/usr/bin/env bash
# conduit 集成测试（TESTING.md 第 2 层）：起隔离网络，**结构化**验证路由 + 故障切换。
# 拓扑保证（见 compose.yaml）：echo-proxied 只经代理可达、echo-direct 只直连可达 ——
# 所以「访问成功」本身就证明走对了路，不是看返回字符串。
# ⚠️ 首版断言，尚未在本机实跑验证（mihomo 经 socks5 的远端解析行为待真机确认）；任意 Docker 上 `./run.sh` 跑通后固化。
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose "$@"; }
texec() { compose exec -T tester "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

compose up -d
trap 'compose down -v' EXIT

echo "== 等 mihomo 就绪 =="
for i in $(seq 1 30); do
  texec curl -sf http://mihomo:9090/version >/dev/null 2>&1 && break
  [ "$i" -eq 30 ] && fail "mihomo external-controller 30s 内未就绪"
  sleep 1
done

echo "== 路由：echo-proxied 只能经代理到达 → 成功即证明走了代理 =="
out=$(texec curl -s --max-time 8 -x http://mihomo:7890 http://echo-proxied:5678) \
  || fail "经代理访问 echo-proxied 失败（代理路径不通，或被错误直连而无路由）"
echo "$out" | grep -q proxied || fail "echo-proxied 返回异常：$out"

echo "== 路由：echo-direct 只能直连到达 → 成功即证明走了直连 =="
out=$(texec curl -s --max-time 8 -x http://mihomo:7890 http://echo-direct:5678) \
  || fail "经 mihomo 访问 echo-direct 失败（直连规则没生效被错误代理，或直连路径不通）"
echo "$out" | grep -q direct || fail "echo-direct 返回异常：$out"

echo "== 故障切换：kill 当前上游后新连接应仍通 =="
texec curl -s http://mihomo:9090/proxies/PROXY >/dev/null 2>&1 || true   # 诊断用
compose stop upstream-a >/dev/null
sleep 12   # > health-check interval(10s)，让 mihomo 把 up-a 标记不健康
out=$(texec curl -s --max-time 8 -x http://mihomo:7890 http://echo-proxied:5678) \
  || fail "kill 一个上游后经代理访问失败（故障切换未生效）"
echo "$out" | grep -q proxied || fail "切换后 echo-proxied 返回异常：$out"

echo "PASS: 路由（代理/直连各自结构化证明）+ 故障切换基础断言通过"
# TODO: 用 /proxies/PROXY 断言 selected 确实从 up-a 切到 up-b 并量化耗时；长连接确认 chain=DIRECT。
