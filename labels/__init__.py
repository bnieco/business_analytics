"""ENR shared label-printing package.

The print engine that Filament Inventory Phase 3 validated on the Brother
QL-720NW, extracted into a reusable, app-agnostic package (Label Printing
Service v1, Phase S).

Layout
------
  labels.engine    — the byte-assembly + transport engine (P-touch Template
                     dialect). No app knowledge, no hardcoded templates.
  labels.registry  — YAML-backed printer + template registries.
  labels.service   — the FastAPI microservice that fronts the same engine
                     over HTTP for non-Python / remote callers.

Two front doors, one engine (the locked hybrid architecture):
  * filament-intake imports the engine IN-PROCESS via its existing
    `spoolman.webapp.ptouch` module (zero behavior change);
  * every other app calls the `labels.service` HTTP API.
"""
