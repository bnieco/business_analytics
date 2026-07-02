"""Thin client for the shared Label Printing Service (built in parallel).

The garage app carries NO printer sockets or byte-building — it is a client of
the label-service HTTP API (see the vault "Label Printing Service —
Extensibility Scope"):

    GET  {LABEL_SERVICE_URL}/templates
        -> [ { "name": ..., "printer": ..., "template_key": ...,
               "fields": ["QR_URL0001", "TXT_LINE10002", ...], ... }, ... ]
    POST {LABEL_SERVICE_URL}/print   { "template": ..., "fields": {...},
                                       "copies": n }

Field names are P-touch OBJECT NAMES declared by the template at registration
time, so they are NOT hardcoded here: we read the garage template's `fields`
list from GET /templates and map each object name to an item value by
name-substring heuristics (QR/URL -> the item's QR deep link, LINE1/LINE2 ->
label lines, LOCCODE/CODE -> loc_code, etc.).

Config (env, inline literals in the NAS compose):
    LABEL_SERVICE_URL  service origin. Local-dev default http://10.0.0.16:8744
                       (planned host port); NAS container should use the peer
                       container name (e.g. http://label-service:8000).
    GARAGE_TEMPLATE    the garage-bin template's registered name
                       (default 'garage_bin' — registered later by Brian via
                       P-touch Editor design + Transfer Manager + YAML entry).

The service and template do not exist yet: every failure mode degrades to a
clear, user-visible message instead of a stack trace.
"""
from __future__ import annotations

import os
from typing import Any

import requests

LABEL_SERVICE_URL = os.environ.get(
    "LABEL_SERVICE_URL", "http://10.0.0.16:8744").rstrip("/")
GARAGE_TEMPLATE = os.environ.get("GARAGE_TEMPLATE", "garage_bin")
HTTP_TIMEOUT = float(os.environ.get("LABEL_SERVICE_TIMEOUT", "10"))


class LabelServiceUnavailable(Exception):
    """The label service could not be reached (not deployed yet / down)."""


class TemplateNotRegistered(Exception):
    """The service is up but the garage template is not in its registry."""


class FieldMappingError(Exception):
    """A template field name could not be mapped to an item value."""


def _get_templates() -> list[dict]:
    try:
        resp = requests.get(f"{LABEL_SERVICE_URL}/templates", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise LabelServiceUnavailable(
            f"Label service not available at {LABEL_SERVICE_URL} "
            f"(not deployed yet, or down): {exc.__class__.__name__}"
        ) from exc
    except requests.HTTPError as exc:
        raise LabelServiceUnavailable(
            f"Label service at {LABEL_SERVICE_URL} answered "
            f"{exc.response.status_code} on GET /templates."
        ) from exc
    data = resp.json()
    # Accept either a bare list or {"templates": [...]}.
    return data.get("templates", []) if isinstance(data, dict) else data


#: Distinguishes "no mapping rule matched this object name" from "the rule
#: matched but the item's value is empty" (legitimate — e.g. a blank line 2).
_UNMAPPED = object()


def _map_field(object_name: str, item: dict, qr_url: str) -> Any:
    """Map one P-touch object name to an item value, by name substrings.

    Returns the value (possibly None for a legitimately-empty field) or the
    `_UNMAPPED` sentinel when no rule recognizes the name.
    """
    n = object_name.upper()
    if "QR" in n or "URL" in n:
        return qr_url
    if "LINE1" in n or "LINE_1" in n:
        return item.get("label_line1")
    if "LINE2" in n or "LINE_2" in n:
        return item.get("label_line2")
    if "LOCCODE" in n or "LOC_CODE" in n or "CODE" in n:
        return item.get("loc_code")
    if "LOCATION" in n:
        return item.get("location")
    if "ROOM" in n:
        return item.get("room")
    if "QTY" in n or "NUMBER" in n or "COUNT" in n:
        return item.get("qty")
    if "IDX" in n or "INDEX" in n:
        return item.get("idx")
    if "MEDIA" in n:
        return item.get("media")
    return _UNMAPPED


def print_item_label(item: dict, qr_url: str, copies: int = 1) -> dict:
    """Print the garage-bin label for `item` via the label service.

    Raises LabelServiceUnavailable / TemplateNotRegistered / FieldMappingError
    with operator-readable messages; the API layer surfaces them verbatim.
    """
    templates = _get_templates()
    tpl = next((t for t in templates if t.get("name") == GARAGE_TEMPLATE), None)
    if tpl is None:
        names = sorted(t.get("name", "?") for t in templates)
        raise TemplateNotRegistered(
            f"Template '{GARAGE_TEMPLATE}' is not registered in the label "
            f"service yet (available: {names or 'none'}). It lands after "
            f"Brian designs + transfers the garage QL template."
        )

    fields: dict[str, Any] = {}
    unmapped: list[str] = []
    for object_name in tpl.get("fields", []):
        value = _map_field(object_name, item, qr_url)
        if value is _UNMAPPED:
            unmapped.append(object_name)
        else:
            # None = mapped but legitimately empty (e.g. blank line 2) -> "".
            fields[object_name] = "" if value is None else str(value)
    if unmapped:
        raise FieldMappingError(
            f"Template '{GARAGE_TEMPLATE}' declares field(s) this app cannot "
            f"map: {unmapped}. Update labels._map_field for the new names."
        )

    try:
        resp = requests.post(
            f"{LABEL_SERVICE_URL}/print",
            json={"template": GARAGE_TEMPLATE, "fields": fields,
                  "copies": copies},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise LabelServiceUnavailable(
            f"Label service not available at {LABEL_SERVICE_URL}: "
            f"{exc.__class__.__name__}"
        ) from exc
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text[:300]
        raise LabelServiceUnavailable(
            f"Label service refused the print "
            f"({exc.response.status_code}): {detail}"
        ) from exc
    return resp.json() if resp.content else {"ok": True}
