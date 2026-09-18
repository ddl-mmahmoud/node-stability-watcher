#!/usr/bin/env python3
r"""
Create a New Relic dashboard from an NRQL query via NerdGraph, so a chart can
be generated from a script instead of pasted into the UI by hand.

The NRQL query is read from stdin (same convention as newrelic.py). The
script creates a new one-page, one-widget dashboard and prints the dashboard
URL. Pass --image-out to also save a PNG snapshot of the chart itself, via
NerdGraph's dashboardWidgetCreateSnapshotUrl mutation.

Example
-------
  echo "SELECT uniqueCount(\`label.kubernetes.io/hostname\`) FROM K8sNodeSample \\
    WHERE \`label.dominodatalab.com/node-pool\` in ('platform') \\
    and clusterName = 'e2e-smoke143445' \\
    FACET \`label.dominodatalab.com/node-pool\`, \`label.node.kubernetes.io/instance-type\`, \`label.topology.kubernetes.io/zone\` \\
    TIMESERIES 1 minutes SINCE '2026-09-17 14:42:00+0000' UNTIL '2026-09-17 16:44:00+0000'" | \\
    newrelic_chart_dashboard.py --title "e2e-smoke143445 node pool" --image-out chart.png
"""

import argparse
import json
import os
import re
import sys
import time

import requests

NERDGRAPH_URL = "https://api.newrelic.com/graphql"


class NerdGraphError(RuntimeError):
    pass


