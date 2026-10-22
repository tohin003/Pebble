import sys
import threading
from pathlib import Path
import pytest
from pebble.testing import run_tests


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS test sandbox")
def test_node_recipe_cannot_write_original_or_read_private_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("private")
    original = root / "source.js"
    original.write_text("original")
    (root / "isolation.test.js").write_text(
        "const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');\ntest('isolation',()=>{assert.throws(()=>fs.readFileSync("
        + repr(str(private))
        + "));assert.throws(()=>fs.writeFileSync("
        + repr(str(original))
        + ",'corrupt'));});\n"
    )
    result = run_tests(
        {"root": str(root), "recipe": "node-test", "drafts": []}, threading.Event()
    )
    assert result["exit_code"] == 0, result["output"]
    assert original.read_text() == "original"
