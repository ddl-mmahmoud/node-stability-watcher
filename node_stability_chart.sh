#!/bin/bash

build_id_or_url="$1"
build_id="$(grep -oP '\d+$' <<< "$build_id_or_url")"

results="/mnt/artifacts"

pipeline_json="$results/pipeline.json"
chart_image="$results/node-stability-$stagename.png"

stagename="$(uv run circleci_explore.py --slug gh/cerebrotech/domino --build "$build_id" --workflow-name e2e-smoke --job-name e2e-smoke-create-deployment  --step-logs DOMINO_USERHOST | grep DOMINO_USERHOST | grep -m1 -oP '(?<=https://).*?(?=[.])')"

chart_image="$results/node-stability-$stagename.png"

uv run circleci_explore.py --slug gh/cerebrotech/domino --build "$build_id" --pipeline-summary > "$pipeline_json"

start_time="$(jq -r '.pipeline.workflows[].jobs[] | select(.name | contains("create-deployment")) | .end' "$pipeline_json" | sed -e 's/T/ /g' -e 's/Z$/+0000/g')"
end_time="$(jq -r '.pipeline.end' "$pipeline_json" | sed -e 's/T/ /g' -e 's/Z$/+0000/g')"

uv run newrelic_chart_dashboard.py --image-out "$chart_image" --visualization viz.stacked-bar --title "Node stability of $stagename" <<EOF
SELECT uniqueCount(\`label.kubernetes.io/hostname\`) FROM K8sNodeSample WHERE \`label.dominodatalab.com/node-pool\` in ('platform') and clusterName = '$stagename' FACET \`label.dominodatalab.com/node-pool\`, \`label.node.kubernetes.io/instance-type\`, \`label.topology.kubernetes.io/zone\` TIMESERIES 1 minutes SINCE '$start_time' UNTIL '$end_time'
EOF

