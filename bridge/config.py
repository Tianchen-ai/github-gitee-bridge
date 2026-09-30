"""Small, strict TOML configuration; credentials stay in the environment."""
import os
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Repository:
    github: str
    gitee: str
    gitee_account_type: str = "user"
    allow_public_target: bool = False

    @property
    def key(self):
        return f"{self.github}=>{self.gitee}"


@dataclass
class Config:
    repositories: list[Repository]
    state_dir: Path = Path("./state")
    interval: int = 300
    sync: dict = field(default_factory=lambda: {
        "git": True, "issues": True, "issue_comments": True,
        "pull_requests": True, "pr_comments": True, "review_comments": True,
        "labels": True, "milestones": True, "reverse_comments": False,
    })

    @classmethod
    def load(cls, path):
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
        unknown = set(raw) - {"repositories", "state_dir", "interval", "sync", "direction"}
        if unknown or raw.get("direction", "github2gitee") != "github2gitee":
            raise ValueError("Only direction=github2gitee is supported; unknown config: " + str(unknown))
        repos = [Repository(**r) for r in raw.get("repositories", [])]
        if not repos:
            raise ValueError("Configure at least one repository mapping")
        for repo in repos:
            for name in (repo.github, repo.gitee):
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or any(
                    part in {".", ".."} for part in name.split("/")
                ):
                    raise ValueError("Invalid owner/repository: " + name)
            if repo.gitee_account_type not in {"user", "org"}:
                raise ValueError("gitee_account_type must be user or org")
            if type(repo.allow_public_target) is not bool:
                raise ValueError("allow_public_target must be boolean")
        for platform in ("github", "gitee"):
            if len({getattr(r, platform).lower() for r in repos}) != len(repos):
                raise ValueError("Repository mappings must be one-to-one")
        config = cls(repos, Path(os.environ.get("BRIDGE_STATE_DIR", raw.get("state_dir", "./state"))),
                     raw.get("interval", 300))
        if type(config.interval) is not int or config.interval < 10:
            raise ValueError("interval must be an integer >= 10 seconds")
        for name, value in raw.get("sync", {}).items():
            if name not in config.sync or type(value) is not bool:
                raise ValueError("Unknown feature or non-boolean value: " + name)
            config.sync[name] = value
        return config
