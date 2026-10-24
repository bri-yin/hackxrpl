#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
repo=$(python3 -c 'import json; print(json.load(open("coordination.json"))["repository"])')
gh label create 'agent:ready' --repo "$repo" --color 0E8A16 --description 'Defined task eligible for agent dispatch' --force
gh label create 'agent:blocked' --repo "$repo" --color D93F0B --description 'Requires a decision, dependency, or external prerequisite' --force
