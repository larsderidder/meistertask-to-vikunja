"""CLI and helper utilities for importing Meistertask exports into Vikunja."""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    import csv

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def _iso_from_ms(ms_value: Optional[str]) -> Optional[str]:
    if ms_value is None:
        return None
    try:
        ms = int(float(ms_value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _iso_from_csv(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _split_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    value = value.strip()
    if not value:
        return []
    for sep in ["\n", ";", ","]:
        if sep in value:
            parts = [p.strip() for p in value.split(sep)]
            return [p for p in parts if p]
    return [value]


def _build_export_from_csv(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    project_name = None
    project_notes = None
    sections: Dict[str, Dict[str, Any]] = {}
    labels: Dict[str, Dict[str, Any]] = {}
    tasks: List[Dict[str, Any]] = []
    task_labels: List[Dict[str, Any]] = []

    for idx, row in enumerate(rows):
        project_name = project_name or row.get("project") or "Imported Project"
        project_notes = project_notes or ""
        section_name = row.get("section") or "Unsorted"
        if section_name not in sections:
            sections[section_name] = {
                "hashid": f"section:{section_name}",
                "name": section_name,
                "sequence": float(idx),
                "color": None,
                "description": None,
                "indicator": None,
                "limit": None,
            }
        section_id = sections[section_name]["hashid"]
        task_hash = row.get("token") or row.get("id") or f"task:{idx}"
        status = int(row.get("status") or 1)
        due_iso = _iso_from_csv(row.get("due_date"))
        status_updated = _iso_from_csv(row.get("status_updated_at"))

        task = {
            "hashid": task_hash,
            "name": row.get("name") or "",
            "notes": row.get("notes") or "",
            "status": status,
            "sequence": float(idx),
            "section_id": section_id,
            "due": None,
            "completed_at": None,
            "assignee_name": row.get("assignee") or "",
            "comments_raw": row.get("comments") or "",
        }
        if due_iso:
            task["due"] = due_iso
        if status == 2 and status_updated:
            task["completed_at"] = status_updated
        tasks.append(task)

        for tag in _split_list(row.get("tags")):
            if tag not in labels:
                labels[tag] = {"hashid": f"label:{tag}", "name": tag, "color": None}
            task_labels.append(
                {
                    "hashid": f"tasklabel:{task_hash}:{tag}",
                    "task_id": task_hash,
                    "label_id": labels[tag]["hashid"],
                }
            )

    export = {
        "project": {
            "name": project_name or "Imported Project",
            "notes": project_notes or "",
            "hashid": "csv",
        },
        "sections": list(sections.values()),
        "tasks": tasks,
        "labels": list(labels.values()),
        "checklists": [],
        "checklist_items": [],
        "custom_fields": [],
        "custom_field_types": [],
        "dropdown_items": [],
        "task_labels": task_labels,
        "timeline_items": [],
        "project_settings": [],
    }
    return export


def _load_export(path: Path) -> Dict[str, Any]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not json_names:
                raise ValueError("Zip file contains no .json export.")
            with zf.open(json_names[0]) as fh:
                return json.load(fh)
    if path.suffix.lower() == ".csv":
        return _build_export_from_csv(_read_csv_rows(path))
    return json.loads(path.read_text())


@dataclass
class Config:
    """Configuration for the importer CLI and API client."""

    base_url: str
    token: str
    project_id: Optional[int]
    dry_run: bool
    verify_ssl: bool
    continue_on_error: bool
    skip_checklists: bool
    skip_labels: bool
    debug_http: bool
    assignee_map: Dict[str, int]
    comment_delimiter: Optional[str]
    purge_project: bool
    purge_confirm: Optional[str]
    limit_tasks: Optional[int]


class VikunjaClient:
    """Minimal Vikunja API client for creating projects, tasks, and related entities."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.project_resource = "projects"
        self.kanban_view_id: Optional[int] = None
        self.session.headers.update(
            {
                "Authorization": f"Bearer {cfg.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/api/v1{path}"

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.cfg.dry_run:
            print(f"DRY RUN {method} {path} payload={payload}")
            return {}
        url = self._url(path)
        if self.cfg.debug_http:
            print(f"HTTP {method} {url}")
        resp = self.session.request(
            method,
            url,
            json=payload,
            verify=self.cfg.verify_ssl,
        )
        if self.cfg.debug_http:
            print(f"HTTP {resp.status_code} {url}")
        if not resp.ok:
            if self.cfg.debug_http:
                print(f"HTTP BODY {resp.text}")
            raise VikunjaHTTPError(method, path, resp.status_code, resp.text)
        if resp.text:
            return resp.json()
        return {}

    def _request_raw(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, Dict[str, Any]]:
        if self.cfg.dry_run:
            print(f"DRY RUN {method} {path} payload={payload} params={params}")
            return None, {}
        url = self._url(path)
        if self.cfg.debug_http:
            print(f"HTTP {method} {url} params={params}")
        resp = self.session.request(
            method,
            url,
            json=payload,
            params=params,
            verify=self.cfg.verify_ssl,
        )
        if self.cfg.debug_http:
            print(f"HTTP {resp.status_code} {url}")
        if not resp.ok:
            if self.cfg.debug_http:
                print(f"HTTP BODY {resp.text}")
            raise VikunjaHTTPError(method, path, resp.status_code, resp.text)
        if resp.text:
            return resp.json(), dict(resp.headers)
        return None, dict(resp.headers)

    def create_project(self, title: str, description: Optional[str]) -> int:
        payload = {"title": title}
        if description:
            payload["description"] = description
        path = f"/{self.project_resource}"
        try:
            data = self._request("PUT", path, payload)
        except VikunjaHTTPError as exc:
            if exc.status_code == 404 and self.project_resource == "projects":
                self.project_resource = "lists"
                data = self._request("PUT", "/lists", payload)
            else:
                raise
        return int(data.get("id", 0))

    def create_bucket(
        self,
        project_id: int,
        view_id: int,
        title: str,
        position: Optional[float],
        limit: Optional[int],
    ) -> int:
        payload: Dict[str, Any] = {"title": title}
        if position is not None:
            payload["position"] = position
        if limit is not None:
            payload["limit"] = limit
        data = self._request(
            "PUT",
            f"/{self.project_resource}/{project_id}/views/{view_id}/buckets",
            payload,
        )
        return int(data.get("id", 0))

    def list_buckets(self, project_id: int, view_id: int) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/{self.project_resource}/{project_id}/views/{view_id}/buckets")
        if isinstance(data, list):
            return data
        return []

    def delete_bucket(self, project_id: int, view_id: int, bucket_id: int) -> None:
        self._request("DELETE", f"/{self.project_resource}/{project_id}/views/{view_id}/buckets/{bucket_id}")

    def create_label(self, title: str, color: Optional[str]) -> int:
        payload: Dict[str, Any] = {"title": title}
        if color:
            payload["hex_color"] = color
        data = self._request("PUT", "/labels", payload)
        return int(data.get("id", 0))

    def list_labels(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/labels")
        if isinstance(data, list):
            return data
        return []

    def create_task(self, project_id: int, payload: Dict[str, Any]) -> int:
        data = self._request("PUT", f"/{self.project_resource}/{project_id}/tasks", payload)
        return int(data.get("id", 0))

    def add_label_to_task(self, task_id: int, label_id: int) -> None:
        payload = {"label_id": label_id}
        self._request("PUT", f"/tasks/{task_id}/labels", payload)

    def add_assignee_to_task(self, task_id: int, user_id: int) -> None:
        payload = {"user_id": user_id}
        self._request("PUT", f"/tasks/{task_id}/assignees", payload)

    def create_comment(self, task_id: int, comment: str) -> None:
        payload = {"comment": comment}
        self._request("PUT", f"/tasks/{task_id}/comments", payload)

    def create_checklist(self, task_id: int, title: str) -> int:
        payload = {"title": title}
        data = self._request("POST", f"/tasks/{task_id}/checklists", payload)
        return int(data.get("id", 0))

    def create_checklist_item(self, task_id: int, checklist_id: int, title: str, done: bool) -> None:
        payload = {"title": title, "done": done}
        self._request(
            "POST",
            f"/tasks/{task_id}/checklists/{checklist_id}/items",
            payload,
        )

    def ensure_project_resource(self, project_id: int) -> None:
        try:
            self._request("GET", f"/{self.project_resource}/{project_id}")
        except VikunjaHTTPError as exc:
            if exc.status_code == 404 and self.project_resource == "projects":
                self.project_resource = "lists"
                self._request("GET", f"/lists/{project_id}")
            else:
                raise

    def get_project_views(self, project_id: int) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/{self.project_resource}/{project_id}/views")
        if isinstance(data, list):
            return data
        return []

    def get_list_view_id(self, project_id: int) -> Optional[int]:
        views = self.get_project_views(project_id)
        for view in views:
            if view.get("view_kind") == "list":
                return int(view["id"])
        if views:
            return int(views[0]["id"])
        return None

    def get_kanban_view_id(self, project_id: int) -> Optional[int]:
        if self.kanban_view_id:
            return self.kanban_view_id
        views = self.get_project_views(project_id)
        for view in views:
            if view.get("view_kind") == "kanban":
                self.kanban_view_id = int(view["id"])
                return self.kanban_view_id
        return None

    def list_tasks_in_view(self, project_id: int, view_id: int) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            data, headers = self._request_raw(
                "GET",
                f"/{self.project_resource}/{project_id}/views/{view_id}/tasks",
                params={"page": page, "per_page": per_page},
            )
            if not data:
                break
            if isinstance(data, list) and data and isinstance(data[0], dict) and "tasks" in data[0]:
                for bucket in data:
                    tasks.extend(bucket.get("tasks") or [])
            elif isinstance(data, list):
                tasks.extend(data)
            total_pages = int(
                headers.get("X-Pagination-Total-Pages", headers.get("x-pagination-total-pages", 1))
                or 1
            )
            if page >= total_pages:
                break
            page += 1
        return tasks

    def delete_task(self, task_id: int) -> None:
        self._request("DELETE", f"/tasks/{task_id}")


class VikunjaHTTPError(RuntimeError):
    """HTTP error wrapper that preserves the request context."""

    def __init__(self, method: str, path: str, status_code: int, text: str):
        super().__init__(f"{method} {path} failed: {status_code} {text}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.text = text


def _normalize_color(color: Optional[str]) -> Optional[str]:
    if not color:
        return None
    color = color.strip()
    if not color:
        return None
    if not color.startswith("#"):
        return f"#{color}"
    return color


def _color_from_title(title: str) -> str:
    import hashlib

    digest = hashlib.md5(title.encode("utf-8")).hexdigest()
    return f"#{digest[:6]}"


def _sorted_by_sequence(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=lambda x: (x.get("sequence") is None, x.get("sequence", 0)))


def _status_done(status: Optional[int]) -> bool:
    # Meistertask task status: 1=open, 2=completed (observed).
    return status == 2


def _checklist_done(status: Optional[int]) -> bool:
    # Meistertask checklist item status: 1=open, 5=completed (observed).
    return status == 5


def _parse_due(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if "T" in value:
        return _iso_from_csv(value)
    return _iso_from_ms(value)


def _load_assignee_map(path: Optional[str]) -> Dict[str, int]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    raise ValueError("Assignee map must be a JSON object of {name: user_id}.")


def _write_assignee_map_template(input_path: Path, output_path: Path) -> None:
    if input_path.suffix.lower() != ".csv":
        raise ValueError("Assignee map template generation only supports CSV exports.")
    rows = _read_csv_rows(input_path)
    assignees = sorted({(row.get("assignee") or "").strip() for row in rows if row.get("assignee")})
    template = {name: None for name in assignees}
    output_path.write_text(json.dumps(template, indent=2, sort_keys=True))


def import_to_vikunja(export: Dict[str, Any], cfg: Config) -> None:
    """Import a Meistertask export dict into Vikunja."""

    client = VikunjaClient(cfg)

    def safe_call(desc: str, func):
        try:
            return func()
        except Exception as exc:
            if not cfg.continue_on_error:
                raise
            print(f"Warning: {desc} failed: {exc}", file=sys.stderr)
            return None

    project = export["project"]
    project_id = cfg.project_id
    if not project_id:
        print(f"Creating project: {project['name']}")
        project_id = safe_call(
            "create project",
            lambda: client.create_project(project["name"], project.get("notes")),
        )
        if not project_id:
            raise RuntimeError("Failed to create project (no id returned).")
    else:
        print(f"Using existing project ID: {project_id}")
        safe_call(
            f"verify project {project_id}",
            lambda: client.ensure_project_resource(project_id),
        )
    if cfg.purge_project:
        if cfg.purge_confirm != "YES":
            raise RuntimeError("Refusing to purge project without --purge-confirm YES.")
        print(f"Purging tasks and buckets in project {project_id}...")
        list_view_id = safe_call(
            f"fetch list view for project {project_id}",
            lambda: client.get_list_view_id(project_id),
        )
        if list_view_id is None:
            raise RuntimeError("No project view found; cannot purge tasks.")
        tasks_to_delete = safe_call(
            f"list tasks for project {project_id}",
            lambda: client.list_tasks_in_view(project_id, list_view_id),
        ) or []
        print(f"Deleting {len(tasks_to_delete)} tasks...")
        for task in tasks_to_delete:
            task_id = task.get("id")
            if not task_id:
                continue
            safe_call(
                f"delete task {task_id}",
                lambda tid=task_id: client.delete_task(int(tid)),
            )

        kanban_view_id = safe_call(
            f"fetch kanban view for project {project_id}",
            lambda: client.get_kanban_view_id(project_id),
        )
        if kanban_view_id is not None:
            buckets = safe_call(
                f"list buckets for project {project_id} view {kanban_view_id}",
                lambda: client.list_buckets(project_id, kanban_view_id),
            ) or []
            if not buckets:
                print("No buckets to delete.")
                return
            buckets_sorted = sorted(buckets, key=lambda b: b.get("position") or 0)
            buckets_to_delete = buckets_sorted[:-1]
            print(f"Deleting {len(buckets_to_delete)} buckets (leaving 1) ...")
            for bucket in buckets_to_delete:
                bucket_id = bucket.get("id")
                if not bucket_id:
                    continue
                safe_call(
                    f"delete bucket {bucket_id}",
                    lambda bid=bucket_id: client.delete_bucket(project_id, kanban_view_id, int(bid)),
                )

    # Create buckets from sections.
    buckets_by_section: Dict[str, int] = {}
    view_id = None
    if export["sections"]:
        print("Resolving kanban view for buckets...")
        view_id = safe_call(
            f"fetch kanban view for project {project_id}",
            lambda: client.get_kanban_view_id(project_id),
        )
        if view_id is None:
            print("No kanban view found; skipping buckets.")
    if view_id is not None:
        print(f"Creating {len(export['sections'])} buckets...")
        existing_buckets = safe_call(
            f"list buckets for project {project_id} view {view_id}",
            lambda: client.list_buckets(project_id, view_id),
        ) or []
        existing_by_title = {str(b.get("title", "")).strip().lower(): b for b in existing_buckets}
        for section in _sorted_by_sequence(export["sections"]):
            bucket_title = str(section.get("name") or "").strip()
            bucket_key = bucket_title.lower()
            existing = existing_by_title.get(bucket_key)
            if existing:
                bucket_id = int(existing.get("id", 0))
            else:
                bucket_id = safe_call(
                    f"create bucket {bucket_title}",
                    lambda s=section: client.create_bucket(
                        project_id,
                        view_id,
                        s["name"],
                        s.get("sequence"),
                        s.get("limit"),
                    ),
                )
            if bucket_id:
                buckets_by_section[section["hashid"]] = bucket_id
                if not existing:
                    existing_by_title[bucket_key] = {"id": bucket_id, "title": bucket_title}

    # Create labels.
    labels_by_hash: Dict[str, int] = {}
    if not cfg.skip_labels:
        print(f"Creating {len(export['labels'])} labels...")
        existing_labels = safe_call("list labels", client.list_labels) or []
        existing_by_title = {str(l.get("title", "")).strip().lower(): l for l in existing_labels}
        for label in export["labels"]:
            label_title = str(label.get("name") or "").strip()
            label_key = label_title.lower()
            existing = existing_by_title.get(label_key)
            if existing:
                label_id = int(existing.get("id", 0))
            else:
                color = _normalize_color(label.get("color")) or _color_from_title(label_title)
                label_id = safe_call(
                    f"create label {label_title}",
                    lambda l=label, c=color: client.create_label(l["name"], c),
                )
            if label_id:
                labels_by_hash[label["hashid"]] = label_id
                if not existing:
                    existing_by_title[label_key] = {"id": label_id, "title": label_title}

    # Pre-group task labels.
    task_labels: Dict[str, List[str]] = {}
    for tl in export["task_labels"]:
        task_labels.setdefault(tl["task_id"], []).append(tl["label_id"])

    # Pre-group checklists and items.
    checklists_by_task: Dict[str, List[Dict[str, Any]]] = {}
    for checklist in export["checklists"]:
        checklists_by_task.setdefault(checklist["task_id"], []).append(checklist)
    checklist_items_by_checklist: Dict[str, List[Dict[str, Any]]] = {}
    for item in export["checklist_items"]:
        checklist_items_by_checklist.setdefault(item["checklist_id"], []).append(item)

    tasks = _sorted_by_sequence(export["tasks"])
    if cfg.limit_tasks:
        tasks = tasks[: cfg.limit_tasks]

    print(f"Creating {len(tasks)} tasks...")
    for task in tasks:
        payload: Dict[str, Any] = {
            "title": task["name"],
            "description": task.get("notes") or "",
            "done": _status_done(task.get("status")),
        }
        due = _parse_due(task.get("due"))
        if due:
            payload["due_date"] = due
        done_at = _parse_due(task.get("completed_at"))
        if done_at:
            payload["done_at"] = done_at

        section_id = task.get("section_id")
        if section_id and section_id in buckets_by_section:
            payload["bucket_id"] = buckets_by_section[section_id]

        task_id = safe_call(
            f"create task {task['name']}",
            lambda p=payload: client.create_task(project_id, p),
        )
        if not task_id:
            if cfg.continue_on_error:
                continue
            raise RuntimeError("Failed to create task (no id returned).")

        if not cfg.skip_labels:
            for label_hash in task_labels.get(task["hashid"], []):
                label_id = labels_by_hash.get(label_hash)
                if not label_id:
                    continue
                safe_call(
                    f"add label {label_id} to task {task_id}",
                    lambda lid=label_id: client.add_label_to_task(task_id, lid),
                )

        assignee_name = task.get("assignee_name") or ""
        if assignee_name and cfg.assignee_map:
            user_id = cfg.assignee_map.get(assignee_name)
            if user_id:
                safe_call(
                    f"add assignee {assignee_name} to task {task_id}",
                    lambda uid=user_id: client.add_assignee_to_task(task_id, uid),
                )

        comments_raw = task.get("comments_raw") or ""
        if comments_raw:
            if cfg.comment_delimiter:
                comment_parts = [c.strip() for c in comments_raw.split(cfg.comment_delimiter)]
                comment_parts = [c for c in comment_parts if c]
            else:
                comment_parts = _split_list(comments_raw)
            for comment in comment_parts:
                safe_call(
                    f"create comment on task {task_id}",
                    lambda c=comment: client.create_comment(task_id, c),
                )

        if cfg.skip_checklists:
            continue

        for checklist in _sorted_by_sequence(checklists_by_task.get(task["hashid"], [])):
            checklist_id = safe_call(
                f"create checklist {checklist['name']} for task {task_id}",
                lambda c=checklist: client.create_checklist(task_id, c["name"]),
            )
            if not checklist_id:
                if cfg.continue_on_error:
                    continue
                raise RuntimeError("Failed to create checklist (no id returned).")
            items = _sorted_by_sequence(checklist_items_by_checklist.get(checklist["hashid"], []))
            for item in items:
                safe_call(
                    f"create checklist item {item['name']} on task {task_id}",
                    lambda i=item: client.create_checklist_item(
                        task_id,
                        checklist_id,
                        i["name"],
                        _checklist_done(i.get("status")),
                    ),
                )


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Meistertask export into Vikunja.")
    parser.add_argument("--input", required=True, help="Path to Meistertask export (.zip or .json).")
    parser.add_argument("--base-url", help="Vikunja base URL, e.g. https://vikunja.example.com.")
    parser.add_argument("--token", help="Vikunja API token (JWT).")
    parser.add_argument("--env-file", default=".env", help="Path to .env file for VIKUNJA_API_TOKEN.")
    parser.add_argument("--project-id", type=int, help="Existing Vikunja project ID to import into.")
    parser.add_argument("--dry-run", action="store_true", help="Print API calls without sending.")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue after errors.")
    parser.add_argument("--skip-checklists", action="store_true", help="Skip checklist import.")
    parser.add_argument("--skip-labels", action="store_true", help="Skip label import.")
    parser.add_argument("--debug-http", action="store_true", help="Log HTTP requests and responses.")
    parser.add_argument("--assignee-map", help="Path to JSON file mapping assignee names to user IDs.")
    parser.add_argument(
        "--write-assignee-map",
        help="Write assignee map template JSON from CSV input and exit.",
    )
    parser.add_argument("--comment-delimiter", help="Delimiter to split comments in CSV.")
    parser.add_argument("--purge-project", action="store_true", help="Delete all tasks in the target project before import.")
    parser.add_argument("--purge-confirm", help="Set to YES to confirm purge.")
    parser.add_argument("--limit-tasks", type=int, help="Only import the first N tasks.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Run the Meistertask import CLI."""

    args = _parse_args(argv or sys.argv[1:])
    token = args.token or os.environ.get("VIKUNJA_API_TOKEN")
    base_url = args.base_url or os.environ.get("VIKUNJA_BASE_URL")
    if args.write_assignee_map:
        _write_assignee_map_template(Path(args.input), Path(args.write_assignee_map))
        print(f"Wrote assignee map template to {args.write_assignee_map}")
        return 0
    if (not token or not base_url) and args.env_file:
        env_path = Path(args.env_file)
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key == "VIKUNJA_API_TOKEN" and not token:
                    token = value
                if key == "VIKUNJA_BASE_URL" and not base_url:
                    base_url = value
    if not base_url or not token:
        print("Error: base URL and token are required (use --base-url/--token or env/.env).", file=sys.stderr)
        return 1
    base_url = base_url.rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[: -len("/api/v1")]
    cfg = Config(
        base_url=base_url,
        token=token,
        project_id=args.project_id,
        dry_run=args.dry_run,
        verify_ssl=not args.insecure,
        continue_on_error=args.continue_on_error,
        skip_checklists=args.skip_checklists,
        skip_labels=args.skip_labels,
        debug_http=args.debug_http,
        assignee_map=_load_assignee_map(args.assignee_map),
        comment_delimiter=args.comment_delimiter,
        purge_project=args.purge_project,
        purge_confirm=args.purge_confirm,
        limit_tasks=args.limit_tasks,
    )
    try:
        export = _load_export(Path(args.input))
        import_to_vikunja(export, cfg)
    except Exception as exc:  # noqa: BLE001 - CLI should report any failure.
        if not cfg.continue_on_error:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Warning: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
