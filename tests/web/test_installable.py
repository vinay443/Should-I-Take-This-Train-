"""The pieces that let a phone keep the page: manifest, icons, service worker, offline
fallback. The behaviour in a real browser is checked by scripts/check_web_offline.py."""

import json
import re
import struct
from datetime import datetime

import pytest
from conftest import SAMPLE_CSV
from fastapi.testclient import TestClient

from sitt.db import init_db
from sitt.ingest.timetable import load_timetable, read_timetable
from sitt.tz import IST
from sitt.web import app as web
from sitt.web.app import create_app

NOW = datetime(2026, 9, 28, 7, 0, tzinfo=IST)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "sitt.duckdb"
    with init_db(path) as con:
        load_timetable(con, read_timetable(SAMPLE_CSV))
    return TestClient(create_app(db_path=path, model_dir=None, now=lambda: NOW))


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_the_manifest_makes_the_page_installable(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    # What Chrome needs to offer "Install app".
    assert manifest["name"] and manifest["short_name"]
    assert manifest["start_url"] == "/" and manifest["display"] == "standalone"
    sizes = set()
    for icon in manifest["icons"]:
        image = client.get(icon["src"])
        assert image.status_code == 200, icon["src"]
        width, height = png_size(image.content)
        assert f"{width}x{height}" == icon["sizes"]
        sizes.add((width, icon["purpose"]))
    assert {(192, "any"), (512, "any"), (512, "maskable")} <= sizes


def test_the_page_links_the_manifest(client):
    page = client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'name="theme-color"' in page and 'rel="apple-touch-icon"' in page


def test_the_service_worker_is_served_from_the_root_and_never_cached(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert "__VERSION__" not in response.text and "__SHELL__" not in response.text


def test_every_file_the_worker_keeps_exists(client):
    version = web.static_version()
    shell = web.shell_files(version)
    assert "/" in shell and "/patterns" in shell
    assert f"/static/app.js?v={version}" in shell and f"/static/styles.css?v={version}" in shell
    assert any(url.endswith(".woff2") for url in shell)
    assert json.dumps(shell) in client.get("/sw.js").text
    for url in shell:
        assert client.get(url).status_code == 200, url


def test_pages_load_their_scripts_by_version(client):
    # A new page must never run with an old script the phone kept: the URLs differ.
    version = web.static_version()
    page = client.get("/").text
    assert f'src="/static/app.js?v={version}"' in page
    assert f'href="/static/styles.css?v={version}"' in page
    assert f'src="/static/patterns.js?v={version}"' in client.get("/patterns").text
    # Every versioned file a page links is one the worker keeps under that same URL.
    shell = set(web.shell_files(version))
    for name in ("/", "/patterns"):
        for url in re.findall(r'(?:src|href)="(/static/[^"]+\?v=[^"]+)"', client.get(name).text):
            assert url in shell, url


def test_the_worker_only_stores_the_apps_own_pages(client):
    source = client.get("/sw.js").text
    assert 'const PAGES = ["/", "/patterns"];' in source
    # Writes and answers always go to the app; the page keeps answers itself.
    assert '"/api/recommend"' not in source and '"/api/reports"' not in source
    assert 'request.method !== "GET"' in source
    # Other pages (the API docs, raw JSON) fall back to "/" offline but are never stored
    # under it: storing is only for a page in PAGES. The browser check
    # (scripts/check_web_offline.py) proves this in Chromium.
    assert 'networkFirst(request, page ? url.pathname : "/", page)' in source


def test_the_cache_version_changes_when_a_static_file_changes(tmp_path, monkeypatch):
    static = tmp_path / "static"
    for folder in ("fonts", "icons"):
        (static / folder).mkdir(parents=True)
    (static / "sw.js").write_text(
        'const VERSION = "__VERSION__"; const SHELL = __SHELL__;', "utf-8"
    )
    (static / "app.js").write_text("one", "utf-8")
    monkeypatch.setattr(web, "STATIC", static)
    first = web.service_worker()
    assert first == web.service_worker()  # stable while nothing changes
    (static / "app.js").write_text("two", "utf-8")
    assert web.service_worker() != first


def test_the_page_falls_back_to_saved_answers():
    script = (web.STATIC / "app.js").read_text("utf-8")
    assert "class OfflineError" in script and "saveAnswer(params, answer)" in script
    assert "Not live." in script and "render(saved.answer, saved.saved_at)" in script
    # The worker registers only where browsers allow it.
    assert "window.isSecureContext" in script
