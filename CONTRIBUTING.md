# Contributing

Useful contributions include small reproductions of missing SQL access or wrong C# callers, corrected documentation, and tests for supported patterns. Please discuss a larger parser or storage change in an issue first.

## Development

Use Python 3.10+ in a virtual environment:

```bash
python -m pip install -e ".[dev,onnx]" onnx build twine
python -m ruff check local_codebase_indexer scripts benchmarks/compare_tools.py
python -m mypy
python -m pytest -q
python -m build
python -m twine check dist/*
```

ONNX dependencies are required for the full CI suite; do not silently skip these checks. CI covers Linux and Windows. To reproduce the first-run experience, run `local-codebase-indexer demo --json`.

## Report a navigation problem

Include your package/Python/OS versions, the exact command, expected source locations, actual output and its confidence labels. Prefer a small synthetic reproduction that you can share. Do not upload private application code, credentials, or your entire index database. `doctor --path <file>` can explain an excluded file before you report it.

## Evaluate a retrieval change

```bash
python benchmarks/run_benchmark.py --compare benchmarks/baseline.json
local-codebase-indexer --root . evaluate --queries benchmarks/queries_project.json --json
```

Add verified questions and expected source locations when a new behavior needs coverage. Keep evaluation datasets separate from query-expansion rules; do not copy test answers into synonyms, weaken a threshold, or edit gold answers just to make a run pass. A synthetic demo checks a supported path; it does not replace retrieval or real-project evaluation.

Public documentation and Release notes use English, with Traditional/Simplified Chinese README translations. Keep generated demo assets in sync by running `python scripts/render_demo.py` after installing `.[media]`.

Contributions are under this repository's [MIT license](LICENSE).
