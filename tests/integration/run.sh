#!/usr/bin/env bash
# conduit 集成测试：用 render 真实产出跑 mihomo，断言路由语义（本地 + GitHub PR CI 都跑）。
#   私网 IP → 直连(rule#0 兜底)  /  域名 → 代理  /  kill upstream → 保持选中，手动切换后恢复
# 需要：docker + docker compose；python3 + 能 import conduit（CI 里先 `pip install -e .`）。
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose "$@"; }
texec() { compose exec -T tester "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== 用 render 产出生成配置 =="
"${PYTHON:-python3}" gen_config.py || fail "gen_config 失败"

trap 'compose down -v' EXIT
compose up -d

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

echo "== 节点故障不得自动切换；只有手动选择另一个节点才能恢复 =="
group_now() { texec curl -fsS "http://mihomo:9090/proxies/$1" | "${PYTHON:-python3}" -c "import sys,json;print(json.load(sys.stdin)['now'])"; }
region=$(group_now PROXY)
region_path=$("${PYTHON:-python3}" -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$region")
now=$(group_now "$region_path")
case "$now" in
  upstream-a) other=upstream-b ;;
  upstream-b) other=upstream-a ;;
  *) fail "意外节点：$now" ;;
esac
compose stop "$now" >/dev/null
# 连续制造连接失败，排除一次缓存或短暂探测窗口造成的假阳性。
for i in $(seq 1 3); do
  if texec curl -fsS --max-time 5 -x http://mihomo:7890 http://echo-proxied:5678 >/dev/null 2>&1; then
    fail "故障节点被自动替换，禁止自动切换的约束失效"
  fi
  [ "$(group_now "$region_path")" = "$now" ] || fail "选中节点在无人操作时改变"
done
# 主动测速也不能改变 select 选择。
texec curl -fsS --max-time 10 "http://mihomo:9090/group/$region_path/delay?url=http%3A%2F%2Fecho-health%3A5678&timeout=1000" >/dev/null
[ "$(group_now "$region_path")" = "$now" ] || fail "测速改变了选中节点"
# 回环/私网仍直连。
out=$(texec curl -fsS --max-time 8 -x http://mihomo:7890 http://172.28.0.5:5678)
[ "$out" = direct ] || fail "节点故障影响了私网直连"
texec curl -fsS -X PUT -H 'Content-Type: application/json' -d "{\"name\":\"$other\"}" "http://mihomo:9090/proxies/$region_path" >/dev/null
[ "$(group_now "$region_path")" = "$other" ] || fail "手动切换未生效"
out=$(texec curl -fsS --max-time 8 -x http://mihomo:7890 http://echo-proxied:5678)
[ "$out" = proxied ] || fail "手动切换后仍不可用"
# 热加载和重启均保持手动选择，不能被默认成员顺序覆盖。
texec curl -fsS -X PUT -H 'Content-Type: application/json' -d '{"path":"/cfg/config.yaml"}' 'http://mihomo:9090/configs?force=true' >/dev/null
[ "$(group_now "$region_path")" = "$other" ] || fail "热加载丢失手动选择"
compose restart mihomo >/dev/null
for i in $(seq 1 30); do
  texec curl -fsS http://mihomo:9090/version >/dev/null 2>&1 && break
  [ "$i" -eq 30 ] && fail "重启后 mihomo 未就绪"
  sleep 1
done
[ "$(group_now "$region_path")" = "$other" ] || fail "重启丢失手动选择"
echo "PASS: 代理/直连分流、故障不切换、手动切换恢复、选择持久化"
