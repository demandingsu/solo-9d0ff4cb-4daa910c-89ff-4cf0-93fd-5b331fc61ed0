#!/usr/bin/env bash
# 迁仓 API 演示：跨夜归仓、逐日回填与切换守卫、改期换日、切换后更正。
# 用法：先启动服务（docker compose up 或 uvicorn app.main:app），再执行 scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "1. 灌入旧仓点位（按采样日入仓）：R10-001 跨夜，点位被拆在 11-20 / 11-21 两天"
curl -sS -X POST "$BASE/legacy/points" -H 'Content-Type: application/json' -d '{
  "points": [
    {"trip_code": "R10-001", "sampled_at": "2026-11-20T23:40:00Z", "lat": 31.10, "lon": 121.40},
    {"trip_code": "R10-001", "sampled_at": "2026-11-20T23:55:00Z", "lat": 31.12, "lon": 121.43},
    {"trip_code": "R10-001", "sampled_at": "2026-11-21T00:10:00Z", "lat": 31.15, "lon": 121.46},
    {"trip_code": "R10-001", "sampled_at": "2026-11-21T00:30:00Z", "lat": 31.18, "lon": 121.49},
    {"trip_code": "R10-002", "sampled_at": "2026-11-21T08:00:00Z", "lat": 31.20, "lon": 121.50}
  ]}' | jq .

step "2. 未切换时查询 11-20..11-21：旧仓按采样日供数，R10-001 被拆成两天"
curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-21" | jq .

step "3. 回填 11-20：导入班次 R10-001（23:40 发车）与整趟带编号轨迹"
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [{
    "trip_code": "R10-001",
    "departure_at": "2026-11-20T23:40:00Z",
    "points": [
      {"seq": 1, "sampled_at": "2026-11-20T23:40:00Z", "lat": 31.10, "lon": 121.40},
      {"seq": 2, "sampled_at": "2026-11-20T23:55:00Z", "lat": 31.12, "lon": 121.43},
      {"seq": 3, "sampled_at": "2026-11-21T00:10:00Z", "lat": 31.15, "lon": 121.46},
      {"seq": 4, "sampled_at": "2026-11-21T00:30:00Z", "lat": 31.18, "lon": 121.49}
    ]}]}' | jq .

step "4. 数据员拿到 11-20 的回填状态指纹"
TOKEN=$(curl -sS "$BASE/dates/2026-11-20/state" | jq -r .state_token)
echo "state_token = $TOKEN"

step "5. 核对期间又补传了一个点（回填后轨迹有变）"
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [{"trip_code": "R10-001", "points": [
      {"seq": 1, "sampled_at": "2026-11-20T23:40:00Z", "lat": 31.10, "lon": 121.40},
      {"seq": 2, "sampled_at": "2026-11-20T23:55:00Z", "lat": 31.12, "lon": 121.43},
      {"seq": 3, "sampled_at": "2026-11-21T00:10:00Z", "lat": 31.15, "lon": 121.46},
      {"seq": 4, "sampled_at": "2026-11-21T00:30:00Z", "lat": 31.18, "lon": 121.49},
      {"seq": 5, "sampled_at": "2026-11-21T00:45:00Z", "lat": 31.19, "lon": 121.50}
    ]}]}' | jq .

step "6. 用旧指纹切换 → 409 退回并提示重做"
curl -sS -o /tmp/switch_reject.json -w "HTTP %{http_code}\n" \
  -X POST "$BASE/dates/2026-11-20/switch" -H 'Content-Type: application/json' \
  -d "{\"expected_state\": \"$TOKEN\"}"
jq . /tmp/switch_reject.json

step "7. 重新核对，用最新指纹切换 → 成功"
TOKEN=$(curl -sS "$BASE/dates/2026-11-20/state" | jq -r .state_token)
curl -sS -X POST "$BASE/dates/2026-11-20/switch" -H 'Content-Type: application/json' \
  -d "{\"expected_state\": \"$TOKEN\"}" | jq .

step "8. 切换后查询：跨夜整趟轨迹归到发车日 11-20，每趟车只出现一次"
curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-21" | jq .

step "9. 班次改期：R10-001 发车时刻更正为 11-21 00:10（11-21 仍未切换，供数来源不同）"
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [{"trip_code": "R10-001", "departure_at": "2026-11-21T00:10:00Z"}]}' | jq .

step "10. 改期后查询：整趟轨迹随之换到 11-21（仍从新仓供数），11-20 不再出现"
curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-21" | jq .

step "11. 切换后仍可更正：把发车时刻改回 11-20 23:40"
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [{"trip_code": "R10-001", "departure_at": "2026-11-20T23:40:00Z"}]}' | jq .
curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-21" | jq .
