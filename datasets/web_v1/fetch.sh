#!/usr/bin/env bash
# Downloads the 25 public web images (third-party; internal testing only, never commit or show to
# customers), then writes ground-truth labels. Usage: bash datasets/web_v1/fetch.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p eval_data/web/raw
while IFS=$'\t' read -r name url; do
  [ -f "eval_data/web/raw/$name" ] || curl -sSL -A "Mozilla/5.0" --max-time 60 -o "eval_data/web/raw/$name" "$url" || echo "FAILED: $name"
done < datasets/web_v1/sources.tsv
python datasets/web_v1/labels.py
echo "Offline stages (no API key):  python -m sereno.eval.offline_checks eval_data/web/labelled"
echo "Full model run (needs key):   python -m sereno.eval.run_eval eval_data/web/labelled --out reports/web_v1"
