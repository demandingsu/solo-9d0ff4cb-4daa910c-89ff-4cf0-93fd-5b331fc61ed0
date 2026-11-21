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

step "12. 回填并核对下一班的轨迹（R10-202 与上一班 R10-201，11-20 已切换供数）"
# R10-201 误收了 R10-202 的 23:30 / 23:45 两个点（seq 3、4）
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [
    {"trip_code": "R10-201", "departure_at": "2026-11-20T21:00:00Z", "points": [
      {"seq": 1, "sampled_at": "2026-11-20T21:00:00Z", "lat": 30.90, "lon": 121.30},
      {"seq": 2, "sampled_at": "2026-11-20T21:20:00Z", "lat": 30.92, "lon": 121.32},
      {"seq": 3, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
      {"seq": 4, "sampled_at": "2026-11-20T23:45:00Z", "lat": 31.12, "lon": 121.12}
    ]},
    {"trip_code": "R10-202", "departure_at": "2026-11-20T23:20:00Z", "points": [
      {"seq": 10, "sampled_at": "2026-11-20T23:20:00Z", "lat": 31.20, "lon": 121.20},
      {"seq": 11, "sampled_at": "2026-11-20T23:35:00Z", "lat": 31.22, "lon": 121.22}
    ]}
  ]}' | jq .
SRC_V=$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq '[.days[].trips[] | select(.trip_code=="R10-201")][0].batch_version')
DST_V=$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq '[.days[].trips[] | select(.trip_code=="R10-202")][0].batch_version')
echo "核对时批次版本：R10-201=$SRC_V，R10-202=$DST_V"

step "13. 核对后两班各自补传了一个点（已有新批次）"
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [
    {"trip_code": "R10-201", "points": [
      {"seq": 1, "sampled_at": "2026-11-20T21:00:00Z", "lat": 30.90, "lon": 121.30},
      {"seq": 2, "sampled_at": "2026-11-20T21:20:00Z", "lat": 30.92, "lon": 121.32},
      {"seq": 3, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
      {"seq": 4, "sampled_at": "2026-11-20T23:45:00Z", "lat": 31.12, "lon": 121.12},
      {"seq": 5, "sampled_at": "2026-11-21T00:00:00Z", "lat": 31.14, "lon": 121.14}
    ]},
    {"trip_code": "R10-202", "points": [
      {"seq": 10, "sampled_at": "2026-11-20T23:20:00Z", "lat": 31.20, "lon": 121.20},
      {"seq": 11, "sampled_at": "2026-11-20T23:35:00Z", "lat": 31.22, "lon": 121.22},
      {"seq": 14, "sampled_at": "2026-11-21T00:05:00Z", "lat": 31.24, "lon": 121.24}
    ]}
  ]}' | jq -c '.imported'

step "14. 用核对时的版本发起改挂：3→12、4→13，作用在双方最新批次，补传点保留"
curl -sS -X POST "$BASE/trips/rebind" -H 'Content-Type: application/json' -d "{
  \"source_trip_code\": \"R10-201\", \"source_batch_version\": $SRC_V,
  \"target_trip_code\": \"R10-202\", \"target_batch_version\": $DST_V,
  \"mappings\": [{\"source_seq\": 3, \"target_seq\": 12},
                {\"source_seq\": 4, \"target_seq\": 13}]}" | jq .
curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" | jq \
  '[.days[].trips[] | select(.trip_code|startswith("R10-20"))
    | {trip_code, batch_version, seqs: [.points[].seq]}]'

step "15. 目标点号已有相同采样时刻和坐标：只从源班次移除（去重）"
# R10-402 在核对时就已经有 12 号点，与 R10-401 误收的 3 号点完全相同
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [
    {"trip_code": "R10-401", "departure_at": "2026-11-20T20:30:00Z", "points": [
      {"seq": 1, "sampled_at": "2026-11-20T20:30:00Z", "lat": 30.85, "lon": 121.25},
      {"seq": 3, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10}
    ]},
    {"trip_code": "R10-402", "departure_at": "2026-11-20T23:25:00Z", "points": [
      {"seq": 10, "sampled_at": "2026-11-20T23:25:00Z", "lat": 31.18, "lon": 121.18},
      {"seq": 12, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10}
    ]}
  ]}' | jq -c '.imported'
