"""The tool surface is a contract: catalog.yaml, tools.yaml and the adapter routes must agree."""

import subprocess
from pathlib import Path

import yaml

from dba_observer.config import catalog
from dba_observer.server import ROUTES

ROOT = Path(__file__).resolve().parents[1]
TOOLS_YAML = ROOT / "docker" / "tools.yaml"
CHECK = ROOT / "docker" / "check-tools-yaml.sh"


def docs():
    return [d for d in yaml.safe_load_all(TOOLS_YAML.read_text()) if d]


def toolbox_tools():
    return {d["name"]: d for d in docs() if d["kind"] == "tool"}


def test_catalog_has_50_unique_tools():
    names = [t["name"] for t in catalog()["tools"]]
    assert len(names) == 50
    assert len(set(names)) == 50


def test_tools_yaml_matches_catalog_exactly():
    assert set(toolbox_tools()) == {t["name"] for t in catalog()["tools"]}


def test_backends_agree():
    tools = toolbox_tools()
    for t in catalog()["tools"]:
        kind = tools[t["name"]]["type"]
        if t["backend"] == "adapter":
            assert kind == "http", t["name"]
            assert tools[t["name"]]["path"] == f"/v1/tools/{t['name']}"
            assert t["name"] in ROUTES, f"{t['name']} has no adapter route"
        else:
            assert kind in ("mysql-sql", "postgres-sql"), t["name"]
    adapter_tools = {t["name"] for t in catalog()["tools"] if t["backend"] == "adapter"}
    assert set(ROUTES) == adapter_tools


def test_adapter_query_params_match_routes():
    tools = toolbox_tools()
    for name, route in ROUTES.items():
        declared = {p["name"] for p in tools[name].get("queryParams") or []}
        assert declared == {p.name for p in route.params}, name


def test_every_tool_is_annotated_read_only():
    for name, t in toolbox_tools().items():
        assert t.get("annotations", {}).get("readOnlyHint") is True, name


def test_no_server_prefix_and_no_passthrough_names():
    for name in toolbox_tools():
        assert not name.startswith(("dba_", "dba-observer")), name
        assert "execute" not in name and "query_sql" not in name, name


def test_only_bound_parameters():
    for name, t in toolbox_tools().items():
        assert "templateParameters" not in t, name


def test_integer_parameters_are_bounded():
    for name, t in toolbox_tools().items():
        for p in (t.get("parameters") or []) + (t.get("queryParams") or []):
            if p["type"] == "integer":
                assert "minValue" in p, f"{name}.{p['name']}"
                if p["name"] == "limit":
                    assert p.get("maxValue", 10**9) <= 500, f"{name}.limit"


def test_toolsets_reference_real_tools():
    names = set(toolbox_tools())
    for d in docs():
        if d["kind"] == "toolset":
            assert set(d["tools"]) <= names, d["name"]


def test_check_script_accepts_ours():
    subprocess.run(["bash", str(CHECK), str(TOOLS_YAML)], check=True, capture_output=True)


def test_check_script_rejects_execute_sql(tmp_path):
    bad = tmp_path / "tools.yaml"
    bad.write_text(TOOLS_YAML.read_text() + "\n---\nkind: tool\nname: x\ntype: mysql-execute-sql\nsource: mysql\ndescription: x\n")
    r = subprocess.run(["bash", str(CHECK), str(bad)], capture_output=True, text=True)
    assert r.returncode != 0 and "mysql-execute-sql" in r.stderr


def test_check_script_rejects_template_parameters(tmp_path):
    bad = tmp_path / "tools.yaml"
    bad.write_text(TOOLS_YAML.read_text() + "\n---\nkind: tool\nname: x\ntype: mysql-sql\nsource: mysql\ndescription: x\nstatement: SELECT 1\ntemplateParameters: []\n")
    r = subprocess.run(["bash", str(CHECK), str(bad)], capture_output=True, text=True)
    assert r.returncode != 0
