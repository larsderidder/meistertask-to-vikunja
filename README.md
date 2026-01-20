# meistertask-vikunja

Import Meistertask exports into Vikunja via the API. Supports Meistertask JSON exports, ZIP archives
that include a JSON export, and CSV exports with basic task fields.

## Install

Source install:

```bash
python -m pip install -e .
```

## Usage

Provide a Meistertask export and a Vikunja API token.

```bash
meistertask-to-vikunja \
  --input /path/to/meistertask-export.zip \
  --base-url https://vikunja.example.com \
  --token $VIKUNJA_API_TOKEN
```

Environment variables:

- `VIKUNJA_BASE_URL`
- `VIKUNJA_API_TOKEN`

Optional flags:

- `--project-id` to import into an existing project
- `--dry-run` to log requests without sending
- `--skip-checklists` or `--skip-labels`
- `--assignee-map /path/to/map.json` to map assignee names to Vikunja user IDs
- `--write-assignee-map /path/to/map.json` to write a template map from a CSV export
- `--purge-project --purge-confirm YES` to delete existing tasks before import

Assignee map JSON format:

```json
{
  "Jane Doe": 5,
  "Sam Lee": 9
}
```

Generate a template assignee map from a CSV export:

```bash
meistertask-to-vikunja \
  --input /path/to/project-export.csv \
  --write-assignee-map ./assignee-map.json
```

Assignee mapping uses display names from the CSV export. JSON exports do not include assignee IDs,
so you need to supply the map when importing from CSV.

## Choosing JSON or CSV

### CSV vs JSON tradeoffs

- CSV gives extra fields you don’t get in the JSON export: assignee (name), tags, comments, plus
  straightforward status, due_date.
- JSON preserves structure and relationships: stable IDs, section ordering, label colors, checklist
  items, and full notes; CSV flattens this and loses checklist structure/details.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
pytest
```
