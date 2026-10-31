"""The deployable app serves its UI without starting a model session."""

import importlib.util
import html
import re
import sys
from pathlib import Path

from starlette.testclient import TestClient


def test_app_serves_chat_and_only_public_assets(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CONSOLE_CHAT_RUNTIME", str(tmp_path / "server"))
    monkeypatch.setenv("CONSOLE_CHAT_BINARY", sys.executable)
    spec = importlib.util.spec_from_file_location(
        "console_demo_app_http", directory / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with TestClient(module.app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Console Chat" in response.text
        assert "shiny-chat" in response.text
        assert "Which airlines have the longest delays?" in response.text
        assert "336,776" in response.text
        for count in ("26,115", "3,322", "1,458", "16"):
            assert count in response.text
        assert 'role="tab"' in response.text and "Table views" in response.text
        assert "Parquet" in response.text and "Format code" in response.text
        assert "Plots &amp; results" not in response.text
        greeting = html.unescape(response.text).replace('\\"', '"')
        for language in ("R", "Python", "SQL"):
            assert f'data-runtime="{language}"' in greeting
        # Sessions use the host's prepared packages unless a user opts in.
        checkbox = re.search(r'<input[^>]*id="dynamic"[^>]*>', response.text)
        assert checkbox and "checked" not in checkbox.group()
        assert client.get("/styles.css").status_code == 200
        assert client.get("/server.lock").status_code == 404
        assert client.get("/nycflights13/flights.parquet").status_code == 404


def test_deployment_can_hide_dynamic_packages(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.setenv("CONSOLE_CHAT_RUNTIME", str(tmp_path / "server"))
    monkeypatch.setenv("CONSOLE_CHAT_BINARY", sys.executable)
    monkeypatch.setenv("CONSOLE_CHAT_DYNAMIC_PACKAGES", "0")
    spec = importlib.util.spec_from_file_location(
        "console_demo_app_hidden", directory / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with TestClient(module.app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'id="dynamic"' not in response.text
