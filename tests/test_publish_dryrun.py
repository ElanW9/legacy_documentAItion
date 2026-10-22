import json

from docgen.publish import DryRunPublisher


def test_dry_run_writes_payload_preview_and_metadata(tmp_path):
    pub = DryRunPublisher(tmp_path)
    assert pub.get_page_state("ORD_PROC (PL/SQL package)") is None
    r = pub.publish(
        title="ORD_PROC (PL/SQL package)",
        body="<p>x &amp; y</p>",
        metadata={"cache_key": "k"},
        labels=["ai-generated"],
        existing=None,
        version_message="m",
    )
    assert r.action == "dry_run"
    assert (tmp_path / "ord_proc_pl_sql_package.storage.xml").read_text() == "<p>x &amp; y</p>"
    assert "<p>x &amp; y</p>" in (tmp_path / "ord_proc_pl_sql_package.html").read_text()
    meta = json.loads((tmp_path / "ord_proc_pl_sql_package.meta.json").read_text())
    assert meta == {
        "title": "ORD_PROC (PL/SQL package)",
        "labels": ["ai-generated"],
        "metadata": {"cache_key": "k"},
    }
