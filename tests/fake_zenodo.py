"""A local stand-in for the Zenodo deposit API.

Cluster 4 Part D2, built before the driver. A driver written against a server
that always says yes handles failure badly, and deposit failures are the ones
that leave a repository in an inconsistent state: a created-but-unpublished
record, or two records from one retry.

A fake rather than mocks, because the interesting behaviour is in HTTP status
handling and retry logic, and a mock asserts our assumptions instead of testing
them. It is also what lets a reviewer run the deposit path with no credentials.

**This encodes what we believe the API does.** Divergence between the fake and
the live sandbox is a finding to record, not a bug to paper over, and the live
test exists to look for it. Two have been found so far and are noted where they
apply:

  - a real deposition returns the uploaded `files`, which is how a resumed
    deposit knows what it has already sent. Omitting it made the driver
    re-upload;
  - the real API accepts incomplete metadata on a draft and validates on
    publish. The fake originally validated on every write, which made it
    stricter than the API and left the driver's publish-time error handling
    untested;
  - the real API refuses metadata writes to a *published* deposition, which must
    be reopened through `actions/edit` first. The fake permitted them, so a retry
    that replayed the whole sequence passed offline and failed against the
    sandbox.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

VALID_TOKEN = "fake-token-with-deposit-scope"


class FakeZenodoState:
    """Records what the server was asked to do, so tests can assert on it."""

    def __init__(self) -> None:
        self.depositions: dict[int, dict] = {}
        self.files: dict[int, dict[str, bytes]] = {}
        self.published: set[int] = set()
        self.next_id = 1000
        self.requests: list[tuple[str, str]] = []
        # Faults the test arms before exercising the driver.
        self.drop_next_upload = False
        self.fail_next_publish_with: int | None = None

    def reset_faults(self) -> None:
        self.drop_next_upload = False
        self.fail_next_publish_with = None


def _validate_metadata(metadata: dict) -> list[str]:
    """Zenodo's own required fields, which are not DataCite's.

    Kept explicit rather than derived from our schema profile: the point of the
    fake is to be an independent opinion about what is acceptable.
    """
    problems = []
    for field in ("title", "upload_type", "creators"):
        if not metadata.get(field):
            problems.append(f"{field} is required")
    for creator in metadata.get("creators") or []:
        if not creator.get("name"):
            problems.append("each creator requires a name")
    if metadata.get("access_right") == "embargoed" and not metadata.get("embargo_date"):
        problems.append("embargo_date is required when access_right is embargoed")
    return problems


class _Handler(BaseHTTPRequestHandler):
    state: FakeZenodoState

    def log_message(self, *args):  # noqa: D102 - silence the default logging
        pass

    # -- helpers ----------------------------------------------------------

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()
        if token == VALID_TOKEN:
            return True
        self._json(401, {"message": "Permission denied", "status": 401})
        return False

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _deposition(self, dep_id: int) -> dict:
        base = f"http://{self.headers.get('Host')}"
        published = dep_id in self.state.published
        payload = {
            "id": dep_id,
            "record_id": dep_id,
            "conceptrecid": str(9000 + dep_id),
            "state": "done" if published else "unsubmitted",
            "submitted": published,
            # Observed from the sandbox: a draft carries no doi or conceptdoi at
            # all. The fake returned them as null, which a caller checking key
            # presence rather than value would have read as a published record.
            "created": "2026-01-01T00:00:00.000000+00:00",
            "modified": "2026-01-01T00:00:00.000000+00:00",
            "owner": 1,
            "title": self.state.depositions.get(dep_id, {}).get("title", ""),
            "metadata": self.state.depositions.get(dep_id, {}),
            # Real Zenodo returns the uploaded files on the deposition, which is
            # how a resumed deposit knows what it has already sent. Omitting it
            # here made the driver re-upload; the fake was wrong, not the driver.
            "files": [{"filename": name, "key": name, "filesize": len(data)}
                      for name, data in sorted(
                          self.state.files.get(dep_id, {}).items())],
            "links": {
                "self": f"{base}/api/deposit/depositions/{dep_id}",
                "bucket": f"{base}/api/files/bucket-{dep_id}",
                "files": f"{base}/api/deposit/depositions/{dep_id}/files",
                "publish": f"{base}/api/deposit/depositions/{dep_id}/actions/publish",
                "edit": f"{base}/api/deposit/depositions/{dep_id}/actions/edit",
                "discard": f"{base}/api/deposit/depositions/{dep_id}/actions/discard",
                "newversion": f"{base}/api/deposit/depositions/{dep_id}/actions/newversion",
                "latest_draft": f"{base}/api/deposit/depositions/{dep_id}",
                "latest_draft_html": f"{base}/deposit/{dep_id}",
                "badge": f"{base}/badge/doi/{dep_id}.svg",
                "html": f"{base}/record/{dep_id}",
            },
        }
        if published:
            payload["doi"] = f"10.5072/zenodo.{dep_id}"
            payload["conceptdoi"] = f"10.5072/zenodo.{9000 + dep_id}"
        return payload

    # -- routes -----------------------------------------------------------

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        self.state.requests.append(("POST", path))
        if not self._authorised():
            return

        if path == "/api/deposit/depositions":
            dep_id = self.state.next_id
            self.state.next_id += 1
            # A draft accepts whatever it is given. Validation happens on
            # publish, which is what the sandbox does and what the fake
            # originally got wrong.
            self.state.depositions[dep_id] = self._body().get("metadata", {})
            self.state.files[dep_id] = {}
            return self._json(201, self._deposition(dep_id))

        if path.endswith("/actions/publish"):
            dep_id = int(path.split("/")[-3])
            if dep_id not in self.state.depositions:
                return self._json(404, {"message": "Not found", "status": 404})
            if self.state.fail_next_publish_with is not None:
                status = self.state.fail_next_publish_with
                self.state.fail_next_publish_with = None
                return self._json(status, {"message": "Refused", "status": status})
            if dep_id in self.state.published:
                return self._json(409, {"message": "Already published",
                                        "status": 409})
            problems = _validate_metadata(self.state.depositions[dep_id])
            if problems:
                return self._json(400, {"message": "Validation error",
                                        "status": 400,
                                        "errors": [{"field": "metadata",
                                                    "message": p}
                                                   for p in problems]})
            if not self.state.files.get(dep_id):
                return self._json(400, {"message": "Deposition has no files",
                                        "status": 400})
            self.state.published.add(dep_id)
            return self._json(202, self._deposition(dep_id))

        if path.endswith("/actions/newversion"):
            dep_id = int(path.split("/")[-3])
            if dep_id not in self.state.published:
                return self._json(403, {"message": "Only published depositions "
                                                   "can have new versions",
                                        "status": 403})
            new_id = self.state.next_id
            self.state.next_id += 1
            self.state.depositions[new_id] = dict(self.state.depositions[dep_id])
            self.state.files[new_id] = {}
            payload = self._deposition(dep_id)
            payload["links"]["latest_draft"] = (
                f"http://{self.headers.get('Host')}/api/deposit/depositions/{new_id}")
            return self._json(201, payload)

        return self._json(404, {"message": "Not found", "status": 404})

    def do_PUT(self):  # noqa: N802
        path = urlparse(self.path).path
        self.state.requests.append(("PUT", path))
        if not self._authorised():
            return

        if path.startswith("/api/files/bucket-"):
            dep_id = int(path.split("/")[3].removeprefix("bucket-").split("/")[0])
            filename = path.split("/")[-1]
            length = int(self.headers.get("Content-Length") or 0)
            if self.state.drop_next_upload:
                self.state.drop_next_upload = False
                # Close without responding: the client sees a broken connection
                # part-way through, which is the failure retry logic must survive.
                self.close_connection = True
                return
            data = self.rfile.read(length)
            self.state.files.setdefault(dep_id, {})[filename] = data
            return self._json(201, {"key": filename, "size": len(data),
                                    "checksum": f"md5:{len(data)}"})

        if path.startswith("/api/deposit/depositions/"):
            dep_id = int(path.rstrip("/").split("/")[-1])
            if dep_id not in self.state.depositions:
                return self._json(404, {"message": "Not found", "status": 404})
            if dep_id in self.state.published:
                # Observed: the real API refuses metadata writes to a published
                # record, which must be reopened through actions/edit first. The
                # fake permitted them, so a retry that replayed the sequence
                # passed offline and failed against the sandbox.
                return self._json(404, {"message": "Not found.", "status": 404})
            # As with creation: drafts are not validated. See the module note.
            self.state.depositions[dep_id] = self._body().get("metadata", {})
            return self._json(200, self._deposition(dep_id))

        return self._json(404, {"message": "Not found", "status": 404})

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        self.state.requests.append(("GET", path))
        if not self._authorised():
            return
        if path.startswith("/api/deposit/depositions/"):
            dep_id = int(path.rstrip("/").split("/")[-1])
            if dep_id not in self.state.depositions:
                return self._json(404, {"message": "Not found", "status": 404})
            return self._json(200, self._deposition(dep_id))
        return self._json(404, {"message": "Not found", "status": 404})


class FakeZenodo:
    """Context manager running the fake on a free port."""

    def __init__(self) -> None:
        self.state = FakeZenodoState()
        handler = type("_Bound", (_Handler,), {"state": self.state})
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeZenodo":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
