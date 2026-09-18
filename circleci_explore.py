#!/usr/bin/env python3
"""
circleci_explore.py — translate a CircleCI build/job or pipeline reference up to
its pipeline, then browse workflows, jobs, and artifacts from there.

Given any one of:
  - a legacy CircleCI job URL:   https://circleci.com/gh/{org}/{repo}/{build_num}
  - a CircleCI pipeline URL:     https://app.circleci.com/pipelines/{gh|github}/{org}/{repo}/{pipeline_num}
  - --slug gh/{org}/{repo} --build {build_num}       (or --pipeline {pipeline_num})
  - --org {org} --repo {repo} --build {build_num}    (or --pipeline {pipeline_num})

this script resolves build_num -> job -> workflow -> pipeline, or pipeline_num ->
pipeline directly (printing a summary either way), then supports:
  --list-workflows-in-pipeline
  --list-jobs-in-workflow    [--workflow-name NAME]
  --list-artifacts-in-job    [--workflow-name NAME] [--job-name SUBSTRING]
  --artifact SUBSTRING       [--workflow-name NAME] [--job-name SUBSTRING]
  --list-steps               [--workflow-name NAME] [--job-name SUBSTRING]
  --step-logs SUBSTRING      [--workflow-name NAME] [--job-name SUBSTRING]
  --pipeline-overview        [--json]
  --pipeline-summary

When starting from a build/job, "the workflow" defaults to the one containing
it, and "the job" defaults to the build/job itself; --workflow-name / --job-name
override those defaults. When starting from a pipeline directly, there is no
such default — --workflow-name (and, for job-level actions, --job-name) are
required. --job-name matches by substring; if more than one job matches, the
script halts and asks you to disambiguate (same for --artifact against artifact
paths).

Required environment variable:
  CIRCLE_TOKEN   CircleCI personal API token

Usage:
  # Resolve a job URL up to its pipeline (default action: print summary)
  python circleci_explore.py https://circleci.com/gh/cerebrotech/internal-e2e-tests-service/301325

  # Equivalent, via --slug or --org/--repo
  python circleci_explore.py --slug gh/cerebrotech/internal-e2e-tests-service --build 301325
  python circleci_explore.py --org cerebrotech --repo internal-e2e-tests-service --build 301325

  # Resolve a pipeline URL directly (no build/job involved)
  python circleci_explore.py https://app.circleci.com/pipelines/github/cerebrotech/domino/237614

  # Equivalent, via --slug or --org/--repo + --pipeline
  python circleci_explore.py --slug gh/cerebrotech/domino --pipeline 237614
  python circleci_explore.py --org cerebrotech --repo domino --pipeline 237614

  # List workflows in the resolved pipeline
  python circleci_explore.py <url> --list-workflows-in-pipeline

  # List jobs in the build's own workflow, or a different named workflow
  python circleci_explore.py <url> --list-jobs-in-workflow
  python circleci_explore.py <url> --list-jobs-in-workflow --workflow-name setup_test-e2e

  # List artifacts for a job (default: the build's own job)
  python circleci_explore.py <url> --list-artifacts-in-job
  python circleci_explore.py <url> --list-artifacts-in-job --job-name e2e-run-nexus

  # Dump one artifact's content to stdout (errors if the substring is ambiguous)
  python circleci_explore.py <url> --job-name e2e-run-nexus --artifact helm-values > values.yaml

  # List steps for a job (default: the build's own job)
  python circleci_explore.py <url> --list-steps
  python circleci_explore.py <url> --list-steps --job-name e2e-run-nexus

  # Dump a step's log output to stdout (errors if the substring is ambiguous)
  python circleci_explore.py <url> --job-name e2e-run-nexus --step-logs "Run tests"

  # Starting from a pipeline URL, --workflow-name (and --job-name) are required
  # for workflow/job-level actions since there's no default build/job:
  python circleci_explore.py <pipeline-url> --list-jobs-in-workflow --workflow-name test-e2e
  python circleci_explore.py <pipeline-url> --workflow-name test-e2e --job-name e2e-run-nexus --artifact helm-values

  # Flat list of every step across every workflow/job in the pipeline
  python circleci_explore.py <url> --pipeline-overview

  # Same, as structured JSON: start/end/duration for every step, job, workflow,
  # and the pipeline as a whole, plus a rolled-up DONE/FAIL/SKIP status at each
  # level (FAIL if any child FAILed, else DONE if any child is DONE, else SKIP)
  python circleci_explore.py <url> --pipeline-overview --json

  # Pipeline summary JSON: same shape, but only recurses down to each
  # workflow's jobs (status/timing) — it does not fetch per-job steps or
  # artifacts, so it's much cheaper for a large pipeline.
  python circleci_explore.py <url> --pipeline-summary
"""

