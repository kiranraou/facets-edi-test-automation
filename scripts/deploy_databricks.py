"""Deploy the framework to a Databricks (Free Edition) workspace with the Python SDK.

    # credentials come from .env (DATABRICKS_HOST / DATABRICKS_TOKEN), see .env.example
    python scripts/deploy_databricks.py --run setup
    python scripts/deploy_databricks.py --run regression
    python scripts/deploy_databricks.py --app

What it does:
  1. builds the facets_qa wheel and uploads it to the workspace (content-hashed path)
  2. creates/updates two serverless jobs:
       facets-qa | 1. setup facets tables   (one-off: DDL + 20k synthetic members)
       facets-qa | 2. daily regression      (270/271 on 25 Spark partitions + 837 claims)
  3. optionally runs a job and prints each task's output
  4. optionally deploys the Streamlit app (Databricks Apps)

This mirrors databricks.yml (Asset Bundle), which needs Databricks CLI >= 0.250.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from facets_qa.common.env import load_dotenv, workspace_client  # noqa: E402
from databricks.sdk.service import apps, compute, jobs, workspace

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "workspace.facets_qa"
JOB_SETUP = "facets-qa | 1. setup facets tables"
JOB_REGRESSION = "facets-qa | 2. daily regression"
APP_NAME = "facets-qa-dashboard"


def build_wheel() -> Path:
    dist = ROOT / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(dist), str(ROOT)], check=True)
    return next(dist.glob("facets_qa-*.whl"))


def upload(w: WorkspaceClient, local: Path, remote: str) -> None:
    w.workspace.mkdirs(str(Path(remote).parent))
    w.workspace.upload(remote, io.BytesIO(local.read_bytes()), format=workspace.ImportFormat.AUTO, overwrite=True)


def upload_wheel(w: WorkspaceClient, base: str) -> str:
    whl = build_wheel()
    digest = hashlib.sha256(whl.read_bytes()).hexdigest()[:10]
    remote = f"{base}/wheels/{digest}/{whl.name}"  # new path per build so serverless envs never cache stale code
    upload(w, whl, remote)
    print(f"uploaded {whl.name} -> {remote}")
    return remote


def wheel_task(key: str, entry_point: str, params: list[str], depends_on: list[str] | None = None) -> jobs.Task:
    return jobs.Task(
        task_key=key,
        environment_key="default",
        python_wheel_task=jobs.PythonWheelTask(package_name="facets_qa", entry_point=entry_point, parameters=params),
        depends_on=[jobs.TaskDependency(task_key=d) for d in (depends_on or [])],
        timeout_seconds=3600,
    )


def upsert_job(w: WorkspaceClient, name: str, tasks: list[jobs.Task], wheel: str,
               schedule: jobs.CronSchedule | None = None) -> int:
    settings = dict(
        name=name,
        tasks=tasks,
        environments=[jobs.JobEnvironment(
            environment_key="default",
            spec=compute.Environment(environment_version="2", dependencies=[wheel]),
        )],
        max_concurrent_runs=1,
        schedule=schedule,
        tags={"project": "facets-qa"},
    )
    existing = next((j for j in w.jobs.list(name=name)), None)
    if existing:
        w.jobs.reset(existing.job_id, jobs.JobSettings(**settings))
        print(f"updated job {existing.job_id}: {name}")
        return existing.job_id
    job_id = w.jobs.create(**settings).job_id
    print(f"created job {job_id}: {name}")
    return job_id


def run_and_report(w: WorkspaceClient, job_id: int) -> bool:
    print(f"running job {job_id} ... (serverless start-up takes ~1-2 min)")
    waiter = w.jobs.run_now(job_id)
    try:
        waiter.result(timeout=__import__("datetime").timedelta(minutes=40))
    except Exception as exc:  # failed runs raise; we still want the task output below
        print(f"run did not succeed: {str(exc)[:200]}")
    run = w.jobs.get_run(waiter.run_id)
    ok = run.state.result_state == jobs.RunResultState.SUCCESS
    for t in run.tasks or []:
        out = w.jobs.get_run_output(t.run_id)
        state = t.state.result_state.value if t.state and t.state.result_state else "?"
        print(f"\n--- task {t.task_key}: {state}")
        if out.logs:
            print(out.logs[-4000:])
        if out.error:
            print("ERROR:", out.error)
            trace = __import__("re").sub(r"\x1b\[[0-9;]*m", "", out.error_trace or "")
            print(trace[-2500:])
    print(f"\nrun page: {run.run_page_url}")
    return ok


def deploy_app(w: WorkspaceClient, base: str, warehouse_id: str) -> None:
    src = f"{base}/app"
    # The app ships its own copy of the facets_qa package (Apps only see the source folder).
    staging = ROOT / "dist" / "app"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(ROOT / "app", staging, ignore=shutil.ignore_patterns("__pycache__", ".streamlit"))
    shutil.copytree(ROOT / "src" / "facets_qa", staging / "facets_qa", ignore=shutil.ignore_patterns("__pycache__"))
    for f in staging.rglob("*"):
        if f.is_file():
            upload(w, f, f"{src}/{f.relative_to(staging).as_posix()}")
    print(f"uploaded app source -> {src}")

    resources = [apps.AppResource(name="sql-warehouse", sql_warehouse=apps.AppResourceSqlWarehouse(
        id=warehouse_id, permission=apps.AppResourceSqlWarehouseSqlWarehousePermission.CAN_USE))]
    existing = next((a for a in w.apps.list() if a.name == APP_NAME), None)
    if existing is None:
        print("creating app (first time takes a few minutes) ...")
        existing = w.apps.create_and_wait(apps.App(name=APP_NAME, resources=resources,
                                                   description="Facets 270/271 + 837 QA dashboard"))
    else:
        w.apps.update(APP_NAME, apps.App(name=APP_NAME, resources=resources))
        if existing.compute_status and existing.compute_status.state != apps.ComputeState.ACTIVE:
            w.apps.start_and_wait(APP_NAME)

    sp = existing.service_principal_client_id
    for stmt in (f"GRANT USE CATALOG ON CATALOG {SCHEMA.split('.')[0]} TO `{sp}`",
                 f"GRANT USE SCHEMA, SELECT ON SCHEMA {SCHEMA} TO `{sp}`"):
        w.statement_execution.execute_statement(statement=stmt, warehouse_id=warehouse_id, wait_timeout="50s")
    print(f"granted read access on {SCHEMA} to app service principal {sp}")

    dep = w.apps.deploy_and_wait(APP_NAME, apps.AppDeployment(source_code_path=src))
    print(f"deployment {dep.deployment_id}: {dep.status.state.value} {dep.status.message or ''}")
    print(f"app url: {w.apps.get(APP_NAME).url}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", help="Databricks CLI profile, used only when .env has no token")
    p.add_argument("--run", choices=["setup", "regression"])
    p.add_argument("--app", action="store_true", help="deploy the Streamlit app")
    p.add_argument("--members", type=int, default=20_000)
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    w = workspace_client(args.profile)
    me = w.current_user.me().user_name
    base = f"/Workspace/Users/{me}/facets-qa"
    wheel = upload_wheel(w, base)

    setup_id = upsert_job(w, JOB_SETUP, [
        wheel_task("create_and_seed_facets", "setup-facets", [f"--schema={SCHEMA}", f"--members={args.members}"]),
    ], wheel)
    regression_id = upsert_job(w, JOB_REGRESSION, [
        wheel_task("eligibility_270_271", "run-eligibility-spark",
                   [f"--schema={SCHEMA}", "--gateway=mock", "--workers=25", "--failure-rate=0.05", "--drift=10"]),
        wheel_task("claims_837", "run-claims-databricks", [f"--schema={SCHEMA}"]),
    ], wheel, schedule=jobs.CronSchedule(quartz_cron_expression="0 0 22 ? * MON-FRI",
                                          timezone_id="America/Chicago", pause_status=jobs.PauseStatus.PAUSED))

    ok = True
    if args.run == "setup":
        ok = run_and_report(w, setup_id)
    elif args.run == "regression":
        ok = run_and_report(w, regression_id)

    if args.app:
        warehouse = next(iter(w.warehouses.list()))
        deploy_app(w, base, warehouse.id)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
