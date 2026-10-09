"""Reconcile current source state, independently of webhook ordering."""
import hashlib
import logging
import re

from .api import APIError, segment


class UncertainWrite(RuntimeError):
    pass


def marker(repo, kind, source):
    key = hashlib.sha256(f"{repo}\0{kind}\0{source}".encode()).hexdigest()
    return f"<!-- bridge:{key} -->"


def gitee_label_name(name):
    """Gitee rejects spaces and names outside 2–20 characters; retain source names in bodies."""
    if re.fullmatch(r"[A-Za-z0-9_./\\\-\u4e00-\u9fff]{2,20}", name):
        return name
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-")[:9] or "label"
    return stem + "-" + hashlib.sha256(name.encode()).hexdigest()[:10]


def attributed(obj, platform, extra=""):
    user = (obj.get("user") or {}).get("login", "unknown")
    url = obj.get("html_url", "")
    return f"{user} on {platform}:\n\n{obj.get('body') or ''}\n\n---\nOriginal: {url}\n{extra}"


class Engine:
    def __init__(self, config, state, github, gitee, git):
        self.config, self.state = config, state
        self.github, self.gitee, self.git = github, gitee, git
        self.users = {}

    def bot(self, api):
        if api.platform not in self.users:
            self.users[api.platform] = api.request("GET", "/user")
        return self.users[api.platform]

    def upsert(self, repo, kind, source, api, rows, create, update, payload,
               representation, identity="number", body_field="body"):
        """Durable create intent + bot-owned recovery marker closes the lost-response window.

        A pending POST with no visible result is deliberately NOT retried automatically.
        Remote APIs do not offer idempotency keys: choosing availability here would duplicate.
        """
        token = marker(repo.key, kind, source)
        payload = {**payload, body_field: payload.get(body_field, "") + "\n\n" + token}
        mapping = self.state.get(repo.key, kind, source)
        existing = None
        if mapping and mapping["target"]:
            existing = next((r for r in rows if str(r[identity]) == mapping["target"]), None)
            if existing is None:
                raise RuntimeError(f"Mapped {kind} {source} disappeared; refusing duplicate recreation")
        else:
            matches = [r for r in rows if token in (r.get(body_field) or "") and (
                (r.get("user") or {}).get("id") == self.bot(api)["id"]
                or body_field == "description")]
            if len(matches) > 1:
                raise RuntimeError(f"Multiple recovery markers for {kind} {source}; manual repair required")
            if matches:
                existing = matches[0]
                self.state.save(repo.key, kind, source, existing[identity], representation)
            elif mapping and mapping["pending"]:
                raise UncertainWrite(f"Uncertain {kind} {source}: inspect target, then use resolve command")
        if existing is None:
            self.state.begin(repo.key, kind, source)
            try:
                existing = create(payload)
            except APIError as exc:
                # Definitive rejection cannot have created the object. Timeouts/5xx stay pending.
                if exc.status in {400, 401, 403, 404, 405, 422, 429}:
                    self.state.clear_pending(repo.key, kind, source)
                raise
            if not isinstance(existing, dict) or identity not in existing:
                raise UncertainWrite(f"Invalid creation response for {kind} {source}")
            self.state.save(repo.key, kind, source, existing[identity], representation)
            rows.append(existing)
        # Compare against live target, so a manual target edit is reconciled too.
        if any(existing.get(k) != v for k, v in payload.items()):
            update(existing[identity], payload)
        return existing

    def ensure_repository(self, repo):
        source = self.github.request("GET", f"/repos/{repo.github}")
        try:
            target = self.gitee.request("GET", f"/repos/{repo.gitee}")
        except APIError as exc:
            if exc.status != 404:
                raise
            owner, name = repo.gitee.split("/")
            if repo.gitee_account_type == "user" and self.bot(self.gitee)["login"].lower() != owner.lower():
                raise RuntimeError("Personal target owner must match Gitee token owner")
            path = f"/orgs/{owner}/repos" if repo.gitee_account_type == "org" else "/user/repos"
            self.gitee.request("POST", path, data={
                "name": name, "path": name, "private": bool(source["private"]),
                "description": (source.get("description") or "")[:200], "auto_init": False,
                "has_issues": True})
            target = self.gitee.request("GET", f"/repos/{repo.gitee}")
        if source["private"] and target.get("private") is not True and not repo.allow_public_target:
            raise RuntimeError("Private source → public target requires allow_public_target=true")
        return source

    def reconcile(self, repo):
        source_repo = self.ensure_repository(repo)
        features = self.config.sync
        if features["git"]:
            if features["pull_requests"] and any(b["name"].startswith("bridge/pr/") for b in
                    self.github.list(f"/repos/{repo.github}/branches")):
                raise RuntimeError("Source uses reserved bridge/pr/ namespace; rename those branches first")
            git_status = self.git.mirror(repo)
            if git_status != "empty" and source_repo.get("default_branch"):
                target_repo = self.gitee.request("GET", f"/repos/{repo.gitee}")
                if target_repo.get("default_branch") != source_repo["default_branch"]:
                    # Gitee requires name on repository PATCH, even for a default-branch-only update.
                    self.gitee.request("PATCH", f"/repos/{repo.gitee}", data={
                        "name": target_repo["name"], "default_branch": source_repo["default_branch"]})
        errors = []

        def attempt(name, fn):
            try:
                fn()
            except Exception as exc:
                logging.error("%s: %s: %s", repo.key, name, exc)
                errors.append(f"{name}: {exc}")

        if features["labels"]:
            attempt("labels", lambda: self.labels(repo))
        if features["milestones"]:
            attempt("milestones", lambda: self.milestones(repo))
        if features["issues"]:
            source = self.github.list(f"/repos/{repo.github}/issues", state="all", sort="created", direction="asc")
            target = self.gitee.list(f"/repos/{repo.gitee}/issues", state="all")
            for issue in source:
                if "pull_request" not in issue:
                    attempt(f"issue {issue['number']}", lambda issue=issue: self.issue(repo, issue, target))
        if features["pull_requests"]:
            source = self.github.list(f"/repos/{repo.github}/pulls", state="all", sort="created", direction="asc")
            pulls = self.gitee.list(f"/repos/{repo.gitee}/pulls", state="all")
            archives = self.gitee.list(f"/repos/{repo.gitee}/issues", state="all")
            for pull in source:
                attempt(f"pull {pull['number']}", lambda pull=pull: self.pull(repo, pull, pulls, archives))
        if errors:
            raise RuntimeError("; ".join(errors))

    def labels(self, repo):
        path = f"/repos/{repo.gitee}/labels"
        target = {r["name"]: r for r in self.gitee.list(path)}
        for label in self.github.list(f"/repos/{repo.github}/labels"):
            name = gitee_label_name(label["name"])
            # Live Gitee currently requires bare hex, unlike the upstream helper's '#' prefix.
            data = {"name": name, "color": label["color"].lstrip("#")}
            prior = self.state.get(repo.key, "label", label["name"])
            if self.state.mapped_target(repo.key, "label", name) and not prior:
                raise RuntimeError(f"Label name mapping collision: {label['name']}")
            if name not in target:
                self.gitee.request("POST", path, data=data)
            elif target[name].get("color", "").lstrip("#") != data["color"]:
                self.gitee.request("PATCH", path + "/" + segment(name), data=data)
            self.state.save(repo.key, "label", label["name"], name, "label")

    def milestones(self, repo):
        path = f"/repos/{repo.gitee}/milestones"
        targets = self.gitee.list(path, state="all")
        for item in self.github.list(f"/repos/{repo.github}/milestones", state="all"):
            if not item.get("due_on"):
                # Gitee requires a deadline; don't invent a date.
                continue
            self.upsert(repo, "milestone", item["number"], self.gitee, targets,
                        lambda p: self.gitee.request("POST", path, data=p),
                        lambda n, p: self.gitee.request("PATCH", f"{path}/{n}", data=p),
                        {k: item.get(k) or "" for k in ("title", "description", "state", "due_on")},
                        "milestone", body_field="description")

    def metadata_note(self, obj):
        labels = ", ".join(x["name"] for x in obj.get("labels", []))
        milestone = obj.get("milestone")
        return f"Source labels: {labels or '(none)'}\nSource milestone: " + (
            f"{milestone['title']} ({milestone.get('html_url', '')})" if milestone else "(none)")

    def references(self, repo, obj):
        numbers = sorted(set(re.findall(r"(?<![\w/])#(\d+)\b", obj.get("body") or "")), key=int)
        if not numbers:
            return ""
        # Keep platform-native closing/autolinking rules out of the bridge: explicit source links.
        return "\nReferences in the original text use GitHub numbering: " + ", ".join(
            f"[#{n}](https://github.com/{repo.github}/issues/{n})" for n in numbers)

    def assign_metadata(self, repo, source, target, representation):
        root = f"/repos/{repo.gitee}"
        if self.config.sync["labels"]:
            desired = []
            for label in source.get("labels", []):
                mapping = self.state.get(repo.key, "label", label["name"])
                if not mapping:
                    raise RuntimeError(f"Label not synchronized: {label['name']}")
                desired.append(mapping["target"])
            current = [x["name"] for x in target.get("labels", [])]
            if sorted(current) != sorted(desired):
                self.gitee.request("PUT", f"{root}/{representation}/{target['number']}/labels", data=desired)
        if self.config.sync["milestones"]:
            milestone = source.get("milestone")
            mapping = self.state.get(repo.key, "milestone", milestone["number"]) if milestone else None
            desired = mapping["target"] if mapping and mapping["target"] else None
            current = (target.get("milestone") or {}).get("number")
            if desired and str(current) != desired:
                field = "milestone_number" if representation == "pulls" else "milestone"
                path = (f"{root}/pulls/{target['number']}" if representation == "pulls" else
                        f"/repos/{repo.gitee.split('/')[0]}/issues/{target['number']}")
                self.gitee.request("PATCH", path, data={field: int(desired), "repo": repo.gitee.split('/')[1]})

    def issue(self, repo, source, targets):
        owner, name = repo.gitee.split("/")
        path = f"/repos/{owner}/issues"
        payload = {"title": source["title"], "body": attributed(source, "GitHub", self.metadata_note(source) + self.references(repo, source))}
        target = self.upsert(repo, "issue", source["id"], self.gitee, targets,
            lambda p: self.gitee.request("POST", path, data={**p, "repo": name}),
            lambda n, p: self.gitee.request("PATCH", f"{path}/{n}", data={**p, "repo": name}),
            payload, "issues")
        self.state.describe(repo.key, "issue", source, target)
        if target["state"] != source["state"]:
            self.gitee.request("PATCH", f"{path}/{target['number']}",
                               data={"repo": name, "state": source["state"]})
        self.assign_metadata(repo, source, target, "issues")
        if self.config.sync["issue_comments"]:
            self.comments(repo, source, target, "issue", "issues")

    def pull(self, repo, summary, pulls, archives):
        source = self.github.request("GET", f"/repos/{repo.github}/pulls/{summary['number']}")
        mapping = self.state.get(repo.key, "pull", source["id"])
        # Closed historical PRs often have deleted heads or no diff. Preserve a discussion archive.
        representation = mapping["representation"] if mapping and mapping["target"] else (
            "issues" if source["state"] == "closed" else "pulls")
        # Recovery must search BOTH representations after a crash, even if source state changed.
        token = marker(repo.key, "pull", source["id"])
        if not mapping or not mapping["target"]:
            recovered = [kind for kind, rows in (("issues", archives), ("pulls", pulls))
                         if any(token in (x.get("body") or "") and
                                (x.get("user") or {}).get("id") == self.bot(self.gitee)["id"] for x in rows)]
            if len(recovered) > 1:
                raise RuntimeError("PR recovery marker exists in multiple representations")
            if recovered:
                representation = recovered[0]
        state = "merged" if source.get("merged") else source["state"]
        head = source["head"]
        base = source["base"]
        note = (f"Canonical PR state: **{state}**\n"
                f"Source branch: `{head.get('label', head['ref'])}` → `{base['ref']}`\n"
                f"Head SHA: `{head['sha']}`\nMerge SHA: `{source.get('merge_commit_sha') or ''}`\n"
                "Merge, review approval and CI are managed on GitHub.\n" + self.metadata_note(source) + self.references(repo, source))
        if representation == "issues":
            note += "\nThis Issue archives a historical PR; see Original for the code diff."
        root = f"/repos/{repo.gitee}"
        owner, name = repo.gitee.split("/")
        path = f"{root}/pulls" if representation == "pulls" else f"/repos/{owner}/issues"
        extra = {}
        if representation == "pulls":
            if self.config.sync["git"]:
                extra["head"] = self.git.pull_head(repo, source)
            else:
                extra["head"] = f"bridge/pr/{source['number']}/head"
                branch = self.gitee.request("GET", f"{root}/branches/{segment(extra['head'])}")
                if branch["commit"]["sha"] != head["sha"]:
                    raise RuntimeError("Externally mirrored PR head does not match GitHub head SHA")
            extra["base"] = base["ref"]
            native = next((p for p in pulls if mapping and str(p["number"]) == mapping["target"]), None)
            if native and (native.get("base") or {}).get("ref") != base["ref"]:
                note += "\nCanonical base changed. Gitee cannot retarget through its API; use Original for the current diff."
        if representation == "issues":
            extra["repo"] = name
        payload = {"title": source["title"], "body": attributed(source, "GitHub", note)}
        target = self.upsert(repo, "pull", source["id"], self.gitee,
            pulls if representation == "pulls" else archives,
            lambda p: self.gitee.request("POST", path, data={**p, **extra}),
            lambda n, p: self.gitee.request("PATCH", f"{path}/{n}",
                data={**p, **({"repo": name} if representation == "issues" else {})}),
            payload, representation)
        self.state.describe(repo.key, "pull", source, target)
        desired_state = "closed" if source["state"] == "closed" else "open"
        if target["state"] == "merged" and desired_state == "open":
            raise RuntimeError("Gitee PR was independently merged; cannot reopen. Resolve on GitHub")
        if target["state"] not in {desired_state, "merged"}:
            self.gitee.request("PATCH", f"{path}/{target['number']}",
                data={"state": desired_state, **({"repo": name} if representation == "issues" else {})})
        self.assign_metadata(repo, source, target, representation)
        if self.config.sync["pr_comments"]:
            self.comments(repo, source, target, "pull", representation)

    def comments(self, repo, source, target, kind, representation):
        gh_path = f"/repos/{repo.github}/issues/{source['number']}/comments"
        gt_path = f"/repos/{repo.gitee}/{representation}/{target['number']}/comments"
        gh_rows = self.github.list(gh_path)
        gt_rows = self.gitee.list(gt_path)
        comment_kind = f"{kind}_comment"
        reverse_markers = {marker(repo.key, f"reverse_{comment_kind}", c["id"]) for c in gt_rows}
        forward_markers = {marker(repo.key, comment_kind, c["id"]) for c in gh_rows}

        def replica(comment, api, known_markers):
            # Account identity alone is insufficient: users may also comment using the token owner.
            return (comment.get("user") or {}).get("id") == self.bot(api)["id"] and any(
                m in known_markers for m in re.findall(r"<!-- bridge:[0-9a-f]{64} -->", comment.get("body") or ""))

        for comment in gh_rows:
            if replica(comment, self.github, reverse_markers):
                continue
            if self.state.mapped_target(repo.key, f"reverse_{comment_kind}", comment["id"]):
                continue
            self.comment(repo, comment, comment_kind, self.gitee, gt_path, gt_rows, representation, "GitHub")
        if kind == "pull" and self.config.sync["review_comments"]:
            for comment in self.github.list(f"/repos/{repo.github}/pulls/{source['number']}/comments"):
                forward_markers.add(marker(repo.key, "review_comment", comment["id"]))
                comment = {**comment, "body": (comment.get("body") or "") +
                           f"\n\nCode discussion: `{comment.get('path', '')}` line "
                           f"{comment.get('line') or comment.get('original_line') or '?'} "
                           f"at `{comment.get('commit_id', '')}`. Reply context: "
                           f"{comment.get('in_reply_to_id') or '(root)'}"}
                self.comment(repo, comment, "review_comment", self.gitee, gt_path, gt_rows, representation, "GitHub")
        if self.config.sync["reverse_comments"]:
            for comment in gt_rows:
                if replica(comment, self.gitee, forward_markers) or any(
                    self.state.mapped_target(repo.key, k, comment["id"])
                    for k in (comment_kind, "review_comment")
                ):
                    continue
                self.comment(repo, comment, f"reverse_{comment_kind}", self.github,
                             gh_path, gh_rows, "issues", "Gitee")

    def comment(self, repo, source, kind, api, path, rows, representation, platform):
        edit_root = (f"/repos/{repo.gitee}/{representation}/comments" if api.platform == "gitee"
                     else f"/repos/{repo.github}/issues/comments")
        target = self.upsert(repo, kind, source["id"], api, rows,
            lambda p: api.request("POST", path, data=p),
            lambda n, p: api.request("PATCH", f"{edit_root}/{n}", data=p),
            {"body": attributed(source, platform)}, "comment", identity="id")
        self.state.describe(repo.key, kind, source, target)
