#!/usr/bin/env bash
# Copy the backend package (the LangGraph payroll graph) into the agent folder, then deploy.
# Usage: ./sync-and-deploy.sh [region]   (needs AWS credentials, Node 20+, uv, npm i -g @aws/agentcore)
set -euo pipefail
cd "$(dirname "$0")"
REGION="${1:-ap-southeast-2}"
rm -rf app/agent/app
cp -r ../../../backend/app app/agent/app
find app/agent/app -name __pycache__ -type d -prune -exec rm -rf {} +
[ -d agentcore/cdk/node_modules ] || (cd agentcore/cdk && npm install --no-audit --no-fund)
agentcore deploy --region "$REGION"
agentcore invoke --runtime agent --payload '{"action":"demo"}' --region "$REGION"
