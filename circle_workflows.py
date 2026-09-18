#!/usr/bin/env python3
"""
circle_workflows.py — list CircleCI workflow runs by name and time range.

Walks the /pipeline list for a project (most-recent-first) and fetches
workflows for each pipeline that falls within the window, stopping once
pipelines are older than --since.

Required environment variable:
  CIRCLE_TOKEN   CircleCI personal API token

Usage:
  # All runs of "test-e2e" workflow in the past week
  python circle_workflows.py --workflow test-e2e --since 2025-05-29

  # Substring match (default) — catches "test-e2e", "test-e2e-pr", etc.
  python circle_workflows.py --workflow test-e2e --since 2025-05-29 --until 2025-06-05

  # Exact name match
  python circle_workflows.py --workflow test-e2e --since 2025-05-29 --exact

  # Filter by branch and/or status
  python circle_workflows.py --workflow nightly --since 2025-06-01 --branch main
  python circle_workflows.py --workflow nightly --since 2025-06-01 --status failed

  # Different project
  python circle_workflows.py --workflow build --since 2025-06-01 --project gh/cerebrotech/domino

  # Include clickable URLs
  python circle_workflows.py --workflow test-e2e --since 2025-06-01 --urls

  # Show first-failed and last-successful job names per workflow
  python circle_workflows.py --workflow test-e2e --since 2025-06-01 --where-failed

  # Machine-readable JSON
  python circle_workflows.py --workflow test-e2e --since 2025-06-01 --json
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone

import requests

CIRCLE_TOKEN = os.environ.get("CIRCLE_TOKEN", "")
API_V2 = "https://circleci.com/api/v2"
DEFAULT_PROJECT = "gh/cerebrotech/internal-e2e-tests-service"

# Status values CircleCI uses
STATUSES = ["success", "failed", "error", "canceled", "unauthorized", "running", "on_hold", "failing"]
IN_PROGRESS_STATUSES = {"running", "on_hold", "failing"}


def _headers():
    return {"Circle-Token": CIRCLE_TOKEN, "Content-Type": "application/json"}


def _get(path, params=None):
    r = requests.get(f"{API_V2}/{path}", headers=_headers(), params=params)
    r.raise_for_status()
    return r.json()


def _parse_dt(s):
    """Parse ISO date string (YYYY-MM-DD or full ISO8601) to UTC-aware datetime."""
    if len(s) == 10:
        s += "T00:00:00Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fmt_dt(s):
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def _duration_secs(created, stopped):
    """Return seconds between two ISO timestamps, or None on error."""
    if not created or not stopped:
        return None
    try:
        dt1 = datetime.fromisoformat(created.replace("Z", "+00:00"))
        dt2 = datetime.fromisoformat(stopped.replace("Z", "+00:00"))
        return max(0, int((dt2 - dt1).total_seconds()))
    except Exception:
        return None


def _fmt_secs(secs):
    """Format integer seconds as a human-readable duration string."""
    if secs is None:
        return ""
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _iter_pages(path, params=None):
    """Yield all items from a paginated CircleCI endpoint."""
    params = dict(params or {})
    while True:
        data = _get(path, params=params)
        items = data.get("items", [])
        yield from items
        next_token = data.get("next_page_token")
        if not next_token:
            break
        params["page-token"] = next_token


def _iter_pipelines(slug, branch=None):
    """Yield pipelines for the project, most-recent first."""
    params = {}
    if branch:
        params["branch"] = branch
    yield from _iter_pages(f"project/{slug}/pipeline", params=params)


def _iter_workflows(pipeline_id):
    """Yield all workflows for a pipeline."""
    yield from _iter_pages(f"pipeline/{pipeline_id}/workflow")


def _workflow_url(slug, pipeline_number, workflow_id):
    """Build the CircleCI web app URL for a workflow."""
    # API slugs use gh/ and bb/; the web app uses github/ and bitbucket/
    url_slug = slug
    for short, long in [("gh/", "github/"), ("bb/", "bitbucket/")]:
        if url_slug.startswith(short):
            url_slug = long + url_slug[len(short):]
            break
    return f"https://app.circleci.com/pipelines/{url_slug}/{pipeline_number}/workflows/{workflow_id}"


def _get_job_summary(workflow_id):
    """
    Return (first_failed_name, last_success_name) for a workflow.

    first_failed_name: name of the build job that stopped earliest among failed jobs.
    last_success_name: name of the build job that stopped latest among successful jobs.
    Either value is "" when no matching jobs exist.
    """
    jobs = list(_iter_pages(f"workflow/{workflow_id}/job"))
    # Ignore approval jobs — they aren't real build steps
    build_jobs = [j for j in jobs if j.get("type") != "approval"]

    failed = [j for j in build_jobs if j.get("status") == "failed" and j.get("stopped_at")]
    success = [j for j in build_jobs if j.get("status") == "success" and j.get("stopped_at")]

    first_failed = min(failed, key=lambda j: j["stopped_at"])["name"] if failed else ""
    last_success = max(success, key=lambda j: j["stopped_at"])["name"] if success else ""
    return first_failed, last_success


def main():
    parser = argparse.ArgumentParser(
        description="List CircleCI workflow runs matching a name within a time range.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("--project", "-p", default=DEFAULT_PROJECT, metavar="SLUG",
                        help=f"Project slug (default: {DEFAULT_PROJECT})")
    parser.add_argument("--workflow", "-w", required=True,
                        help="Workflow name to match (substring by default; see --exact)")
    parser.add_argument("--since", required=True, metavar="DATE",
                        help="Start of time range, inclusive (YYYY-MM-DD or ISO8601)")
    parser.add_argument("--until", metavar="DATE",
                        help="End of time range, inclusive (YYYY-MM-DD or ISO8601; default: now)")
    parser.add_argument("--branch", "-b", metavar="BRANCH",
                        help="Filter pipelines to this branch")
    parser.add_argument("--exact", action="store_true",
                        help="Exact name match instead of substring")
    parser.add_argument("--status", metavar="STATUS",
                        help=f"Filter by workflow status: {', '.join(STATUSES)}")
    parser.add_argument("--urls", action="store_true",
                        help="Add a column with the CircleCI web URL for each workflow")
    parser.add_argument("--where-failed", action="store_true",
                        help="Add columns for the first failed job and last successful job"
                             " (fetches jobs per workflow; slower)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Output results as JSON array")
    args = parser.parse_args()

    if not CIRCLE_TOKEN:
        sys.exit("error: CIRCLE_TOKEN environment variable is not set.")

    since = _parse_dt(args.since)
    until = _parse_dt(args.until) if args.until else datetime.now(timezone.utc)

    if since > until:
        sys.exit(f"error: --since ({args.since}) is after --until")

    slug = args.project
    match_desc = f"name={args.workflow!r}" + (" (exact)" if args.exact else " (substring)")
    print(
        f"Searching {slug} for workflows matching {match_desc}"
        f" from {since.strftime('%Y-%m-%d')} to {until.strftime('%Y-%m-%d')} ...",
        file=sys.stderr,
    )

    results = []
    pipelines_fetched = 0
    pipelines_in_range = 0

    for pipeline in _iter_pipelines(slug, branch=args.branch):
        created_str = pipeline.get("created_at", "")
        if not created_str:
            continue

        pipeline_dt = _parse_dt(created_str)

        if pipeline_dt < since:
            # Pipelines are most-recent-first; everything from here is older.
            break

        if pipeline_dt > until:
            pipelines_fetched += 1
            continue

        pipelines_fetched += 1
        pipelines_in_range += 1

        branch = (pipeline.get("vcs") or {}).get("branch", "")
        pipeline_number = pipeline.get("number")
        pipeline_id = pipeline["id"]

        for wf in _iter_workflows(pipeline_id):
            wf_name = wf.get("name", "")
            if args.exact:
                if wf_name != args.workflow:
                    continue
            else:
                if args.workflow not in wf_name:
                    continue

            if args.status and wf.get("status") != args.status:
                continue

            wf_id = wf["id"]
            wf_status = wf.get("status", "")
            wf_created = wf.get("created_at", "")
            wf_stopped = wf.get("stopped_at", "")
            in_progress = not wf_stopped and wf_status in IN_PROGRESS_STATUSES
            effective_end = (
                datetime.now(timezone.utc).isoformat() if in_progress else wf_stopped
            )
            entry = {
                "workflow_id": wf_id,
                "workflow_name": wf_name,
                "status": wf_status,
                "created_at": wf_created,
                "stopped_at": wf_stopped,
                "duration_secs": _duration_secs(wf_created, effective_end),
                "duration_in_progress": in_progress,
                "pipeline_id": pipeline_id,
                "pipeline_number": pipeline_number,
                "branch": branch,
            }
            if args.urls:
                entry["url"] = _workflow_url(slug, pipeline_number, wf_id)
            if args.where_failed:
                first_failed, last_success = _get_job_summary(wf_id)
                entry["first_failed_job"] = first_failed
                entry["last_success_job"] = last_success
            results.append(entry)

    print(
        f"Checked {pipelines_fetched} pipeline(s) ({pipelines_in_range} in range); "
        f"found {len(results)} matching workflow run(s).",
        file=sys.stderr,
    )

    if args.json_out:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No matching workflows found.")
        return

    results.sort(key=lambda r: r["created_at"], reverse=True)

    # Column widths
    max_wf = max(len(r["workflow_name"]) for r in results)
    max_branch = min(max(len(r["branch"]) for r in results), 40)

    def _dur_str(r):
        s = _fmt_secs(r.get("duration_secs"))
        return f"({s})" if s and r.get("duration_in_progress") else s

    max_dur = max((len(_dur_str(r)) for r in results), default=3)
    max_dur = max(max_dur, 3)  # at least "dur"

    header_parts = [
        f"{'created':<16}",
        f"{'dur':>{max_dur}}",
        f"{'status':<12}",
        f"{'workflow':<{max_wf}}",
        f"{'branch':<{max_branch}}",
        f"{'#':>6}",
        f"{'workflow_id':<36}",
    ]
    if args.where_failed:
        max_ff = max((len(r.get("first_failed_job", "")) for r in results), default=0)
        max_ff = max(max_ff, len("first_failed"))
        max_ls = max((len(r.get("last_success_job", "")) for r in results), default=0)
        max_ls = max(max_ls, len("last_success"))
        header_parts.append(f"{'first_failed':<{max_ff}}")
        header_parts.append(f"{'last_success':<{max_ls}}")
    if args.urls:
        header_parts.append("url")

    header = "  ".join(header_parts)
    print()
    print(header)
    print("-" * len(header))

    for r in results:
        dur = _dur_str(r)
        branch_col = r["branch"][:max_branch]
        row_parts = [
            f"{_fmt_dt(r['created_at']):<16}",
            f"{dur:>{max_dur}}",
            f"{r['status']:<12}",
            f"{r['workflow_name']:<{max_wf}}",
            f"{branch_col:<{max_branch}}",
            f"{str(r['pipeline_number'] or ''):>6}",
            f"{r['workflow_id']:<36}",
        ]
        if args.where_failed:
            row_parts.append(f"{r.get('first_failed_job', ''):<{max_ff}}")
            row_parts.append(f"{r.get('last_success_job', ''):<{max_ls}}")
        if args.urls:
            row_parts.append(r.get("url", ""))
        print("  ".join(row_parts))

    print(f"\nTotal: {len(results)}")


if __name__ == "__main__":
    main()
