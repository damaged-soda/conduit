#!/usr/bin/env bash
# conduit 集成测试：用 render 真实产出跑 mihomo，断言路由语义（本地 + GitHub PR CI 都跑）。
#   私网 IP → 直连(rule#0 兜底)  /  域名 → 代理  /  kill upstream → 切换
# 需要：docker + docker compose；python3 + 能 import conduit（CI 里先 `pip install -e .`）。
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose "$@"; }
texec() { compose exec -T tester "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== 用 render 产出生成配置 =="
"${PYTHON:-python3}" gen_config.py || fail "gen_config 失败"

compose up -d
trap 'compose down -v' EXIT

echo "== 等 mihomo 就绪 =="
for i in $(seq 1 30); do
  texec curl -sf http://mihomo:9090/version >/dev/null 2>&1 && break
  [ "$i" -eq 30 ] && fail "mihomo 30s 内未就绪"
  sleep 1
done

echo "== 域名目标 → 应走 PROXY（echo-proxied 只在 backnet，mihomo 必须经 upstream）=="
out=$(texec curl -s --max-time 8 -x http://mihomo:7890 http://echo-proxied:5678) \
  || fail "经代理访问 echo-proxied 失败（代理路径不通）"
echo "$out" | grep -q proxied || fail "echo-proxied 返回异常：$out"

echo "== 私网 IP 目标 → 应走 DIRECT（172.28.0.5 只在 directnet，upstream 够不到）=="
out=$(texec curl -s --max-time 8 -x http://mihomo:7890 http://172.28.0.5:5678) \
  || fail "私网目标未走直连 —— render 的私网兜底直连缺失（rule#0 回归）"
echo "$out" | grep -q direct || fail "私网目标返回异常：$out"

echo "== 故障切换：kill 当前选中的 upstream，断言选中真的切换 + 仍通 =="
# PROXY 是 select(默认→AUTO)。AUTO 是 fallback 外壳，优先走隐藏的 AUTO-FAST(url-test)，再回退原始节点。
# 停掉当前实际 upstream 后，echo-proxied 仍通（它只在 backnet，mihomo 必须经另一个 upstream 才能到）。
group_now() { texec curl -s "http://mihomo:9090/proxies/$1" | "${PYTHON:-python3}" -c "import sys,json;print(json.load(sys.stdin)['now'])"; }
selected_upstream() {
  local n
  n="$(group_now AUTO)"
  case "$n" in
    upstream-*) echo "$n" ;;
    *) group_now "$n" ;;
  esac
}
now=$(selected_upstream); echo "切换前实际 upstream：$now"
compose stop "$now" >/dev/null
sleep 12   # > health-check interval(10s)，让 mihomo 标记其不健康
out=""
for i in $(seq 1 8); do
  out=$(texec curl -s --max-time 8 -x http://mihomo:7890 http://echo-proxied:5678) && break
  sleep 1
done
[ -n "$out" ] || fail "切换后经代理访问失败"
echo "$out" | grep -q proxied || fail "切换后返回异常：$out"
echo "切换后 AUTO.now：$(group_now AUTO)"

echo "PASS: 域名→代理、私网→直连(rule#0 兜底)、故障切换 全过"
