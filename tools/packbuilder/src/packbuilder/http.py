"""Bounded public-source downloads; GitHub credentials are sent only to its API."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from packbuilder.files import write_bytes


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        redirected = super().redirect_request(request, fp, code, msg, headers, url)
        if redirected is not None and urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(request.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


class Fetcher:
    def __init__(self, cache: pathlib.Path):
        self.cache = cache
        self.requests: list[str] = []
        self.opener = urllib.request.build_opener(_Redirects())

    def invalidate(self, url: str) -> None:
        (self.cache / hashlib.sha256(url.encode()).hexdigest()).unlink(missing_ok=True)

    def get(self, url: str, *, immutable: bool = False) -> bytes:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in {"api.github.com", "raw.githubusercontent.com", "enka.network", "play.google.com", "play-lh.googleusercontent.com"}:
            raise FetchError(f"Unsupported source address: {url}")
        path = self.cache / hashlib.sha256(url.encode()).hexdigest()
        if immutable and path.is_file():
            return path.read_bytes()
        headers = {"User-Agent": "endfield-packbuilder/0.1.0", "Accept": "application/vnd.github+json" if parts.hostname == "api.github.com" else "*/*"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token and parts.hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(3):
            self.requests.append(url)
            try:
                with self.opener.open(urllib.request.Request(url, headers=headers), timeout=25) as response:
                    data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise FetchError(f"{url}: response exceeds 32 MiB")
                write_bytes(path, data)
                return data
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise FetchError(f"{url}: HTTP {error.code}", error.code) from error
            except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
                if attempt == 2:
                    raise FetchError(f"{url}: {error}") from error
            time.sleep(attempt + 1)
        raise AssertionError("unreachable")

    def json(self, url: str, *, immutable: bool = False):
        try:
            return json.loads(self.get(url, immutable=immutable).decode("utf-8-sig"))
        except (UnicodeError, ValueError) as error:
            self.invalidate(url)
            raise FetchError(f"{url}: invalid JSON") from error
