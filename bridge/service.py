"""Authenticated webhook ingestion; only the worker talks to the remote APIs."""
import hashlib
import hmac
import logging
import threading
import time

from flask import Flask, jsonify, request


def create_app(config, state, github_secret, gitee_secret="", worker=None):
    if not github_secret:
        raise ValueError("GITHUB_WEBHOOK_SECRET is required for serve")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    @app.get("/healthz")
    def health():
        alive = worker is None or worker.is_alive()
        return jsonify({"alive": alive}), 200 if alive else 503

    @app.post("/webhooks/<platform>")
    def webhook(platform):
        raw = request.get_data()
        if platform == "github":
            signature = "sha256=" + hmac.new(github_secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, request.headers.get("X-Hub-Signature-256", "")):
                return jsonify({"error": "invalid signature"}), 401
        elif platform != "gitee" or not gitee_secret or not config.sync["reverse_comments"]:
            return jsonify({"error": "disabled endpoint"}), 404
        event = request.get_json(silent=True)
        if not isinstance(event, dict):
            return jsonify({"error": "expected JSON object"}), 400
        if platform == "gitee":
            # Gitee webhook PASSWORD mode; signature mode is deliberately not guessed.
            password = request.headers.get("X-Gitee-Token") or event.get("password", "")
            if not isinstance(password, str) or not hmac.compare_digest(password, gitee_secret):
                return jsonify({"error": "invalid password"}), 401
        event_type = request.headers.get("X-GitHub-Event" if platform == "github" else "X-Gitee-Event", "")
        if event_type == "ping":
            return jsonify({"accepted": True}), 200
        supported = ({"push", "create", "delete", "issues", "issue_comment", "pull_request",
                      "pull_request_review_comment", "label", "milestone", "repository"}
                     if platform == "github" else {"Note Hook", "Issue Hook", "Pull Request Hook"})
        if event_type not in supported:
            return jsonify({"ignored": True}), 200
        repository = event.get("repository")
        if not isinstance(repository, dict):
            return jsonify({"error": "invalid repository"}), 400
        name = repository.get("full_name", "")
        if not isinstance(name, str):
            return jsonify({"error": "invalid repository"}), 400
        repo = next((r for r in config.repositories if getattr(r, platform).lower() == name.lower()), None)
        if repo is None:
            return jsonify({"error": "repository not configured"}), 404
        delivery = request.headers.get("X-GitHub-Delivery" if platform == "github" else "X-Gitee-Delivery")
        delivery = delivery or hashlib.sha256(raw).hexdigest()
        if len(delivery) > 200:
            return jsonify({"error": "invalid delivery ID"}), 400
        inserted = state.enqueue(repo.key, platform, delivery)
        return jsonify({"queued": inserted}), 202

    return app


def run_jobs(config, state, engine):
    failures = 0
    for job in state.jobs():
        repo = next((r for r in config.repositories if r.key == job["repo"]), None)
        if repo is None:
            continue
        try:
            engine.reconcile(repo)
        except Exception as exc:
            failures += 1
            logging.error("Synchronization failed for %s: %s", repo.key, exc)
            state.finish(job, exc)
        else:
            state.finish(job)
            logging.info("Synchronized %s", repo.key)
    return failures


class Worker(threading.Thread):
    def __init__(self, config, state, engine):
        super().__init__(name="bridge-worker", daemon=True)
        self.config, self.state, self.engine = config, state, engine
        self.stop_event = threading.Event()

    def run(self):
        next_scan = 0
        try:
            while not self.stop_event.is_set():
                if time.monotonic() >= next_scan:
                    for repo in self.config.repositories:
                        self.state.enqueue(repo.key)
                    next_scan = time.monotonic() + self.config.interval
                run_jobs(self.config, self.state, self.engine)
                self.stop_event.wait(1)
        except Exception:
            logging.exception("Worker stopped unexpectedly")
