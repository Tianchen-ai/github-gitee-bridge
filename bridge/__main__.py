import argparse
import json
import logging
import os
import sys

from .api import API
from .config import Config, load_env_file
from .engine import Engine
from .git import GitSync
from .service import Worker, create_app, run_jobs
from .state import State


def main():
    parser = argparse.ArgumentParser(description="Persistent GitHub → Gitee bridge")
    parser.add_argument("--config", default="bridge.toml")
    parser.add_argument("--env-file", help="Read literal credentials/settings from a file (overrides environment)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate configuration without network access")
    sub.add_parser("once", help="Reconcile all configured repositories once")
    sub.add_parser("status", help="Show jobs and uncertain writes")
    sub.add_parser("mappings", help="Show cross-platform IDs, numbers, URLs and canonical PR state")
    serve = sub.add_parser("serve", help="Run webhook receiver and periodic worker")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", default=8080, type=int)
    resolve = sub.add_parser("resolve", help="Allow retry of an uncertain create after manual inspection")
    resolve.add_argument("--repository", required=True, help="Exact repo key from status")
    resolve.add_argument("--kind", required=True)
    resolve.add_argument("--source", required=True)
    resolve.add_argument("--confirm-absent", action="store_true", required=True,
                         help="Confirm the remote object was NOT created; incorrect use can duplicate")
    args = parser.parse_args()
    if args.env_file:
        os.environ.update(load_env_file(args.env_file))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load(args.config)
    if args.command == "check":
        print(f"Valid: {len(config.repositories)} repository mappings")
        return 0
    state = State(config.state_dir)
    if args.command == "status":
        print(json.dumps(state.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mappings":
        print(json.dumps(state.mappings(), ensure_ascii=False, indent=2))
        return 0
    with state.worker_lock():
        if args.command == "resolve":
            item = state.get(args.repository, args.kind, args.source)
            if not item or not item["pending"] or item["target"]:
                raise ValueError("Not an unresolved create intent")
            state.clear_pending(args.repository, args.kind, args.source)
            state.enqueue(args.repository)
            return 0
        gh_token, gt_token = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITEE_TOKEN")
        if not gh_token or not gt_token:
            raise ValueError("GITHUB_TOKEN and GITEE_TOKEN are required")
        gh, gt = API("github", gh_token), API("gitee", gt_token)
        gt_user = gt.request("GET", "/user")["login"]
        engine = Engine(config, state, gh, gt, GitSync(gh_token, gt_token, gt_user))
        if args.command == "once":
            for repo in config.repositories:
                state.enqueue(repo.key)
            # One-shot must not silently succeed while an older job is in backoff.
            with state.connect() as db:
                for repo in config.repositories:
                    db.execute("UPDATE jobs SET next_run=0 WHERE repo=?", (repo.key,))
            return 1 if run_jobs(config, state, engine) else 0
        from waitress import serve
        worker = Worker(config, state, engine)
        app = create_app(config, state, os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
                         os.environ.get("GITEE_WEBHOOK_SECRET", ""), worker)
        worker.start()
        try:
            serve(app, host=args.host, port=args.port, threads=4,
                  max_request_body_size=2 * 1024 * 1024)
        finally:
            worker.stop_event.set()
            worker.join()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.error("%s", exc)
        sys.exit(1)
