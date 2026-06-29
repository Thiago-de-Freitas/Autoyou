#!/usr/bin/env bash
# Wrapper para cron no Oracle Always Free (3x/dia).
# Exemplo crontab (8h, 14h, 20h horario de Brasilia = 11,17,23 UTC):
#   0 11,17,23 * * * /home/ubuntu/Autoyou/scripts/rodar_pipeline_oracle.sh >> /home/ubuntu/Autoyou/logs/pipeline.log 2>&1

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

mkdir -p logs

export FORCE_CPU_ENCODE=1
export ORACLE_FREE_RUNS_PER_DAY="${ORACLE_FREE_RUNS_PER_DAY:-3}"
export ORACLE_FREE_LIVE_MAX_MIN="${ORACLE_FREE_LIVE_MAX_MIN:-60}"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" oracle_free_guard.py run -- "$PYTHON_BIN" automacao_cortes.py
