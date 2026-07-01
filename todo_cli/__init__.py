"""Todo CLI — a command-line TODO tool with CRUD operations.

Layered architecture:
    CLI (cli.py)  →  Service (service.py)  →  Storage (storage.py)
                          ↓
                     Models (models.py)

- CLI layer never touches files directly.
- Storage layer has no knowledge of business rules.
- Modules communicate via explicit function signatures.
"""

__version__ = "1.0.0"
