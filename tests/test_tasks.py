"""Unit tests for the job's status callback (spec 4.2)."""

# pytest idioms: fixtures as arguments (also for side effects only), self-describing names
# pylint: disable=redefined-outer-name,unused-argument,missing-function-docstring

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from ckanext.aiextract import tasks


@pytest.fixture
def gateway_timeout_server():
    """Local HTTP server answering every POST with 504; records the request paths."""
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        """Always answer 504 Gateway Time-out."""

        def do_POST(self):  # pylint: disable=invalid-name
            """Record the request and answer 504."""
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            requests_seen.append(self.path)
            self.send_response(504)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):  # pylint: disable=redefined-builtin
            """Silence request logging."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests_seen
    finally:
        server.shutdown()
        server.server_close()


def test_callback_uses_short_timeout(monkeypatch):
    captured = {}
    real_factory = tasks.create_callback_session

    def factory():
        session = real_factory()

        def post(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return type("Response", (), {"status_code": 200})()

        session.post = post
        captured["session"] = session
        return session

    monkeypatch.setattr(tasks, "create_callback_session", factory)

    url = "http://ckan:5000/api/3/action/aiextract_hook"
    assert tasks.callback_aiextract_hook(url, "", {"status": "running"}) is True

    assert captured["url"] == url
    assert captured["kwargs"]["timeout"] == tasks.CALLBACK_TIMEOUT
    assert tasks.CALLBACK_TIMEOUT <= 30


def test_callback_session_does_not_retry_server_errors():
    retries = tasks.create_callback_session().get_adapter("http://ckan:5000").max_retries

    for status in (500, 502, 503, 504):
        for method in ("POST", "GET"):
            assert not retries.is_retry(method, status)
            assert not retries.is_retry(method, status, has_retry_after=True)
    assert retries.read == 0
    assert retries.status == 0


def test_callback_gateway_timeout_is_not_retried(gateway_timeout_server, monkeypatch):
    base_url, requests_seen = gateway_timeout_server
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    result = tasks.callback_aiextract_hook(
        f"{base_url}/api/3/action/aiextract_hook", "", {"status": "running"}
    )

    assert result is False
    assert requests_seen == ["/api/3/action/aiextract_hook"]


def test_update_job_status_swallows_callback_errors(monkeypatch):
    def failing_callback(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "callback_aiextract_hook", failing_callback)
    job_dict = {"metadata": {}, "status": "pending", "job_info": {}}

    tasks.update_job_status("http://ckan:5000/x", job_dict, "running")

    assert job_dict["status"] == "running"


def test_non_callback_session_keeps_retries():
    retries = tasks.create_session_with_timeout().get_adapter("http://ckan:5000").max_retries

    assert retries.total == tasks.RETRY_TOTAL
    assert 504 in retries.status_forcelist


def test_update_reports_error_if_failing_before_job_logger_exists(monkeypatch):
    """An error before the job logger is set up must not raise UnboundLocalError."""
    statuses = []

    def failing_init():
        raise RuntimeError("database unavailable")

    job = SimpleNamespace(id="job-1")
    monkeypatch.setattr(tasks, "get_current_job", lambda: job)
    monkeypatch.setattr(tasks.db, "init", failing_init)
    monkeypatch.setattr(
        tasks, "update_job_status", lambda url, job_dict, status, *a: statuses.append(status)
    )

    result = tasks.update(
        "my-dataset", "pkg-1", "res-1", "http://ckan/api/3/action/aiextract_hook", "now"
    )

    assert result == "error"
    assert statuses == ["error"]
