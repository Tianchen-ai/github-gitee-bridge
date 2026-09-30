"""Reuse upstream mirroring; add only GitHub PR ref materialization."""
import os
import subprocess
import tempfile

from lib.sync_repo import mirror_sync
from lib.utils import make_git_env


class GitSync:
    def __init__(self, github_token, gitee_token, gitee_user):
        self.github_token = github_token
        self.gitee_token = gitee_token
        self.gitee_user = gitee_user

    def mirror(self, repo):
        status = mirror_sync(
            f"https://github.com/{repo.github}.git", f"https://gitee.com/{repo.gitee}.git",
            repo.github, self.github_token, self.gitee_token, target_username=self.gitee_user)
        if status not in {"success", "empty"}:
            raise RuntimeError(f"Git mirror failed: {repo.github}")

    def pull_head(self, repo, pull):
        """Fetch base repository's PR ref (also works for forks), verify SHA, then push."""
        number = int(pull["number"])
        branch = f"bridge/pr/{number}/head"
        credentials = []
        try:
            src_env, src_file = make_git_env(self.github_token)
            credentials.append(src_file)
            dst_env, dst_file = make_git_env(self.gitee_token, self.gitee_user)
            credentials.append(dst_file)
            with tempfile.TemporaryDirectory(prefix="bridge-pr-") as directory:
                def run(args, env=src_env):
                    result = subprocess.run(["git", *args], cwd=directory, env=env,
                                            capture_output=True, text=True, timeout=900)
                    if result.returncode:
                        raise RuntimeError(f"PR #{number}: git {args[0]} failed")
                    return result.stdout.strip()
                run(["init", "--bare"])
                run(["fetch", "--no-tags", f"https://github.com/{repo.github}.git",
                     f"+refs/pull/{number}/head:refs/heads/{branch}"])
                sha = run(["rev-parse", f"refs/heads/{branch}"])
                if sha != pull["head"]["sha"]:
                    raise RuntimeError(f"PR #{number}: head changed during sync; retry with fresh API state")
                run(["push", "--force", f"https://gitee.com/{repo.gitee}.git",
                     f"refs/heads/{branch}:refs/heads/{branch}"], dst_env)
        finally:
            for path in credentials:
                os.unlink(path)
        return branch
