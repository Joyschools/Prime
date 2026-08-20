# Deploy hardening

- `types.py` was renamed to `prime_pdf_types.py` because a top-level `types.py` shadows Python's standard-library `types` module during Render's Python bootstrap.
- `.python-version` pins the Render runtime family to Python 3.13 instead of following Render's moving default.
- `.gitignore` prevents Python bytecode/cache files from being committed.

Do not rename `pypdf/types.py`: that file belongs to the vendored `pypdf` package.
