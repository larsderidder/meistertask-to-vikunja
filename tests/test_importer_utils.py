import json
import zipfile

from meistertask_vikunja import cli


def test_split_list_variants():
    assert cli._split_list(None) == []
    assert cli._split_list("") == []
    assert cli._split_list("alpha") == ["alpha"]
    assert cli._split_list("alpha, beta") == ["alpha", "beta"]
    assert cli._split_list("alpha;beta") == ["alpha", "beta"]
    assert cli._split_list("alpha\nbeta") == ["alpha", "beta"]


def test_build_export_from_csv_rows():
    rows = [
        {
            "project": "Demo",
            "section": "Backlog",
            "name": "Task A",
            "notes": "Note",
            "status": "2",
            "due_date": "2024-01-01T10:00:00Z",
            "status_updated_at": "2024-01-02T10:00:00Z",
            "assignee": "Alice",
            "comments": "First comment",
            "tags": "one, two",
            "token": "t-1",
        },
        {
            "project": "Demo",
            "section": "Doing",
            "name": "Task B",
            "status": "1",
            "tags": "one",
            "token": "t-2",
        },
    ]

    export = cli._build_export_from_csv(rows)

    assert export["project"]["name"] == "Demo"
    assert len(export["sections"]) == 2
    assert len(export["tasks"]) == 2
    assert len(export["labels"]) == 2
    assert len(export["task_labels"]) == 3

    first_task = export["tasks"][0]
    assert first_task["name"] == "Task A"
    assert first_task["completed_at"].startswith("2024-01-02T")


def test_load_export_from_json_zip(tmp_path):
    payload = {"project": {"name": "Demo"}}
    json_path = tmp_path / "export.json"
    json_path.write_text(json.dumps(payload))

    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(json_path, arcname="export.json")

    loaded = cli._load_export(zip_path)
    assert loaded == payload
