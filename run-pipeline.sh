#!/bin/bash
# Run the full MLAT data pipeline: data-pipe → decoder → correlator → output
#
# The Neuron SDK's init() checks --port and --buyer-or-seller BEFORE main()
# runs, so these MUST be CLI args (env file values are loaded too late).

set -euo pipefail

cd "$(dirname "$0")/data-pipe"

# Read port from buyer-env
PORT=$(grep '^port=' .buyer-env | cut -d= -f2)
if [ -z "$PORT" ]; then
    echo "ERROR: port not found in .buyer-env" >&2
    exit 1
fi

echo "Starting pipeline with port=$PORT" >&2

./mlat-pipe \
    --port "$PORT" \
    --buyer-or-seller buyer \
    --mode peer \
    --list-of-sellers-source env \
    --envFile .buyer-env \
    2>pipe.log \
| python3 ../modes-decoder/main.py 2>../decoder.log \
| python3 ../correlation-engine/main.py 2>../correlator.log \
> correlation_groups.jsonl
