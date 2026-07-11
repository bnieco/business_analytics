"""YAML-backed printer + template registries for the Label Printing Service.

Loads `printers.yaml` and `templates.yaml` (this package's directory by
default; override with the LABELS_PRINTERS_YAML / LABELS_TEMPLATES_YAML env
vars for tests or a bind-mounted config) into typed, validated objects.

Nothing about printers or templates is hardcoded in Python — registering a
new template or printer is an edit to the YAML, not a code change. See the
TEMPLATE-KEY DRIFT WARNING in templates.yaml (Argus M1): the registry is the
single source of truth for `template_key`, and it MUST be updated after any
re-transfer, because raw 9100 offers no way to verify the on-printer key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_PKG_DIR = Path(__file__).resolve().parent
DEFAULT_PRINTERS_YAML = _PKG_DIR / "printers.yaml"
DEFAULT_TEMPLATES_YAML = _PKG_DIR / "templates.yaml"

# The default printed for a field the caller omits, when a template does not
# set its own `placeholder`. (Argus M4: unknown fields always 400; missing
# fields default per-template — each app owns its own empties.)
DEFAULT_PLACEHOLDER = "None"

# Dialects the engine actually implements today. `raster` (PT-P750W) is a
# known FUTURE dialect: it may appear in printers.yaml without crashing the
# load, but any attempt to print through it is rejected by the service.
IMPLEMENTED_DIALECTS = frozenset({"ptouch-template"})


class RegistryError(ValueError):
    """Raised when the YAML registries are malformed or inconsistent."""


@dataclass(frozen=True)
class Printer:
    name: str
    model: str
    ip: str
    port: int
    transport: str
    dialect: str
    media: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "ip": self.ip,
            "port": self.port,
            "transport": self.transport,
            "dialect": self.dialect,
            "media": self.media,
        }


@dataclass(frozen=True)
class Template:
    name: str
    printer: str
    template_key: int
    objects: tuple[str, ...]
    placeholder: str = DEFAULT_PLACEHOLDER
    media: str = ""
    owner_app: str = ""
    # Data path the engine uses to assemble this template's stream:
    #   "named"   — ^ON/^DI per object (single-line; the proven filament path).
    #   "ordered" — ordered-fill (^SS pin + Tab delimiter + ^CR line feeds), the
    #               doc-supported MULTI-LINE path. Requires the template's objects
    #               to be NUMBERED in fill order. Default "named" so every existing
    #               template and caller is unaffected.
    fill_mode: str = "named"
    # (ordered mode only) object names that MAY carry a multi-line value — these
    # and ONLY these are ^CR-split; a newline in any other cell fails closed.
    # Ignored by the engine in named mode. Default empty.
    multiline_fields: tuple[str, ...] = ()
    _extra: dict = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "printer": self.printer,
            "template_key": self.template_key,
            "fields": list(self.objects),
            "placeholder": self.placeholder,
            "media": self.media,
            "owner_app": self.owner_app,
            "fill_mode": self.fill_mode,
            "multiline_fields": list(self.multiline_fields),
        }


def _require(mapping: dict, key: str, ctx: str):
    if key not in mapping or mapping[key] in (None, ""):
        raise RegistryError(f"{ctx}: required key {key!r} is missing or empty")
    return mapping[key]


class Registry:
    """The loaded, cross-validated pair of registries."""

    def __init__(self, printers: dict[str, Printer], templates: dict[str, Template]):
        self.printers = printers
        self.templates = templates

    # -- lookups ----------------------------------------------------------- #
    def printer(self, name: str) -> Printer | None:
        return self.printers.get(name)

    def template(self, name: str) -> Template | None:
        return self.templates.get(name)

    # -- construction ------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        printers_path: str | Path | None = None,
        templates_path: str | Path | None = None,
    ) -> "Registry":
        """Load + validate both YAML files. Raises RegistryError on any problem."""
        printers_path = Path(
            printers_path
            or os.environ.get("LABELS_PRINTERS_YAML")
            or DEFAULT_PRINTERS_YAML
        )
        templates_path = Path(
            templates_path
            or os.environ.get("LABELS_TEMPLATES_YAML")
            or DEFAULT_TEMPLATES_YAML
        )

        printers_raw = _load_yaml(printers_path, "printers")
        templates_raw = _load_yaml(templates_path, "templates")

        printers: dict[str, Printer] = {}
        for name, cfg in printers_raw.items():
            ctx = f"printers.yaml entry {name!r}"
            if not isinstance(cfg, dict):
                raise RegistryError(f"{ctx}: must be a mapping")
            printers[name] = Printer(
                name=name,
                model=str(_require(cfg, "model", ctx)),
                ip=str(_require(cfg, "ip", ctx)),
                port=int(_require(cfg, "port", ctx)),
                transport=str(_require(cfg, "transport", ctx)),
                dialect=str(_require(cfg, "dialect", ctx)),
                media=str(cfg.get("media", "")),
            )

        templates: dict[str, Template] = {}
        for name, cfg in templates_raw.items():
            ctx = f"templates.yaml entry {name!r}"
            if not isinstance(cfg, dict):
                raise RegistryError(f"{ctx}: must be a mapping")
            printer_name = str(_require(cfg, "printer", ctx))
            if printer_name not in printers:
                raise RegistryError(
                    f"{ctx}: printer {printer_name!r} is not in printers.yaml "
                    f"(known: {sorted(printers)})"
                )
            key = int(_require(cfg, "template_key", ctx))
            if not (0 <= key <= 999):
                raise RegistryError(f"{ctx}: template_key must be 0..999, got {key}")
            objects = cfg.get("objects")
            if not isinstance(objects, list) or not objects or not all(
                isinstance(o, str) and o for o in objects
            ):
                raise RegistryError(f"{ctx}: objects must be a non-empty list of names")
            if len(set(objects)) != len(objects):
                raise RegistryError(f"{ctx}: duplicate object names in objects")
            # fill_mode: default "named" so existing templates are unchanged.
            fill_mode = str(cfg.get("fill_mode", "named"))
            if fill_mode not in ("named", "ordered"):
                raise RegistryError(
                    f"{ctx}: fill_mode must be 'named' or 'ordered', got "
                    f"{fill_mode!r}"
                )
            # multiline_fields: default empty; every name must be one of objects.
            multiline_raw = cfg.get("multiline_fields") or []
            if not isinstance(multiline_raw, list) or not all(
                isinstance(o, str) and o for o in multiline_raw
            ):
                raise RegistryError(
                    f"{ctx}: multiline_fields must be a list of object names"
                )
            ml_unknown = sorted(set(multiline_raw) - set(objects))
            if ml_unknown:
                raise RegistryError(
                    f"{ctx}: multiline_fields names not in objects: {ml_unknown}"
                )
            templates[name] = Template(
                name=name,
                printer=printer_name,
                template_key=key,
                objects=tuple(objects),
                placeholder=str(cfg.get("placeholder", DEFAULT_PLACEHOLDER)),
                media=str(cfg.get("media", "")),
                owner_app=str(cfg.get("owner_app", "")),
                fill_mode=fill_mode,
                multiline_fields=tuple(multiline_raw),
            )

        if not printers:
            raise RegistryError(f"{printers_path}: no printers defined")
        if not templates:
            raise RegistryError(f"{templates_path}: no templates defined")
        return cls(printers, templates)


def _load_yaml(path: Path, top_key: str) -> dict:
    if not path.is_file():
        raise RegistryError(f"registry file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or top_key not in doc:
        raise RegistryError(f"{path}: expected a top-level {top_key!r} mapping")
    entries = doc[top_key]
    if not isinstance(entries, dict):
        raise RegistryError(f"{path}: {top_key!r} must be a mapping of name -> config")
    return entries
