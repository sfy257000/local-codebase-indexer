import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from local_codebase_indexer.demo import _copy_sample
from local_codebase_indexer.query import CodebaseTools
from local_codebase_indexer.reports import mermaid, render_report
from local_codebase_indexer.setup_project import atomic_write, configure


@pytest.mark.parametrize("client", ["claude", "codex", "generic"])
def test_project_init_is_idempotent_and_preserves_other_servers(tmp_path, client):
    if client == "claude":
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "other"}}, "custom": True})
        )
    elif client == "codex":
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex/config.toml").write_text(
            'model = "existing"\n[mcp_servers.other]\ncommand="other"\n'
        )
    first = configure(str(tmp_path), client)
    second = configure(str(tmp_path), client)
    assert first["server"] == second["server"]
    assert not second["written"]
    if client == "claude":
        saved = json.loads((tmp_path / ".mcp.json").read_text())
        assert saved["custom"] and saved["mcpServers"]["other"]["command"] == "other"
    if client == "codex":
        assert 'model = "existing"' in (tmp_path / ".codex/config.toml").read_text()


def test_init_rejects_conflicting_or_external_configuration(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text('{"mcpServers":{"local-indexer":{"command":"different"}}}')
    with pytest.raises(ValueError, match="different"):
        configure(str(tmp_path), "claude")
    assert "different" in config.read_text()
    config.unlink()
    outside = tmp_path.parent / (tmp_path.name + "outside.json")
    outside.write_text("{}")
    try:
        config.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable for this Windows account")
    with pytest.raises(ValueError):
        configure(str(tmp_path), "claude")
    assert outside.read_text() == "{}"


def test_atomic_export_does_not_overwrite_existing_files_without_force(tmp_path):
    path = tmp_path / "report.html"
    atomic_write(path, "first")
    with pytest.raises(ValueError):
        atomic_write(path, "second")
    assert path.read_text() == "first"
    atomic_write(path, "second", overwrite=True)
    assert path.read_text() == "second"


def test_shareable_report_contains_source_paths_confidence_and_escaped_code(tmp_path):
    _copy_sample(tmp_path)
    tools = CodebaseTools(str(tmp_path))
    try:
        data = tools.data_access("Sales_OrderHeader")
        assert data["programs"][0]["paths"]
        data["writers"][0]["evidence"] = '</pre><script>alert("x")</script>'
        report = render_report(data, tools.index_status())
        assert "<svg " in report and "Content-Security-Policy" in report
        assert "<script>" not in report
        assert "&lt;script&gt;" in report
        assert "OrdersController" in report and "Details.cshtml" in report
        assert "probable" in report and "Evidence SHA256" in report
        assert "flowchart LR" in mermaid(data)
        parsed = json.loads(render_report(data, tools.index_status(), "json"))
        assert parsed["data_access"]["writers"]
        for program in data["programs"]:
            for trace in program["paths"]:
                assert all(s.get("line", 1) >= 1 for s in trace["steps"])
    finally:
        tools.indexer.close()


def test_cli_init_progress_and_report_work_outside_repository(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "x.sql").write_text(
        "CREATE TABLE dbo.Lot(id int);\nGO\nUPDATE dbo.Lot SET id=1;"
    )
    env = dict(os.environ, PYTHONPATH=str(root))
    command = [sys.executable, "-m", "local_codebase_indexer", "--root", str(tmp_path)]
    initialized = subprocess.run(
        command + ["init", "--client", "claude", "--json"],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(initialized.stdout)["passed"] and "complete" in initialized.stderr
    output = tmp_path / ".ai_context/report.html"
    completed = subprocess.run(
        command + ["report", "dbo.Lot", "--output", str(output), "--json"],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout)["found"] and output.exists()
    second = subprocess.run(
        command + ["report", "dbo.Lot", "--output", str(output)],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert second.returncode == 1


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_doctor_agrees_with_index_for_bom_unicode_sql(tmp_path, encoding):
    (tmp_path / "x.sql").write_bytes("CREATE TABLE dbo.Lot(id int);".encode(encoding))
    tools = CodebaseTools(str(tmp_path))
    try:
        tools.refresh()
        report = tools.doctor("x.sql")
        assert report["eligible_files"] == 1
        assert report["counts"].get("binary", 0) == 0
        assert tools.data_access("dbo.Lot")["found"]
        assert tools.index_status()["state"] == "fresh"
    finally:
        tools.indexer.close()
