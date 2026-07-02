"""ENR Garage Inventory App.

Postgres-backed replacement for the hand-maintained garage-label spreadsheet
(`Label Inventory xls format.xls`). See the vault scope doc
"Garage Inventory App — Project Scope" for design + decisions.

Deliberately independent of the `spoolman/` package (which is being refactored
in a parallel build): this package carries its own env-driven DB helper and a
thin HTTP client for the shared Label Printing Service.
"""