import json
import os
import re
import sys
import argparse
from datetime import datetime

import requests

CIRCLE_TOKEN = os.environ.get("CIRCLE_TOKEN", "")
API_V2 = "https://circleci.com/api/v2"
API_V11 = "https://circleci.com/api/v1.1"

_quiet = False


def progress(msg):
    if not _quiet:
        print(f">> {msg}", file=sys.stderr, flush=True)


def _headers():
    return {"Circle-Token": CIRCLE_TOKEN}


def _v2(path):
    r = requests.get(f"{API_V2}/{path}", headers=_headers())
    r.raise_for_status()
    return r.json()


def _v11(path):
    r = requests.get(f"{API_V11}/{path}", headers=_headers())
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Input parsing: URL / --slug / --org+--repo  ->  ("build"|"pipeline", org, repo, num)
# ---------------------------------------------------------------------------

def parse_legacy_build_url(url):
    m = re.search(r'/gh/([^/]+)/([^/]+)/(\d+)', url)
    if not m:
        sys.exit(f"error: could not parse org/repo/build from URL: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def parse_pipeline_url(url):
    m = re.search(r'/pipelines/(?:github|gh|bitbucket|bb)/([^/]+)/([^/]+)/(\d+)', url)
    if not m:
        sys.exit(f"error: could not parse org/repo/pipeline number from URL: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def parse_slug(slug):
    parts = slug.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "gh":
        sys.exit(f"error: --slug must look like 'gh/org/repo', got {slug!r}")
    return parts[1], parts[2]


def resolve_target(args):
    if args.build is not None and args.pipeline is not None:
        sys.exit("error: supply at most one of --build or --pipeline.")

    input_styles = [bool(args.url), bool(args.slug), bool(args.org or args.repo)]
    if sum(input_styles) > 1:
        sys.exit("error: supply exactly one of: a URL, --slug, or --org/--repo.")

    if args.url:
        if args.build is not None:
            sys.exit("error: --build isn't used with a URL (the number is parsed from it).")
        if args.pipeline is not None:
            sys.exit("error: --pipeline isn't used with a URL (the number is parsed from it).")
        if "/pipelines/" in args.url:
            org, repo, pipeline_num = parse_pipeline_url(args.url)
            return "pipeline", org, repo, pipeline_num
        org, repo, build_num = parse_legacy_build_url(args.url)
        return "build", org, repo, build_num

    if args.slug:
        org, repo = parse_slug(args.slug)
    elif args.org or args.repo:
        if not (args.org and args.repo):
            sys.exit("error: --org and --repo must be used together.")
        org, repo = args.org, args.repo
    else:
        sys.exit(
            "error: supply a URL, --slug + --build/--pipeline, or --org + --repo + --build/--pipeline.\n"
            f"  {sys.argv[0]} https://circleci.com/gh/ORG/REPO/BUILD_NUM\n"
            f"  {sys.argv[0]} https://app.circleci.com/pipelines/github/ORG/REPO/PIPELINE_NUM\n"
            f"  {sys.argv[0]} --slug gh/ORG/REPO --build BUILD_NUM\n"
            f"  {sys.argv[0]} --slug gh/ORG/REPO --pipeline PIPELINE_NUM\n"
            f"  {sys.argv[0]} --org ORG --repo REPO --build BUILD_NUM"
        )

    if args.build is not None:
        return "build", org, repo, args.build
    if args.pipeline is not None:
        return "pipeline", org, repo, args.pipeline
    sys.exit("error: --slug/--org+--repo requires --build or --pipeline as well.")

# ---------------------------------------------------------------------------
# CircleCI API calls
# ---------------------------------------------------------------------------

def get_build_v11(slug, build_num):
    return _v11(f"project/{slug}/{build_num}")


def get_workflow(workflow_id):
    return _v2(f"workflow/{workflow_id}")


def get_pipeline(pipeline_id):
    return _v2(f"pipeline/{pipeline_id}")


def get_pipeline_by_number(slug, pipeline_number):
    return _v2(f"project/{slug}/pipeline/{pipeline_number}")


def get_pipeline_workflows(pipeline_id):
    return _v2(f"pipeline/{pipeline_id}/workflow")["items"]


def get_workflow_jobs(workflow_id):
    return _v2(f"workflow/{workflow_id}/job")["items"]


def get_job_artifacts(slug, job_number):
    return _v2(f"project/{slug}/{job_number}/artifacts")["items"]


def get_job_steps(slug, job_number):
    """Job numbers are build numbers, so the v1.1 build endpoint gives us steps."""
    return get_build_v11(slug, job_number).get("steps", [])


def fetch_step_log(output_url):
    r = requests.get(output_url, headers=_headers())
    r.raise_for_status()
    entries = r.json()
    return "".join(e.get("message", "") for e in entries)


def fetch_artifact(url, out_file):
    CHUNK_SIZE = 1024 * 1024
    r = requests.get(url, headers=_headers(), stream=True)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", 0)) or None
    downloaded = 0
    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
        if not chunk:
            continue
        out_file.write(chunk)
        downloaded += len(chunk)
        report_download_progress(downloaded, total)
    finish_download_progress()


def report_download_progress(downloaded, total):
    if _quiet:
        return
    if total:
        pct = downloaded / total * 100
        bar_width = 30
        filled = int(bar_width * downloaded / total)
        bar = "#" * filled + "-" * (bar_width - filled)
        print(f"\r>> [{bar}] {pct:5.1f}%  {_human_bytes(downloaded)}/{_human_bytes(total)}",
              end="", file=sys.stderr, flush=True)
    else:
        print(f"\r>> downloaded {_human_bytes(downloaded)}", end="", file=sys.stderr, flush=True)


def finish_download_progress():
    if not _quiet:
        print(file=sys.stderr, flush=True)


def _human_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"

# ---------------------------------------------------------------------------
# Disambiguation helpers
# ---------------------------------------------------------------------------

def resolve_workflow_id(pipeline_id, workflow_name, default_id, default_name):
    """Pick a workflow by exact name from the pipeline, or fall back to the build's own workflow."""
    if workflow_name is None:
        if default_id is None:
            sys.exit("error: --workflow-name is required (no build/job was given to infer a default workflow from).")
        return default_id, default_name
    workflows = get_pipeline_workflows(pipeline_id)
    matches = [w for w in workflows if w["name"] == workflow_name]
    if not matches:
        available = sorted({w["name"] for w in workflows})
        sys.exit(
            f"error: workflow {workflow_name!r} not found in pipeline.\nAvailable:\n"
            + "\n".join(f"  {n}" for n in available)
        )
    if len(matches) > 1:
        matches.sort(key=lambda w: w["created_at"], reverse=True)
        progress(
            f"{len(matches)} workflows named {workflow_name!r} found (reruns); "
            f"using most recent: id={matches[0]['id']} created_at={matches[0]['created_at']}"
        )
    chosen = matches[0]
    return chosen["id"], chosen["name"]


def resolve_job(workflow_id, job_name, default_job_number, default_job_name):
    """Pick a job by name substring within a workflow, or fall back to the build's own job."""
    if job_name is None:
        if default_job_number is None:
            sys.exit("error: --job-name is required (no build/job was given to infer a default job from).")
        return default_job_number, default_job_name
    jobs = get_workflow_jobs(workflow_id)
    matches = [j for j in jobs if job_name in j["name"]]
    if not matches:
        available = sorted(j["name"] for j in jobs)
        sys.exit(
            f"error: no job matching {job_name!r} in workflow.\nAvailable:\n"
            + "\n".join(f"  {n}" for n in available)
        )
    if len(matches) > 1:
        names = "\n".join(f"  {j['name']}  (job #{j.get('job_number', '?')})" for j in matches)
        sys.exit(f"error: ambiguous — {len(matches)} jobs match {job_name!r}:\n{names}")
    return matches[0]["job_number"], matches[0]["name"]


def resolve_step(steps, substring):
    matches = [s for s in steps if substring in s["name"]]
    if not matches:
        available = "\n".join(f"  {s['name']}" for s in steps)
        sys.exit(f"error: no step name contains {substring!r}.\nAvailable:\n{available}")
    if len(matches) > 1:
        names = "\n".join(f"  {s['name']}" for s in matches)
        sys.exit(f"error: ambiguous — {len(matches)} steps match {substring!r}:\n{names}")
    return matches[0]


def classify_status(statuses):
    """statuses: iterable of CircleCI status strings. Returns 'ok' | 'not-run' | 'fail'."""
    statuses = set(statuses)
    if statuses <= {"success", "fixed"}:
        return "ok"
    if statuses <= {"not_run", "blocked"}:
        return "not-run"
    return "fail"


STATUS_LABELS = {"ok": "DONE", "fail": "FAIL", "not-run": "SKIP"}


def _parse_iso(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def duration_seconds(start, end):
    """start/end: ISO8601 strings (or None). Returns seconds elapsed, or None if either is missing."""
    start_dt, end_dt = _parse_iso(start), _parse_iso(end)
    if start_dt is None or end_dt is None:
        return None
    return (end_dt - start_dt).total_seconds()


def aggregate_label(labels):
    """Roll up child DONE/FAIL/SKIP labels into one: FAIL beats DONE beats SKIP (SKIP is the default for no children)."""
    labels = list(labels)
    if any(label == "FAIL" for label in labels):
        return "FAIL"
    if any(label == "DONE" for label in labels):
        return "DONE"
    return "SKIP"


def resolve_artifact(artifacts, substring):
    matches = [a for a in artifacts if substring in a["path"]]
    if not matches:
        available = "\n".join(f"  {a['path']}" for a in artifacts)
        sys.exit(f"error: no artifact path contains {substring!r}.\nAvailable:\n{available}")
    if len(matches) > 1:
        paths = "\n".join(f"  {a['path']}" for a in matches)
        sys.exit(f"error: ambiguous — {len(matches)} artifacts match {substring!r}:\n{paths}")
    return matches[0]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Translate a CircleCI build/job reference up to its pipeline, then browse workflows/jobs/artifacts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument(
        "url", nargs="?",
        help=(
            "A legacy CircleCI job URL (https://circleci.com/gh/{org}/{repo}/{build_num}) "
            "or a CircleCI pipeline URL (https://app.circleci.com/pipelines/{gh|github}/{org}/{repo}/{pipeline_num})"
        ),
    )
    parser.add_argument("--slug", help="Project slug: gh/{org}/{repo} (use with --build or --pipeline)")
    parser.add_argument("--org", help="Org/user name (use with --repo and --build/--pipeline)")
    parser.add_argument("--repo", help="Repo/project name (use with --org and --build/--pipeline)")
    parser.add_argument("--build", type=int, help="Build/job number (use with --slug, or --org/--repo)")
    parser.add_argument(
        "--pipeline", type=int,
        help="Pipeline number, in place of a pipeline URL (use with --slug, or --org/--repo)",
    )
    parser.add_argument("--workflow-name", help="Use this workflow instead of the one containing --build's job")
    parser.add_argument(
        "--job-name", metavar="NAME_SUBSTRING",
        help="Select a job by name substring within the active workflow (default: --build's own job)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress messages on stderr")

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("--list-workflows-in-pipeline", action="store_true")
    action_group.add_argument("--list-jobs-in-workflow", action="store_true")
    action_group.add_argument("--list-artifacts-in-job", action="store_true")
    action_group.add_argument(
        "--artifact", metavar="SUBSTRING",
        help="Dump this artifact's content to stdout (errors if the substring is ambiguous)",
    )
    action_group.add_argument("--list-steps", action="store_true", help="List steps for a job")
    action_group.add_argument(
        "--step-logs", metavar="SUBSTRING",
        help="Dump this step's log output to stdout (errors if the substring is ambiguous)",
    )
    action_group.add_argument(
        "--pipeline-overview", action="store_true",
        help="Flat list of every step across every workflow/job in the pipeline: '<workflow> <job> <step> (ok|not-run|fail)'",
    )
    action_group.add_argument(
        "--pipeline-summary", action="store_true",
        help=(
            "JSON summary of the pipeline: workflows and their jobs, with status/timing, "
            "but without descending into per-job steps or artifacts (cheaper than --pipeline-overview --json)"
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="With --pipeline-overview, emit structured JSON (timing + rolled-up status) instead of the flat text list",
    )
    args = parser.parse_args()

    if args.json and not args.pipeline_overview:
        sys.exit("error: --json is only supported together with --pipeline-overview.")

    global _quiet
    _quiet = args.quiet
    json_mode = args.json or args.pipeline_summary

    if not CIRCLE_TOKEN:
        sys.exit("error: CIRCLE_TOKEN environment variable is not set.")

    mode, org, repo, num = resolve_target(args)
    slug = f"gh/{org}/{repo}"

    if mode == "build":
        build_num = num
        progress(f"Fetching build {slug}#{build_num} ...")
        build = get_build_v11(slug, build_num)
        wf_info = build.get("workflows") or {}
        workflow_id = wf_info.get("workflow_id")
        if not workflow_id:
            sys.exit(f"error: build {build_num} has no workflow (may predate workflows, or is a plain non-workflow build).")

        progress(f"Fetching workflow {workflow_id} ...")
        workflow = get_workflow(workflow_id)
        pipeline_id = workflow["pipeline_id"]

        progress(f"Fetching pipeline {pipeline_id} ...")
        pipeline = get_pipeline(pipeline_id)

        default_job_number = build_num
        default_job_name = wf_info.get("job_name")
        default_workflow_id = workflow_id
        default_workflow_name = workflow["name"]

        if not json_mode:
            print(f"\n{slug}  build #{build_num}")
            print(f"  job:      {wf_info.get('job_name')}  (id={wf_info.get('job_id')})")
            print(f"  workflow: {workflow['name']!r}  status={workflow['status']}  (id={workflow_id})")
    else:
        pipeline_num = num
        progress(f"Fetching pipeline {slug}#{pipeline_num} ...")
        pipeline = get_pipeline_by_number(slug, pipeline_num)
        pipeline_id = pipeline["id"]

        default_job_number = None
        default_job_name = None
        default_workflow_id = None
        default_workflow_name = None

        if not json_mode:
            print(f"\n{slug}  pipeline #{pipeline_num} (given directly, no build/job)")

    vcs = pipeline.get("vcs", {})
    if not json_mode:
        print(f"  pipeline: #{pipeline['number']}  state={pipeline['state']}  (id={pipeline_id})")
        print(f"  branch:   {vcs.get('branch')}")
        print(f"  commit:   {vcs.get('revision', '')[:12]}  {vcs.get('commit', {}).get('subject', '')}")

    if args.list_workflows_in_pipeline:
        workflows = get_pipeline_workflows(pipeline_id)
        print(f"\nWorkflows in pipeline #{pipeline['number']}:")
        for w in workflows:
            print(f"  {w['status']:12s}  {w['name']!r}  (id={w['id']}, created_at={w['created_at']})")
        return

    if args.list_jobs_in_workflow:
        active_workflow_id, active_workflow_name = resolve_workflow_id(
            pipeline_id, args.workflow_name, default_workflow_id, default_workflow_name
        )
        jobs = get_workflow_jobs(active_workflow_id)
        print(f"\nJobs in workflow {active_workflow_name!r}:")
        for j in sorted(jobs, key=lambda j: j["name"]):
            print(f"  {j['status']:12s}  {j['name']!r}  (job #{j.get('job_number', '?')})")
        return

    if args.list_artifacts_in_job or args.artifact is not None:
        active_workflow_id, _ = resolve_workflow_id(
            pipeline_id, args.workflow_name, default_workflow_id, default_workflow_name
        )
        job_number, job_name = resolve_job(
            active_workflow_id, args.job_name, default_job_number, default_job_name
        )
        progress(f"Fetching artifacts for job {job_name!r} (job #{job_number}) ...")
        artifacts = get_job_artifacts(slug, job_number)

        if args.list_artifacts_in_job:
            if not artifacts:
                print("No artifacts found.")
                return
            print(f"\nArtifacts for {job_name!r} (job #{job_number}):")
            for a in artifacts:
                print(f"  {a['path']}")
            return

        if not artifacts:
            sys.exit("error: no artifacts found for this job.")
        chosen = resolve_artifact(artifacts, args.artifact)
        progress(f"Downloading artifact: {chosen['path']} ...")
        fetch_artifact(chosen["url"], sys.stdout.buffer)
        return

    if args.list_steps or args.step_logs is not None:
        active_workflow_id, _ = resolve_workflow_id(
            pipeline_id, args.workflow_name, default_workflow_id, default_workflow_name
        )
        job_number, job_name = resolve_job(
            active_workflow_id, args.job_name, default_job_number, default_job_name
        )
        progress(f"Fetching steps for job {job_name!r} (job #{job_number}) ...")
        steps = get_job_steps(slug, job_number)

        if args.list_steps:
            if not steps:
                print("No steps found.")
                return
            print(f"\nSteps for {job_name!r} (job #{job_number}):")
            for i, step in enumerate(steps):
                statuses = ", ".join(sorted({a.get("status", "?") for a in step.get("actions", [])}))
                print(f"  [{i}] {step['name']}  status={statuses}")
            return

        if not steps:
            sys.exit("error: no steps found for this job.")
        chosen = resolve_step(steps, args.step_logs)
        for action in chosen.get("actions", []):
            output_url = action.get("output_url")
            if not output_url:
                continue
            progress(f"Fetching log: {chosen['name']!r} action index={action.get('index')} type={action.get('type')} ...")
            sys.stdout.write(fetch_step_log(output_url))
        return

    if args.pipeline_overview:
        workflows = get_pipeline_workflows(pipeline_id)
        workflow_nodes = []
        for w in sorted(workflows, key=lambda w: w["created_at"]):
            wf_node = {
                "name": w["name"],
                "id": w["id"],
                "raw_status": w["status"],
                "start": w.get("created_at"),
                "end": w.get("stopped_at"),
                "duration_seconds": duration_seconds(w.get("created_at"), w.get("stopped_at")),
                "jobs_fetch_failed": False,
                "jobs": [],
            }
            workflow_nodes.append(wf_node)
            try:
                jobs = get_workflow_jobs(w["id"])
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    wf_node["jobs_fetch_failed"] = True
                    wf_node["status"] = "SKIP"
                    continue
                raise

            job_nodes = []
            for j in sorted(jobs, key=lambda j: j["name"]):
                job_number = j.get("job_number")
                job_node = {
                    "name": j["name"],
                    "job_number": job_number,
                    "raw_status": j["status"],
                    "start": j.get("started_at"),
                    "end": j.get("stopped_at"),
                    "duration_seconds": duration_seconds(j.get("started_at"), j.get("stopped_at")),
                    "steps_fetch_failed": False,
                    "steps": [],
                    "artifacts": [],
                }
                job_nodes.append(job_node)
                if job_number is None:
                    job_node["status"] = STATUS_LABELS[classify_status([j["status"]])]
                    continue

                if args.json:
                    progress(f"Fetching artifacts for job {j['name']!r} (job #{job_number}) ...")
                    try:
                        job_node["artifacts"] = [a["path"] for a in get_job_artifacts(slug, job_number)]
                    except requests.HTTPError as e:
                        if e.response is None or e.response.status_code != 404:
                            raise

                progress(f"Fetching steps for job {j['name']!r} (job #{job_number}) ...")
                try:
                    steps = get_job_steps(slug, job_number)
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        job_node["steps_fetch_failed"] = True
                        job_node["status"] = "SKIP"
                        continue
                    raise
                if not steps:
                    job_node["status"] = STATUS_LABELS[classify_status([j["status"]])]
                    continue

                step_nodes = []
                for step in steps:
                    action_statuses = [a.get("status") for a in step.get("actions", [])]
                    kind = classify_status(action_statuses) if action_statuses else classify_status([j["status"]])
                    starts = [a["start_time"] for a in step.get("actions", []) if a.get("start_time")]
                    ends = [a["end_time"] for a in step.get("actions", []) if a.get("end_time")]
                    start = min(starts) if starts else None
                    end = max(ends) if ends else None
                    step_nodes.append({
                        "name": step["name"],
                        "status": STATUS_LABELS[kind],
                        "start": start,
                        "end": end,
                        "duration_seconds": duration_seconds(start, end),
                    })
                job_node["steps"] = step_nodes
                job_node["status"] = aggregate_label(s["status"] for s in step_nodes)

            wf_node["jobs"] = job_nodes
            wf_node["status"] = aggregate_label(jn["status"] for jn in job_nodes)

        if args.json:
            pipeline_start = pipeline.get("created_at")
            workflow_ends = [wn["end"] for wn in workflow_nodes]
            pipeline_end = max(workflow_ends) if workflow_nodes and all(workflow_ends) else None
            result = {
                "slug": slug,
                "pipeline": {
                    "number": pipeline["number"],
                    "id": pipeline_id,
                    "state": pipeline["state"],
                    "branch": vcs.get("branch"),
                    "commit": {
                        "revision": vcs.get("revision"),
                        "subject": vcs.get("commit", {}).get("subject"),
                    },
                    "start": pipeline_start,
                    "end": pipeline_end,
                    "duration_seconds": duration_seconds(pipeline_start, pipeline_end),
                    "status": aggregate_label(wn["status"] for wn in workflow_nodes),
                    "workflows": workflow_nodes,
                },
            }
            print(json.dumps(result, indent=2))
            return

        print()
        for wf_node in workflow_nodes:
            if wf_node["jobs_fetch_failed"]:
                print(f"(SKIP) {wf_node['name']} - -")
                continue
            for job_node in wf_node["jobs"]:
                if not job_node["steps"]:
                    print(f"({job_node['status']}) {wf_node['name']} {job_node['name']} -")
                    continue
                for step_node in job_node["steps"]:
                    print(f"({step_node['status']}) {wf_node['name']} {job_node['name']} {step_node['name']}")
        return

    if args.pipeline_summary:
        workflows = get_pipeline_workflows(pipeline_id)
        workflow_nodes = []
        for w in sorted(workflows, key=lambda w: w["created_at"]):
            wf_node = {
                "name": w["name"],
                "id": w["id"],
                "raw_status": w["status"],
                "start": w.get("created_at"),
                "end": w.get("stopped_at"),
                "duration_seconds": duration_seconds(w.get("created_at"), w.get("stopped_at")),
                "jobs_fetch_failed": False,
                "jobs": [],
            }
            workflow_nodes.append(wf_node)
            progress(f"Fetching jobs for workflow {w['name']!r} (id={w['id']}) ...")
            try:
                jobs = get_workflow_jobs(w["id"])
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    wf_node["jobs_fetch_failed"] = True
                    wf_node["status"] = "SKIP"
                    continue
                raise

            job_nodes = []
            for j in sorted(jobs, key=lambda j: j["name"]):
                job_nodes.append({
                    "name": j["name"],
                    "job_number": j.get("job_number"),
                    "raw_status": j["status"],
                    "status": STATUS_LABELS[classify_status([j["status"]])],
                    "start": j.get("started_at"),
                    "end": j.get("stopped_at"),
                    "duration_seconds": duration_seconds(j.get("started_at"), j.get("stopped_at")),
                })
            wf_node["jobs"] = job_nodes
            wf_node["status"] = aggregate_label(jn["status"] for jn in job_nodes)

        pipeline_start = pipeline.get("created_at")
        workflow_ends = [wn["end"] for wn in workflow_nodes]
        pipeline_end = max(workflow_ends) if workflow_nodes and all(workflow_ends) else None
        result = {
            "slug": slug,
            "pipeline": {
                "number": pipeline["number"],
                "id": pipeline_id,
                "state": pipeline["state"],
                "branch": vcs.get("branch"),
                "commit": {
                    "revision": vcs.get("revision"),
                    "subject": vcs.get("commit", {}).get("subject"),
                },
                "start": pipeline_start,
                "end": pipeline_end,
                "duration_seconds": duration_seconds(pipeline_start, pipeline_end),
                "status": aggregate_label(wn["status"] for wn in workflow_nodes),
                "workflows": workflow_nodes,
            },
        }
        print(json.dumps(result, indent=2))
        return


if __name__ == "__main__":
    main()
