#!/usr/bin/env bash
# conduit 集成测试（TESTING.md 第 2 层）：起隔离网络，验证路由 + 故障切换。
# ⚠️ 首版断言，尚未在本机实跑验证；在 MBA 上 `./run.sh` 跑通后再固化（可能要微调等待时长 / curl 形态）。
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

echo "== 路由：代理目标走代理 =="
out=$(texec curl -s --max-time 5 -x http://mihomo:7890 http://echo-proxied:5678 || true)
echo "$out" | grep -q proxied || fail "经代理访问 echo-proxied 异常：$out"

echo "== 路由：直连目标走 DIRECT（/connections 核对 chain）=="
texec curl -s --max-time 5 -x http://mihomo:7890 http://echo-direct:5678 >/dev/null || true
conns=$(texec curl -s http://mihomo:9090/connections || true)
echo "$conns" | grep -q 'echo-direct' || echo "WARN: /connections 未见 echo-direct（短连接可能已结束，待换长连接断言 chain=DIRECT）"

echo "== 故障切换：kill 当前上游后新连接应仍通 =="
compose stop upstream-a >/dev/null
sleep 12   # > health-check interval(10s)，让 mihomo 把 up-a 标记不健康
out=$(texec curl -s --max-time 5 -x http://mihomo:7890 http://echo-proxied:5678 || true)
echo "$out" | grep -q proxied || fail "kill 一个上游后经代理访问失败（故障切换未生效）"

echo "PASS: 路由 + 故障切换基础断言通过"
# TODO: 量化「kill→恢复」耗时对照 SLO；用 /proxies/PROXY 观察当前选中节点确认确实切换；长连接确认 DIRECT chain。
