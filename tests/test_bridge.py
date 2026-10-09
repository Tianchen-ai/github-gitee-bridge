import copy
import hashlib
import hmac
import json
import subprocess
from unittest.mock import Mock

import pytest
import requests

from bridge.api import API, APIError
from bridge.config import Config, Repository, load_env_file
from bridge.engine import Engine, marker
from bridge.git import GitSync
from bridge.service import create_app, run_jobs
from bridge.state import State
from lib.sync_repo import mirror_sync


class FakeAPI:
    """Stateful strict subset of the published REST contract (not a live platform)."""
    def __init__(self, platform):
        self.platform = platform
        self.owner = "gh" if platform == "github" else "gt"
        self.bot_id = 100 if platform == "github" else 200
        self.rows = {"issues": [], "pulls": [], "labels": [], "milestones": [], "branches": []}
        self.comment_rows = {}
        self.calls = []
        self.counter = 500
        self.lose_next_post = False

    def list(self, path, **params):
        self.calls.append(("GET_LIST", path, params))
        prefix = f"/repos/{self.owner}/project/"
        assert path.startswith(prefix), path
        suffix = path[len(prefix):]
        if suffix in self.rows:
            return copy.deepcopy(self.rows[suffix])
        assert suffix.endswith("/comments"), path
        return copy.deepcopy(self.comment_rows.get(path, []))

    def request(self, method, path, *, data=None, params=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if path == "/user":
            return {"id": self.bot_id, "login": self.owner}
        if method == "GET" and path == f"/repos/{self.owner}/project":
            return {"private": False, "description": ""}
        if method == "GET" and "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        if method == "GET" and "/pulls/" in path:
            return copy.deepcopy(next(p for p in self.rows["pulls"] if str(p["number"]) == path.split("/")[-1]))
        if method == "POST":
            self.counter += 1
            result = {"id": self.counter, "user": {"id": self.bot_id, "login": self.owner},
                      "state": "open", "labels": [], **data}
            if path.endswith("/comments"):
                self.comment_rows.setdefault(path, []).append(result)
            else:
                kind = path.split("/")[-1]
                assert kind in {"issues", "pulls", "labels", "milestones"}, path
                if kind == "labels" and self.platform == "gitee":
                    assert not data["color"].startswith("#"), "Live Gitee requires bare hex"
                    assert 2 <= len(data["name"]) <= 20 and " " not in data["name"]
                if kind == "issues" and self.platform == "gitee":
                    assert path == "/repos/gt/issues" and data["repo"] == "project"
                result["number"] = f"I{self.counter}" if kind == "issues" and self.platform == "gitee" else self.counter
                if kind == "pulls":
                    assert data["head"] and data["base"]
                    result["base"] = {"ref": data["base"]}
                self.rows[kind].append(result)
            if self.lose_next_post:
                self.lose_next_post = False
                raise RuntimeError("response lost after remote commit")
            return copy.deepcopy(result)
        if method == "PATCH":
            parts = path.split("/")
            if parts[-2] == "comments":
                row = next(c for rows in self.comment_rows.values() for c in rows if str(c["id"]) == parts[-1])
            else:
                kind = parts[-2]
                if kind == "issues" and self.platform == "gitee":
                    assert path.startswith("/repos/gt/issues/") and data["repo"] == "project"
                row = next(r for r in self.rows[kind] if str(r["name"] if kind == "labels" else r["number"]) == parts[-1])
            row.update(copy.deepcopy(data))
            return copy.deepcopy(row)
        if method == "PUT" and path.endswith("/labels"):
            parts = path.split("/")
            row = next(r for r in self.rows[parts[-3]] if str(r["number"]) == parts[-2])
            row["labels"] = [{"name": x} for x in data]
            return row["labels"]
        raise AssertionError((method, path, data))


def issue(number=31):
    return {"id": 1000 + number, "number": number, "title": "A bug", "body": "Details",
            "state": "open", "user": {"id": 1, "login": "alice"}, "labels": [],
            "html_url": f"https://github.com/gh/project/issues/{number}"}


def comment(identity=41, platform="github"):
    return {"id": identity, "body": "Discussion", "user": {"id": 2, "login": "bob"},
            "html_url": f"https://{platform}.com/comment/{identity}"}


def pull(number=52, state="open"):
    return {**issue(number), "state": state, "merged": state == "closed", "merge_commit_sha": "b" * 40,
            "head": {"sha": "a" * 40, "ref": "feature", "label": "fork:feature"},
            "base": {"ref": "main"}}


@pytest.fixture
def setup(tmp_path):
    repo = Repository("gh/project", "gt/project")
    config = Config([repo], tmp_path)
    config.sync["git"] = False
    config.sync["milestones"] = False
    state = State(tmp_path)
    gh, gt = FakeAPI("github"), FakeAPI("gitee")
    git = Mock()
    return repo, config, state, gh, gt, Engine(config, state, gh, gt, git)


def test_issue_reconcile_edit_close_comments_and_restart(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["issues"] = [issue()]
    path = "/repos/gh/project/issues/31/comments"
    gh.comment_rows[path] = [comment()]
    engine.reconcile(repo)
    mapping = state.get(repo.key, "issue", 1031)
    assert mapping["target"] == "I501"  # deliberately unrelated identifiers
    detail = next(m for m in state.mappings() if m["kind"] == "issue")
    assert detail["source_number"] == "31" and detail["source_url"].endswith("/31")
    assert len(gt.rows["issues"]) == 1
    gh.rows["issues"][0].update(title="Edited", state="closed")
    gh.comment_rows[path][0]["body"] = "edited comment"
    gh.comment_rows[path].append(comment(42))
    state = State(cfg.state_dir)
    Engine(cfg, state, gh, gt, Mock()).reconcile(repo)
    engine.reconcile(repo)
    assert len(gt.rows["issues"]) == 1
    assert gt.rows["issues"][0]["title"] == "Edited"
    assert gt.rows["issues"][0]["state"] == "closed"
    comments = gt.comment_rows["/repos/gt/project/issues/I501/comments"]
    assert len(comments) == 2 and "edited comment" in comments[0]["body"]


def test_lost_post_response_recovers_without_duplicate(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["issues"] = [issue()]
    gt.lose_next_post = True
    with pytest.raises(RuntimeError, match="response lost"):
        engine.reconcile(repo)
    assert state.get(repo.key, "issue", 1031)["pending"]
    Engine(cfg, State(cfg.state_dir), gh, gt, Mock()).reconcile(repo)
    assert len(gt.rows["issues"]) == 1
    assert not state.get(repo.key, "issue", 1031)["pending"]


def test_ambiguous_write_absent_does_not_retry(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["issues"] = [issue()]
    state.begin(repo.key, "issue", 1031)
    with pytest.raises(RuntimeError, match="Uncertain"):
        engine.reconcile(repo)
    assert not gt.rows["issues"]


def test_non_bot_forged_marker_not_adopted(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["issues"] = [issue()]
    gt.rows["issues"] = [{**issue(), "number": "Ibad", "body": marker(repo.key, "issue", 1031)}]
    engine.reconcile(repo)
    assert len(gt.rows["issues"]) == 2
    assert state.get(repo.key, "issue", 1031)["target"] != "Ibad"


def test_pr_open_merge_archive_and_review_comments(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["pulls"] = [pull(), pull(53, "closed")]
    gh.comment_rows["/repos/gh/project/issues/52/comments"] = [comment()]
    gh.comment_rows["/repos/gh/project/pulls/52/comments"] = [{**comment(77), "path": "main.py", "line": 3}]
    engine.reconcile(repo)
    assert len(gt.rows["pulls"]) == 1 and len(gt.rows["issues"]) == 1
    assert "archives a historical PR" in gt.rows["issues"][0]["body"]
    gh.rows["pulls"][0].update(state="closed", merged=True)
    engine.reconcile(repo)
    target = gt.rows["pulls"][0]
    assert target["state"] == "closed" and "**merged**" in target["body"]
    assert not any(path.endswith("/merge") for _, path, _ in gt.calls)
    assert len(gt.comment_rows[f"/repos/gt/project/pulls/{target['number']}/comments"]) == 2


def test_pending_open_pr_recovers_as_native_after_source_merge(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["pulls"] = [pull()]
    gt.lose_next_post = True
    with pytest.raises(RuntimeError):
        engine.reconcile(repo)
    gh.rows["pulls"][0].update(state="closed", merged=True)
    engine.reconcile(repo)
    assert len(gt.rows["pulls"]) == 1 and not gt.rows["issues"]
    assert state.get(repo.key, "pull", 1052)["representation"] == "pulls"


def test_reverse_comments_no_loop_including_lost_response(setup):
    repo, cfg, state, gh, gt, engine = setup
    cfg.sync["reverse_comments"] = True
    gh.rows["issues"] = [issue()]
    engine.reconcile(repo)
    target_path = "/repos/gt/project/issues/I501/comments"
    gt.comment_rows[target_path] = [comment(77, "gitee")]
    gh.lose_next_post = True
    with pytest.raises(RuntimeError):
        engine.reconcile(repo)
    engine.reconcile(repo)
    engine.reconcile(repo)
    assert len(gt.comment_rows[target_path]) == 1
    assert len(gh.comment_rows["/repos/gh/project/issues/31/comments"]) == 1
    assert state.get(repo.key, "reverse_issue_comment", 77)["target"]


def test_token_owner_comments_sync_both_ways_without_echo(setup):
    repo, cfg, state, gh, gt, engine = setup
    cfg.sync["reverse_comments"] = True
    gh.rows["issues"] = [issue()]
    source_path = "/repos/gh/project/issues/31/comments"
    gh.comment_rows[source_path] = [{**comment(), "user": {"id": gh.bot_id, "login": "gh"}}]
    engine.reconcile(repo)
    target_path = "/repos/gt/project/issues/I501/comments"
    gt.comment_rows[target_path].append({**comment(900, "gitee"), "user": {"id": gt.bot_id, "login": "gt"}})
    gh.lose_next_post = True
    with pytest.raises(RuntimeError, match="response lost"):
        engine.reconcile(repo)
    engine.reconcile(repo)
    engine.reconcile(repo)
    assert len(gh.comment_rows[source_path]) == 2
    assert len(gt.comment_rows[target_path]) == 2
    assert state.get(repo.key, "reverse_issue_comment", 900)["target"]


def test_gitee_label_constraints_and_assignment(setup):
    repo, cfg, state, gh, gt, engine = setup
    label = {"id": 123, "name": "good first issue", "color": "7057ff"}
    gh.rows["labels"] = [label]
    gh.rows["issues"] = [{**issue(), "labels": [label]}]
    engine.reconcile(repo)
    mapped = state.get(repo.key, "label", label["name"])["target"]
    assert " " not in mapped and len(mapped) <= 20
    assert gt.rows["issues"][0]["labels"] == [{"name": mapped}]
    label["color"] = "123456"
    engine.reconcile(repo)
    assert len(gt.rows["labels"]) == 1
    assert gt.rows["labels"][0]["color"] == "123456"


def test_webhook_duplicate_auth_and_durable_job(setup):
    repo, cfg, state, gh, gt, engine = setup
    client = create_app(cfg, state, "secret").test_client()
    raw = json.dumps({"repository": {"full_name": "gh/project"}}).encode()
    signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues",
               "X-GitHub-Delivery": "abc", "X-Hub-Signature-256": signature}
    assert client.post("/webhooks/github", data=raw, headers={}).status_code == 401
    assert client.post("/webhooks/github", data=raw, headers=headers).json == {"queued": True}
    assert client.post("/webhooks/github", data=raw, headers=headers).json == {"queued": False}
    assert len(State(cfg.state_dir).jobs()) == 1
    job = state.jobs()[0]
    state.enqueue(repo.key)  # webhook arriving while reconciliation is running must not be lost
    state.finish(job)
    assert len(state.jobs()) == 1
    assert client.post("/webhooks/gitee", json={"password": "secret"}).status_code == 404


def test_worker_lock_and_transaction_rollback(tmp_path):
    state = State(tmp_path)
    with state.worker_lock():
        with pytest.raises(RuntimeError, match="Another worker"):
            with State(tmp_path).worker_lock():
                pass
    with pytest.raises(ValueError):
        with state.connect() as db:
            db.execute("INSERT INTO jobs(repo) VALUES('rollback')")
            raise ValueError()
    assert not state.jobs()


def test_failed_job_retried_and_reported(setup):
    repo, cfg, state, gh, gt, engine = setup
    state.enqueue(repo.key)
    engine.reconcile = Mock(side_effect=RuntimeError("failure"))
    assert run_jobs(cfg, state, engine) == 1
    assert not state.jobs()
    assert state.status()["jobs"][0]["error"] == "failure"
    assert state.status()["jobs"][0]["attempts"] == 1


def test_api_does_not_retry_posts_or_accept_partial_pages(monkeypatch):
    api = API("gitee", "not-a-real-token")
    api.session.request = Mock(side_effect=requests.Timeout())
    with pytest.raises(RuntimeError, match="network failure"):
        api.request("POST", "/repos/gt/issues", data={})
    assert api.session.request.call_count == 1
    api.request = Mock(side_effect=[[{"id": i} for i in range(100)], APIError("gitee", "GET", "/x", 500)])
    with pytest.raises(APIError):
        api.list("/x")


def test_git_real_branches_tags_force_update_and_target_only_ref(tmp_path):
    def git(*args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
    src, dst = tmp_path / "source", tmp_path / "target.git"
    git("init", "-b", "main", str(src))
    git("init", "--bare", str(dst))
    git("config", "user.email", "test@example.invalid", cwd=src)
    git("config", "user.name", "Bridge test", cwd=src)
    (src / "a.txt").write_text("hello")
    git("add", ".", cwd=src)
    git("commit", "-m", "initial", cwd=src)
    git("branch", "feature", cwd=src)
    git("tag", "-a", "v1", "-m", "release", cwd=src)
    assert mirror_sync(str(src), str(dst), "test", "", "") == "success"
    assert git("show-ref", cwd=src) == git("show-ref", cwd=dst)
    git("branch", "gitee-only", "main", cwd=dst)
    git("commit", "--amend", "-m", "rewritten", cwd=src)
    assert mirror_sync(str(src), str(dst), "test", "", "") == "success"
    assert git("rev-parse", "main", cwd=src) == git("rev-parse", "main", cwd=dst)
    assert git("rev-parse", "gitee-only", cwd=dst)


def test_gitee_password_hook_and_body_limit(setup):
    repo, cfg, state, gh, gt, engine = setup
    cfg.sync["reverse_comments"] = True
    client = create_app(cfg, state, "gh-secret", "gt-secret").test_client()
    event = {"password": "gt-secret", "repository": {"full_name": "gt/project"}}
    headers = {"X-Gitee-Event": "Note Hook"}
    assert client.post("/webhooks/gitee", json=event, headers=headers).status_code == 202
    assert client.post("/webhooks/gitee", json=event, headers=headers).json == {"queued": False}
    event["password"] = "wrong"
    assert client.post("/webhooks/gitee", json=event, headers=headers).status_code == 401
    assert client.post("/webhooks/github", data=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413


def test_native_pr_ref_is_verified_before_push(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return Mock(returncode=0, stdout="wrong-sha" if args[1] == "rev-parse" else "")
    monkeypatch.setattr("bridge.git.subprocess.run", run)
    with pytest.raises(RuntimeError, match="head changed"):
        GitSync("gh-token", "gt-token", "bot").pull_head(Repository("a/b", "c/d"), pull())
    assert not any(command[1] == "push" for command in calls)
    assert any("+refs/pull/52/head:refs/heads/bridge/pr/52/head" in command for command in calls)


def test_target_deleted_does_not_create_again(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["issues"] = [issue()]
    engine.reconcile(repo)
    gt.rows["issues"].clear()
    with pytest.raises(RuntimeError, match="disappeared"):
        engine.reconcile(repo)
    assert not gt.rows["issues"]


def test_api_repeated_page_and_http_error(monkeypatch):
    api = API("github", "token")
    api.request = Mock(return_value=[{"id": i} for i in range(100)])
    with pytest.raises(RuntimeError, match="repeated pagination"):
        api.list("/items")
    api = API("github", "token")
    response = Mock(status_code=403, headers={})
    api.session.request = Mock(return_value=response)
    with pytest.raises(APIError, match="HTTP 403"):
        api.request("GET", "/items")


def test_config_rejects_ambiguous_mapping_and_unknown_flags(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('direction="both"\n[[repositories]]\ngithub="a/b"\ngitee="c/d"\n')
    with pytest.raises(ValueError):
        Config.load(path)
    path.write_text('[sync]\nisssues=true\n[[repositories]]\ngithub="a/b"\ngitee="c/d"\n')
    with pytest.raises(ValueError, match="Unknown feature"):
        Config.load(path)


def test_env_file_is_literal_and_errors_do_not_expose_values(tmp_path):
    env = tmp_path / "secrets.env"
    sentinel = tmp_path / "must-not-exist"
    env.write_text(f'# comment\nGITHUB_TOKEN="$(touch {sentinel})"\nGITEE_TOKEN=literal#token\n')
    values = load_env_file(env)
    assert values["GITHUB_TOKEN"] == f"$(touch {sentinel})"
    assert values["GITEE_TOKEN"] == "literal#token"
    assert not sentinel.exists()
    env.write_text('UNEXPECTED=do-not-leak-this\n')
    with pytest.raises(ValueError) as error:
        load_env_file(env)
    assert 'do-not-leak-this' not in str(error.value)


def test_default_branch_patch_preserves_required_target_name(setup):
    repo, cfg, state, gh, gt, engine = setup
    cfg.sync["git"] = True
    engine.git.mirror.return_value = "success"
    source_request, target_request = gh.request, gt.request
    current = {"private": False, "name": "Target display name", "default_branch": "master"}
    updates = []

    def source(method, path, **kwargs):
        if method == "GET" and path == "/repos/gh/project":
            return {"private": False, "default_branch": "main", "size": 0}
        return source_request(method, path, **kwargs)

    def target(method, path, **kwargs):
        if path == "/repos/gt/project":
            if method == "PATCH":
                assert kwargs["data"]["name"] == "Target display name"
                updates.append(kwargs["data"])
                current.update(kwargs["data"])
            return dict(current)
        return target_request(method, path, **kwargs)

    gh.request, gt.request = source, target
    engine.reconcile(repo)
    engine.reconcile(repo)
    assert updates == [{"name": "Target display name", "default_branch": "main"}]


def test_milestones_require_due_date_and_labels_are_updated(setup):
    repo, cfg, state, gh, gt, engine = setup
    cfg.sync["milestones"] = True
    gh.rows["labels"] = [{"id": 1, "name": "bug", "color": "ff0000"}]
    gh.rows["milestones"] = [
        {"number": 1, "title": "undated", "due_on": None},
        {"number": 2, "title": "release", "due_on": "2026-12-31T00:00:00Z", "state": "open"},
    ]
    engine.reconcile(repo)
    engine.reconcile(repo)
    assert len(gt.rows["milestones"]) == 1
    assert state.get(repo.key, "milestone", 2)["target"]
    assert len(gt.rows["labels"]) == 1


def test_pr_retarget_is_explained_without_unsupported_patch(setup):
    repo, cfg, state, gh, gt, engine = setup
    gh.rows["pulls"] = [pull()]
    engine.reconcile(repo)
    gh.rows["pulls"][0]["base"]["ref"] = "release"
    engine.reconcile(repo)
    assert "Canonical base changed" in gt.rows["pulls"][0]["body"]
    assert all("base" not in data for method, _, data in gt.calls if method == "PATCH")


def test_private_source_public_target_fails_before_git_or_metadata(setup):
    repo, cfg, state, gh, gt, engine = setup
    old = gh.request
    gh.request = lambda method, path, **kw: ({"private": True} if path == "/repos/gh/project"
                                           else old(method, path, **kw))
    with pytest.raises(RuntimeError, match="Private source"):
        engine.reconcile(repo)
    assert not any(method in {"POST", "PATCH", "PUT"} for method, _, _ in gt.calls)


def test_pr_ref_materialization_with_real_git(tmp_path, monkeypatch):
    def git(*args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
    source, target = tmp_path / "source", tmp_path / "target.git"
    git("init", "-b", "main", str(source))
    git("init", "--bare", str(target))
    git("config", "user.email", "test@example.invalid", cwd=source)
    git("config", "user.name", "Bridge test", cwd=source)
    git("commit", "--allow-empty", "-m", "fork head", cwd=source)
    sha = git("rev-parse", "HEAD", cwd=source)
    git("update-ref", "refs/pull/52/head", sha, cwd=source)
    # Rewrite only this test's public URL strings to local repositories; no network or credentials.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/gh/project.git")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{target.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "https://gitee.com/gt/project.git")
    obj = pull()
    obj["head"]["sha"] = sha
    branch = GitSync("", "", "bot").pull_head(Repository("gh/project", "gt/project"), obj)
    assert branch == "bridge/pr/52/head"
    assert git("rev-parse", branch, cwd=target) == sha
