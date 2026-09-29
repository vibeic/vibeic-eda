"""Short tool roles stay in the list; integration history stays in its drawer."""
from __future__ import annotations

import html
import json
import re

import pytest

import discover_forks as df
from test_page_lists_recipe_patches import HERE, _rendered_rows, _row


def test_librelane_registry_separates_summary_from_existing_installation_history():
    row = next(r for r in json.loads((HERE / "FORKS.json").read_text())["forks"]
               if r["tool"] == "librelane")
    assert row["role"] == "RTL-to-GDS flow driver with OpenROAD 26Q3 compatibility fixes."
    assert len(row["role"].split()) <= 15
    assert row["integration_notes"].startswith("RTL-to-GDS flow driver. The image keeps")
    assert "LIBRELANE_REF" in row["integration_notes"]
    assert "69bbd515" in row["integration_notes"]


@pytest.mark.parametrize("notes", [None, "Install <script>alert(1)</script> & validate \"files\"."])
def test_discovery_transports_optional_notes_without_changing_the_role(monkeypatch, notes):
    monkeypatch.setattr(df, "gh", lambda path: {"_err": "offline test"})
    fork = {"tool": "Tool", "upstream": "up/Tool", "role": "Flow driver."}
    if notes is not None:
        fork["integration_notes"] = notes
    row = df.discover_one(fork, {}, "test")
    assert row["role"] == "Flow driver."
    if notes is None:
        assert "integration_notes" not in row
    else:
        assert row["integration_notes"] == notes


def test_actual_emitted_script_keeps_long_notes_out_of_the_list(tmp_path):
    summary = "RTL-to-GDS flow driver with OpenROAD 26Q3 compatibility fixes."
    notes = "Install <script>alert(1)</script> & validate \"files\". " * 100
    rendered = _rendered_rows(tmp_path, [_row("librelane", role=summary,
                                             integration_notes=notes), _row("Other")])
    row = rendered["librelane"]
    list_row = row.split('<tr class="fork-detail"', 1)[0]
    assert f'<span class="role">{summary}</span>' in list_row
    assert "alert(1)" not in list_row
    drawer = re.search(r'<details\b([^>]*)>[\s\S]*?Image integration details[\s\S]*?</details>', row)
    assert drawer is not None
    assert "open" not in drawer.group(1).split()
    assert html.escape(notes, quote=True).replace("&#x27;", "&#39;") in drawer.group(0)
    assert "<script>alert(1)</script>" not in drawer.group(0)
    assert 'class="fork-detail open"' not in row
    assert "Image integration details" not in rendered["Other"]
