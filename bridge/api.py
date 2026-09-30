"""Strict HTTP adapter. Never retry a possibly successful write."""
import time
from urllib.parse import quote

import requests

from lib.utils import github_headers, gitee_headers


class APIError(RuntimeError):
    def __init__(self, platform, method, path, status):
        self.status = status
        super().__init__(f"{platform} {method} {path}: HTTP {status}")


class API:
    def __init__(self, platform, token):
        self.platform = platform
        self.token = token
        self.base = "https://api.github.com" if platform == "github" else "https://gitee.com/api/v5"
        self.session = requests.Session()
        self.session.headers.update(github_headers(token) if platform == "github" else gitee_headers(token))
        self.session.headers["User-Agent"] = "github-gitee-bridge/0.1"

    def request(self, method, path, *, params=None, data=None):
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, self.base + path, params=params,
                                                json=data, timeout=(10, 60), allow_redirects=False)
            except requests.RequestException:
                if attempt + 1 == attempts:
                    raise RuntimeError(f"{self.platform} {method} {path}: network failure") from None
                time.sleep(2 ** attempt)
                continue
            if 200 <= response.status_code < 300:
                return response.json() if response.content else None
            transient = response.status_code in {429, 500, 502, 503, 504} or (
                response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0")
            if transient and attempt + 1 < attempts:
                try:
                    delay = max(1, float(response.headers.get("Retry-After", 2 ** attempt)))
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(min(delay, 60))
                continue
            raise APIError(self.platform, method, path, response.status_code)

    def list(self, path, **params):
        result, seen = [], set()
        for page in range(1, 10001):
            rows = self.request("GET", path, params={**params, "per_page": 100, "page": page})
            if not isinstance(rows, list):
                raise RuntimeError(f"{self.platform}: expected complete list at {path}")
            if not rows:
                return result
            identities = tuple(str(row.get("id", row.get("name", row.get("number")))) for row in rows)
            if identities in seen:
                raise RuntimeError(f"{self.platform}: repeated pagination at {path}")
            seen.add(identities)
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise RuntimeError("Pagination limit reached; refusing partial synchronization")


def segment(value):
    return quote(str(value), safe="")