def nerdgraph_request(api_key, query, variables):
    resp = requests.post(
        NERDGRAPH_URL,
        headers={"Api-Key": api_key, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise NerdGraphError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


DASHBOARD_CREATE_MUTATION = """
mutation($accountId: Int!, $dashboard: DashboardInput!) {
  dashboardCreate(accountId: $accountId, dashboard: $dashboard) {
    entityResult {
      guid
      name
    }
    errors {
      description
      type
    }
  }
}
"""


def guess_visualization(nrql):
    """Pick a reasonable default chart type from the query shape.

    Note: "viz.bar" is New Relic's *Simplified Bar* chart, not the normal
    bar chart shown in the dashboard UI's chart type picker - that one is
    "viz.stacked-bar" (confirmed via NerdGraph: editing a viz.bar widget in
    the UI and picking "Bar chart" persists it as viz.stacked-bar).
    """
    if re.search(r"\bTIMESERIES\b", nrql, re.IGNORECASE):
        return "viz.line"
    if re.search(r"\bFACET\b", nrql, re.IGNORECASE):
        return "viz.stacked-bar"
    return "viz.billboard"


def build_dashboard_input(title, nrql, account_id, visualization, permissions):
    return {
        "name": title,
        "permissions": permissions,
        "pages": [
            {
                "name": title,
                "widgets": [
                    {
                        "title": title,
                        "layout": {"column": 1, "row": 1, "width": 12, "height": 3},
                        "visualization": {"id": visualization},
                        "rawConfiguration": {
                            "nrqlQueries": [
                                {"accountId": account_id, "query": nrql}
                            ]
                        },
                    }
                ],
            }
        ],
    }


DASHBOARD_WIDGET_SNAPSHOT_MUTATION = """
mutation($widget: DeclarativeUiWidget!) {
  dashboardWidgetCreateSnapshotUrl(widget: $widget) {
    url
  }
}
"""


def build_declarative_widget(title, nrql, account_id, visualization):
    # Standalone widget definition for dashboardWidgetCreateSnapshotUrl - the
    # chart doesn't need to live on a dashboard to be snapshotted this way.
    return {
        "version": 1,
        "type": "declarative/widget",
        "content": {
            "type": "widget",
            "props": {"title": title},
            "content": {
                "type": "visualization",
                "id": visualization,
                "props": {
                    "nrqlQueries": [{"accountIds": [account_id], "query": nrql}]
                },
            },
        },
    }


def fetch_widget_snapshot_url(api_key, widget):
    data = nerdgraph_request(api_key, DASHBOARD_WIDGET_SNAPSHOT_MUTATION, {"widget": widget})
    return data["dashboardWidgetCreateSnapshotUrl"]["url"]


def download_snapshot_image(url, out_path, retries=10, delay=2):
    # Snapshot rendering happens asynchronously after the URL is issued, so
    # the first few fetches can 404 before the PNG is actually ready.
    last_error = None
    for attempt in range(retries):
        if attempt:
            time.sleep(delay)
        resp = requests.get(url, timeout=30)
        if resp.ok and resp.content:
            with open(out_path, "wb") as f:
                f.write(resp.content)
            return
        last_error = RuntimeError(f"snapshot not ready yet (HTTP {resp.status_code})")
    raise last_error


DASHBOARD_DELETE_MUTATION = """
mutation($guid: EntityGuid!) {
  dashboardDelete(guid: $guid) {
    status
    errors {
      description
      type
    }
  }
}
"""


def delete_dashboard(api_key, guid):
    data = nerdgraph_request(api_key, DASHBOARD_DELETE_MUTATION, {"guid": guid})
    result = data["dashboardDelete"]
    if result["errors"]:
        raise NerdGraphError(json.dumps(result["errors"], indent=2))


def entity_permalink(guid):
    # Stable NerdGraph/One redirect that resolves any entity GUID to its page.
    return f"https://one.newrelic.com/redirect/entity/{guid}"


def create_dashboard(api_key, account_id, title, nrql, visualization, permissions):
    dashboard = build_dashboard_input(title, nrql, account_id, visualization, permissions)
    data = nerdgraph_request(
        api_key,
        DASHBOARD_CREATE_MUTATION,
        {"accountId": account_id, "dashboard": dashboard},
    )
    result = data["dashboardCreate"]
    if result["errors"]:
        raise NerdGraphError(json.dumps(result["errors"], indent=2))
    return result["entityResult"]


def build_parser():
    parser = argparse.ArgumentParser(
        prog="newrelic_chart_dashboard.py",
        description=(
            "Create a New Relic dashboard containing a single chart widget "
            "from an NRQL query (read from stdin), and print its URL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NEW_RELIC_API_KEY"),
        metavar="KEY",
        help="NerdGraph User API key (default: $NEW_RELIC_API_KEY).",
    )
    parser.add_argument(
        "--account-id",
        default=os.environ.get("NEW_RELIC_ACCOUNT_ID"),
        metavar="ID",
        help="New Relic account ID (default: $NEW_RELIC_ACCOUNT_ID).",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Title for both the dashboard and its single widget.",
    )
    parser.add_argument(
        "--visualization",
        metavar="VIZ_ID",
        help=(
            "NerdGraph visualization id (e.g. viz.line, viz.stacked-bar, "
            "viz.table, viz.billboard). Note: viz.bar is New Relic's "
            "'Simplified Bar' chart, not a normal bar chart - use "
            "viz.stacked-bar for that. Default: guessed from the query "
            "(TIMESERIES -> viz.line, FACET -> viz.stacked-bar, else viz.billboard)."
        ),
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Make the dashboard PUBLIC_READ_ONLY (viewable without a New Relic login). "
             "Default is PRIVATE (org members only).",
    )
    parser.add_argument(
        "--image-out",
        metavar="PATH",
        help="Also fetch a PNG snapshot of the chart and save it to this path, "
             "via NerdGraph's dashboardWidgetCreateSnapshotUrl mutation. When "
             "given, the dashboard is deleted afterward unless --keep is passed.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the dashboard after saving --image-out (default: delete it). "
             "Has no effect without --image-out, since the dashboard is always "
             "kept in that case.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.api_key:
        parser.error("API key required: set $NEW_RELIC_API_KEY or pass --api-key.")
    if not args.account_id:
        parser.error("Account ID required: set $NEW_RELIC_ACCOUNT_ID or pass --account-id.")

    nrql = sys.stdin.read().strip()
    if not nrql:
        parser.error("No NRQL query provided on stdin.")

    account_id = int(args.account_id)
    visualization = args.visualization or guess_visualization(nrql)
    permissions = "PUBLIC_READ_ONLY" if args.public else "PRIVATE"

    entity = create_dashboard(
        args.api_key, account_id, args.title, nrql, visualization, permissions
    )
    guid = entity["guid"]

    if args.image_out:
        widget = build_declarative_widget(args.title, nrql, account_id, visualization)
        snapshot_url = fetch_widget_snapshot_url(args.api_key, widget)
        download_snapshot_image(snapshot_url, args.image_out)

    keep_dashboard = not args.image_out or args.keep
    if keep_dashboard:
        print(entity_permalink(guid))
    else:
        delete_dashboard(args.api_key, guid)

    if args.image_out:
        print(args.image_out)


if __name__ == "__main__":
    main()
