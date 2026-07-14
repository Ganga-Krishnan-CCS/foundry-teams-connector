"""Offline regression tests for build_reply — no Azure access needed.

Run:  .venv\\Scripts\\python tests\\test_build_reply.py   (or via pytest)
"""
import os
import sys

# Dummy config so app.py imports without a filled .env (values unused: all
# network calls are stubbed below).
os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")
os.environ.setdefault("FOUNDRY_AGENT_NAME", "test-agent")
os.environ.setdefault("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE", "ClientSecret")
os.environ.setdefault("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET", "test")
os.environ.setdefault("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID", "00000000-0000-0000-0000-000000000001")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

app._download_container_file_sync = lambda c, f: b"\x89PNG fake bytes"
app._upload_to_blob_sync = lambda data, name: f"https://blob.example/{name}"


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def ann(file_id, filename):
    return NS(type="container_file_citation", file_id=file_id,
              container_id="cntr_x", filename=filename)


def msg(text, annotations):
    return NS(type="message", content=[NS(type="output_text", text=text, annotations=annotations)])


def test_sandbox_links_stripped():
    r = NS(output=[msg("File: [sales.csv](sandbox:/mnt/data/sales.csv)", [ann("f1", "sales.csv")])])
    a = app.build_reply(r)
    assert "sandbox:" not in a.text
    assert a.text.startswith("File: sales.csv")


def test_auto_named_duplicate_image_dropped():
    r = NS(output=[msg("chart", [ann("cfile_abc", "cfile_abc.png"), ann("cfile_def", "sales.png")])])
    assert len(app.build_reply(r).attachments) == 1


def test_lone_auto_named_image_kept():
    r = NS(output=[msg("chart", [ann("cfile_abc", "cfile_abc.png")])])
    assert len(app.build_reply(r).attachments) == 1


def test_image_and_file_both_delivered():
    r = NS(output=[msg("both", [ann("f1", "sales.png"), ann("f2", "data.csv")])])
    a = app.build_reply(r)
    assert len(a.attachments) == 2


def test_orphan_file_recovered_from_container():
    class FakeFiles:
        def list(self, container_id):
            return [NS(id="cfile_orphan", path="/mnt/data/report.csv")]

    orig = app.openai_client
    app.openai_client = NS(containers=NS(files=FakeFiles()))
    try:
        r = NS(output=[
            NS(type="code_interpreter_call", container_id="cntr_1"),
            msg("Saved: [report.csv](sandbox:/mnt/data/report.csv)", []),
        ])
        a = app.build_reply(r)
        assert len(a.attachments) == 1
        assert "sandbox:" not in a.text
    finally:
        app.openai_client = orig


def test_no_files_plain_text():
    r = NS(output=[msg("Just words.", [])])
    a = app.build_reply(r)
    assert a.text == "Just words."
    assert not a.attachments


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
