"""Unit tests for the internal CKAN base URL handling in ckanext.aiextract.backend (4.2)."""

# pytest idioms: fixtures as arguments (also for side effects only), self-describing names
# pylint: disable=redefined-outer-name,unused-argument,missing-function-docstring

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from ckan.plugins import toolkit

from ckanext.aiextract import backend

SITE_URL = "https://data.example.org"
INTERNAL_URL = "http://ckan:5000"
DOWNLOAD_PATH = "/dataset/pkg-1/resource/res-1/download/paper.pdf"


@pytest.fixture
def internal_base_url(site_config, monkeypatch):
    """Configure ``ckanext.aiextract.callback_base_url`` to the internal CKAN URL."""
    monkeypatch.setitem(toolkit.config, "ckanext.aiextract.callback_base_url", INTERNAL_URL + "/")


def test_base_url_defaults_to_site_url(site_config):
    assert backend.get_ckan_base_url() == SITE_URL


@pytest.mark.usefixtures("internal_base_url")
def test_base_url_uses_callback_base_url():
    assert backend.get_ckan_base_url() == INTERNAL_URL


@pytest.mark.usefixtures("internal_base_url")
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SITE_URL + DOWNLOAD_PATH, INTERNAL_URL + DOWNLOAD_PATH),
        ("https://other.example.org/paper.pdf", "https://other.example.org/paper.pdf"),
        (
            "https://data.example.org.evil.org/paper.pdf",
            "https://data.example.org.evil.org/paper.pdf",
        ),
        ("", ""),
    ],
)
def test_to_internal_url(url, expected):
    assert backend.to_internal_url(url) == expected


def test_to_internal_url_unchanged_without_callback_base_url(site_config):
    assert backend.to_internal_url(SITE_URL + DOWNLOAD_PATH) == SITE_URL + DOWNLOAD_PATH


@pytest.mark.usefixtures("internal_base_url")
def test_extract_text_downloads_via_internal_url(monkeypatch):
    calls = []
    response = SimpleNamespace(content=b"%PDF", raise_for_status=lambda: None)

    class FakeSession:
        """Records GET/POST URLs."""

        def get(self, url, **kwargs):
            """Record a download."""
            calls.append(("GET", url))
            return response

        def post(self, url, **kwargs):
            """Record the extraction request."""
            calls.append(("POST", url))
            return response

    @contextmanager
    def managed_session():
        yield FakeSession()

    monkeypatch.setattr(backend, "managed_session", managed_session)
    resource = {"url": SITE_URL + DOWNLOAD_PATH, "name": "paper.pdf"}

    backend.extract_text(resource, "https://extract.example.org/api", timeout=5)

    assert calls == [
        ("GET", INTERNAL_URL + DOWNLOAD_PATH),
        ("POST", "https://extract.example.org/api"),
    ]
