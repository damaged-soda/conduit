#!/usr/bin/env bash
# conduit 集成测试流程骨架（TESTING.md 第 2 层）：up → 断言 → down。
# 多数断言是 TODO：等 render() 能产出真实配置后替换 mihomo.proxy-only.yaml 再补全。
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose "$@"; }

compose up -d
trap 'compose down -v' EXIT

# TODO: 轮询 external-controller 直到 mihomo 就绪
#   until compose exec -T tester curl -sf http://mihomo:9090/version; do sleep 1; done

echo "== 路由断言 =="
# 经 mihomo 代理访问 echo-proxied，应走 PROXY chain：
#   compose exec -T tester curl -sx http://mihomo:7890 http://echo-proxied:5678
# 访问 echo-direct 应走 DIRECT —— 用 /connections API 核对 chain：
#   compose exec -T tester curl -s http://mihomo:9090/connections
# TODO: 断言

echo "== 故障切换断言 =="
# 记录 PROXY 当前选中的上游 → kill 它 → 隔 health-check interval 后再发请求：
#   compose stop upstream-a
# TODO: 断言新连接走 up-b；量从 kill 到恢复的耗时，对照「人无感」SLO

echo "TODO: 以上断言待 render() 产出真实配置后补全"
