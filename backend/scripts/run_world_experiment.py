"""
Run the whole MiroFish pipeline headless, through the backend HTTP API.

Ontology -> graph build -> create simulation -> prepare -> start -> report.
Designed for experiments on a fictional world (see WORLD_BRIEF_PATH in .env),
but works for any seed material.

The backend must already be running (cd backend && uv run python run.py).

Example:
    uv run python scripts/run_world_experiment.py \
        --file /path/vol3.txt --file /path/carta_del_mondo.md \
        --requirement "Come reagisce la città nei tre mesi dopo la Notte dei Lumi?" \
        --project-name "Mezzaluce, primo esperimento" \
        --lang it --max-rounds 20 --out ./esperimenti/primo

Every step writes its ids to <out>/state.json, so a failed run can be
resumed with --resume (steps already done are skipped).
"""

import argparse
import json
import os
import sys
import time

import httpx


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Api:
    def __init__(self, base_url, lang):
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Accept-Language": lang},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )

    def _check(self, resp):
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code >= 400 or not body.get("success", False):
            raise RuntimeError(f"HTTP {resp.status_code}: {body.get('error') or body}")
        return body.get("data", body)

    def get(self, path):
        return self._check(self.client.get(path))

    def post(self, path, payload=None, **kwargs):
        if payload is not None:
            kwargs["json"] = payload
        return self._check(self.client.post(path, **kwargs))


def poll(fn, done, failed=lambda d: False, interval=10, timeout=6 * 3600, label=""):
    start = time.time()
    last = None
    while True:
        data = fn()
        msg = data.get("message") or data.get("runner_status") or data.get("status")
        progress = data.get("progress")
        line = f"{label}: {msg}" + (f" ({progress}%)" if progress is not None else "")
        if line != last:
            log(line)
            last = line
        if failed(data):
            raise RuntimeError(f"{label} failed: {data.get('error') or data}")
        if done(data):
            return data
        if time.time() - start > timeout:
            raise TimeoutError(f"{label} timed out")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", action="append", required=True, help="seed file (.txt, .md, .pdf); repeatable")
    ap.add_argument("--requirement", required=True, help="the question the simulation should answer")
    ap.add_argument("--project-name", default="world experiment")
    ap.add_argument("--additional-context", default="")
    ap.add_argument("--lang", default="it", help="Accept-Language sent to the backend (zh, en, it)")
    ap.add_argument("--max-rounds", type=int, default=20)
    ap.add_argument("--chunk-size", type=int, default=2000, help="characters per graph chunk (backend default 500)")
    ap.add_argument("--chunk-overlap", type=int, default=200)
    ap.add_argument("--platform", default="parallel", choices=["parallel", "twitter", "reddit"])
    ap.add_argument("--base-url", default="http://localhost:5001")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--resume", action="store_true", help="skip steps already recorded in <out>/state.json")
    ap.add_argument("--skip-report", action="store_true")
    ap.add_argument("--until", choices=["graph", "prepare", "run"], help="stop after this step (resume later with --resume)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    state_path = os.path.join(args.out, "state.json")
    state = {}
    if args.resume and os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

    def save(**kw):
        state.update(kw)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    api = Api(args.base_url, args.lang)
    api.client.get("/health").raise_for_status()

    # 1. Ontology (uploads the seed files)
    if "project_id" not in state:
        log("ontology: uploading seed files")
        files = [("files", (os.path.basename(p), open(p, "rb"))) for p in args.file]
        try:
            data = api.post(
                "/api/graph/ontology/generate",
                data={
                    "simulation_requirement": args.requirement,
                    "project_name": args.project_name,
                    "additional_context": args.additional_context,
                },
                files=files,
            )
        finally:
            for _, (_, fh) in files:
                fh.close()
        save(project_id=data["project_id"], ontology=data.get("ontology"))
        log(f"ontology: project {state['project_id']}")

    # 2. Graph build
    if "graph_id" not in state:
        data = api.post("/api/graph/build", {
            "project_id": state["project_id"],
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
        })
        task_id = data["task_id"]
        task = poll(
            lambda: api.get(f"/api/graph/task/{task_id}"),
            done=lambda d: d["status"] == "completed",
            failed=lambda d: d["status"] == "failed",
            label="graph",
        )
        save(graph_id=task["result"]["graph_id"], graph=task["result"])

    if args.until == "graph":
        log("stopped after graph (--until graph)")
        return

    # 3. Create simulation
    if "simulation_id" not in state:
        data = api.post("/api/simulation/create", {"project_id": state["project_id"], "graph_id": state["graph_id"]})
        save(simulation_id=data["simulation_id"])
        log(f"simulation: {state['simulation_id']}")
    sim = state["simulation_id"]

    # 4. Prepare (profiles + config)
    if not state.get("prepared"):
        data = api.post("/api/simulation/prepare", {"simulation_id": sim})
        task_id = data.get("task_id")
        query = {"task_id": task_id} if task_id else {"simulation_id": sim}
        poll(
            lambda: api.post("/api/simulation/prepare/status", query),
            done=lambda d: d.get("status") in ("completed", "ready"),
            failed=lambda d: d.get("status") == "failed",
            label="prepare",
        )
        save(prepared=True)

    if args.until == "prepare":
        log("stopped after prepare (--until prepare)")
        return

    # 5. Run
    if not state.get("run_done"):
        if not state.get("run_started"):
            api.post("/api/simulation/start", {
                "simulation_id": sim,
                "platform": args.platform,
                "max_rounds": args.max_rounds,
            })
            save(run_started=True)
        poll(
            lambda: api.get(f"/api/simulation/{sim}/run-status"),
            done=lambda d: d.get("runner_status") in ("completed", "stopped"),
            failed=lambda d: d.get("runner_status") == "failed",
            interval=20,
            label="run",
        )
        save(run_done=True)

    if args.until == "run":
        log("stopped after run (--until run)")
        return

    # 6. Report
    if not args.skip_report and not state.get("report_id"):
        data = api.post("/api/report/generate", {"simulation_id": sim})
        task_id = data.get("task_id")
        query = {"task_id": task_id} if task_id else {"simulation_id": sim}
        poll(
            lambda: api.post("/api/report/generate/status", query),
            done=lambda d: d.get("status") == "completed",
            failed=lambda d: d.get("status") == "failed",
            label="report",
        )
        report = api.get(f"/api/report/by-simulation/{sim}")
        save(report_id=report.get("report_id"))
        with open(os.path.join(args.out, "rapporto.md"), "w", encoding="utf-8") as f:
            f.write(report.get("markdown_content", ""))
        log(f"report written to {os.path.join(args.out, 'rapporto.md')}")

    log("done")
    log(f"state: {state_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
