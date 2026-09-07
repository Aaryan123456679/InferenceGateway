#!/usr/bin/env bash
# Runs one locust load window and reports cache hit-rate over that window.
# Run it twice - once with cache on (LABEL=cache), once with NO_CACHE=1
# (LABEL=baseline) - and compare the aggregated p95 this script prints for
# each: the delta is the cache's causal effect on latency.
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
USERS="${USERS:-50}"
SPAWN="${SPAWN:-10}"
DURATION="${DURATION:-2m}"
LABEL="${LABEL:-cache}"
OUT="loadtest/report_${LABEL}"

# Sum every sample of a Prometheus counter (across all label sets).
# index($0,p)==1 matches lines that START with the metric name, so
# gateway_cache_hits_total is summed but a same-prefixed metric isn't.
metric_sum() {
  curl -s "${GATEWAY_URL}/metrics" \
    | awk -v p="$1" 'index($0,p)==1 {s+=$NF} END{printf "%d\n", s+0}'
}

echo ">> snapshot /metrics before run (${LABEL})"
HIT_BEFORE=$(metric_sum gateway_cache_hits_total)

echo ">> locust: ${USERS} users, spawn ${SPAWN}/s, ${DURATION}, label=${LABEL}"
# locust exits non-zero whenever ANY request failed during the run (even
# one, out of hundreds) - under `set -e` that would abort this script
# before the summary below ever prints, hiding the very report a failure
# makes most worth seeing. The failure count/detail is already in locust's
# own printed table above; `|| true` just lets this script keep going.
locust -f loadtest/locustfile.py --host "${GATEWAY_URL}" \
  --headless -u "${USERS}" -r "${SPAWN}" -t "${DURATION}" \
  --csv "${OUT}" --only-summary || true

HIT=$(( $(metric_sum gateway_cache_hits_total) - HIT_BEFORE ))

# The gateway has no gateway_requests_total counter (only per-outcome ones:
# cache hits, backend calls, failovers) - locust's own aggregated request
# count is the authoritative "how many requests did this window send",
# read straight from the CSV it just wrote rather than re-derived from
# metrics that don't line up 1:1 with "one logical request".
REQ=$(python3 -c "
import csv
with open('${OUT}_stats.csv') as f:
    for row in csv.DictReader(f):
        if row['Name'] == 'Aggregated':
            print(row['Request Count'])
")
P95=$(python3 -c "
import csv
with open('${OUT}_stats.csv') as f:
    for row in csv.DictReader(f):
        if row['Name'] == 'Aggregated':
            print(row['95%'])
")

RATE=$(awk -v h="$HIT" -v r="$REQ" 'BEGIN{ if (r>0) printf "%.1f", 100*h/r; else printf "n/a" }')

echo "----------------------------------------"
echo "label:      ${LABEL}"
echo "requests:   ${REQ}"
echo "cache hits: ${HIT}"
echo "hit-rate:   ${RATE}%"
echo "p95 (ms):   ${P95}"
echo "            full stats in ${OUT}_stats.csv"
echo "----------------------------------------"