SRC_V=$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq '[.days[].trips[] | select(.trip_code=="R10-401")][0].batch_version')
DST_V=$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq '[.days[].trips[] | select(.trip_code=="R10-402")][0].batch_version')
# 核对后源班次补传了一个点（已有新批次），涉及的点未变 → 仍可改挂
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [{"trip_code": "R10-401", "points": [
    {"seq": 1, "sampled_at": "2026-11-20T20:30:00Z", "lat": 30.85, "lon": 121.25},
    {"seq": 3, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    {"seq": 5, "sampled_at": "2026-11-21T00:10:00Z", "lat": 31.20, "lon": 121.20}
  ]}]}' | jq -c '.imported'
curl -sS -X POST "$BASE/trips/rebind" -H 'Content-Type: application/json' -d "{
  \"source_trip_code\": \"R10-401\", \"source_batch_version\": $SRC_V,
  \"target_trip_code\": \"R10-402\", \"target_batch_version\": $DST_V,
  \"mappings\": [{\"source_seq\": 3, \"target_seq\": 12}]}" | jq .
curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" | jq \
  '[.days[].trips[] | select(.trip_code|startswith("R10-40"))
    | {trip_code, batch_version, seqs: [.points[].seq]}]'

step "16. 冲突退回：核对后源点被改、目标点号被不同的点占用、源点缺失 → 409 列出点号"
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [
    {"trip_code": "R10-301", "departure_at": "2026-11-20T20:00:00Z", "points": [
      {"seq": 1, "sampled_at": "2026-11-20T20:00:00Z", "lat": 30.80, "lon": 121.20},
      {"seq": 2, "sampled_at": "2026-11-20T20:20:00Z", "lat": 30.82, "lon": 121.22},
      {"seq": 3, "sampled_at": "2026-11-20T22:30:00Z", "lat": 31.00, "lon": 121.00},
      {"seq": 4, "sampled_at": "2026-11-20T22:45:00Z", "lat": 31.02, "lon": 121.02}
    ]},
    {"trip_code": "R10-302", "departure_at": "2026-11-20T22:20:00Z", "points": [
      {"seq": 10, "sampled_at": "2026-11-20T22:20:00Z", "lat": 30.95, "lon": 121.05}
    ]}
  ]}' | jq -c '.imported'
SRC_V=$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq '[.days[].trips[] | select(.trip_code=="R10-301")][0].batch_version')
DST_V=$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq '[.days[].trips[] | select(.trip_code=="R10-302")][0].batch_version')
# 核对后：R10-301 的 4 号点坐标被更正
curl -sS -X POST "$BASE/trips/import" -H 'Content-Type: application/json' -d '{
  "trips": [{"trip_code": "R10-301", "points": [
    {"seq": 1, "sampled_at": "2026-11-20T20:00:00Z", "lat": 30.80, "lon": 121.20},
    {"seq": 2, "sampled_at": "2026-11-20T20:20:00Z", "lat": 30.82, "lon": 121.22},
    {"seq": 3, "sampled_at": "2026-11-20T22:30:00Z", "lat": 31.00, "lon": 121.00},
    {"seq": 4, "sampled_at": "2026-11-20T22:45:00Z", "lat": 31.02001, "lon": 121.02}
  ]}]}' | jq -c '.imported'
curl -sS -o /tmp/rebind_conflict.json -w "HTTP %{http_code}\n" \
  -X POST "$BASE/trips/rebind" -H 'Content-Type: application/json' -d "{
  \"source_trip_code\": \"R10-301\", \"source_batch_version\": $SRC_V,
  \"target_trip_code\": \"R10-302\", \"target_batch_version\": $DST_V,
  \"mappings\": [{\"source_seq\": 3, \"target_seq\": 10},
                {\"source_seq\": 4, \"target_seq\": 13},
                {\"source_seq\": 99, \"target_seq\": 14}]}"
jq . /tmp/rebind_conflict.json
echo "整笔退回后批次版本仍为：$(curl -sS "$BASE/trips?date_from=2026-11-20&date_to=2026-11-20" \
  | jq -c '[.days[].trips[] | select(.trip_code|startswith("R10-30"))
             | {trip_code, batch_version, seqs: [.points[].seq]}]')"
