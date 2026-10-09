"""Implementations: local packages that give Kairo capabilities in a domain.

An implementation is a directory ``<implementations-dir>/<id>/`` with an
``implementation.json`` manifest, and optionally domain guidance, tools, checks
and any other files. It is inert: nothing in it runs because it exists. Its
tools and checks become ordinary actions (``impl.<id>.<tool>``,
``impl.<id>.check``), executed only when cognition requests them and the
runtime accepts them, through the same validation, execution, verification and
logging as every other action.

Three authorities, never merged:

    package content    the filesystem (read and validated here, every time)
    enablement         operator configuration (``enabled``)
    historical usage   action records (implementation id + content digest)

The manifest declares what a package offers and needs; it grants nothing. The
runtime decides whether a package's tools become executable actions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairo.redact import env_owner, protect_env

FORMAT = 1
MANIFEST = "implementation.json"

# Identifiers that become part of action kinds: strict, bounded, unambiguous.
ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")            # no dot, slash, space, underscore
TOOL = re.compile(r"^[a-z][a-z0-9_]{0,39}$")          # no dot, slash, space, hyphen
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")  # a bare command looked up on PATH
KIND = re.compile(r"^impl\.[a-z][a-z0-9-]{0,39}\.[a-z][a-z0-9_]{0,39}$")
PREFIX = "impl."
CHECK = "check"           # reserved tool name: the action that runs declared checks
MAX_KIND = 90
MAX_TOOLS = 32
MAX_CHECKS = 16
MAX_ARGV = 32
MAX_ARG = 500
MAX_TIMEOUT = 3600.0
DEFAULT_TOOL_TIMEOUT = 60.0
TEXT = {"description": 300, "version": 40}
GUIDANCE_READ = 20_000    # characters read from a guidance file (the situation shows less)
SKIP_DIRS = {".git", "__pycache__"}
SKIP_SUFFIXES = (".pyc",)

MANIFEST_FIELDS = {"kairo_implementation", "id", "description", "version", "guidance",
                   "requires", "env", "tools", "checks"}
TOOL_FIELDS = {"name", "description", "run", "params", "timeout", "verify", "effects",
               "idempotency"}
# What a tool declares about effects outside this host (optional; undeclared
# tools keep the plain exit-code semantics):
#   effects: "none"       it only reads or observes
#   effects: "external"   it may change state elsewhere; it must exit 0 when the
#                         operation was performed and 3 when it certainly was not
#                         (anything else, a timeout or a kill: outcome unknown)
#   idempotency: "operation_key"   (external tools only) the external system is
#                         given KAIRO_OPERATION_KEY and performs an operation at
#                         most once per key, so an unresolved operation may be resumed
EFFECTS = frozenset({"none", "external"})
IDEMPOTENCY = frozenset({"operation_key"})
CHECK_FIELDS = {"name", "run", "timeout"}


class ImplementationError(ValueError):
    """Why a package is broken, or why a request to one is refused."""


# -- parameter schemas: a small, strict JSON Schema subset ------------------------

SCALARS = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
PROPERTY_KEYS = {"type", "description", "enum", "items"}


def check_schema(schema: Any) -> dict[str, Any]:
    """Accept only: an object with typed properties (string, integer, number,
    boolean, or array of strings), optional enums, ``required``, and
    ``additionalProperties: false``. Anything else is rejected, not guessed."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ImplementationError("params must be a schema of type object")
    if extra := set(schema) - {"type", "properties", "required", "additionalProperties", "description"}:
        raise ImplementationError(f"unsupported params schema keys: {sorted(extra)}")
    if schema.get("additionalProperties") is not False:
        raise ImplementationError("params schema must set additionalProperties: false")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ImplementationError("params properties must be an object")
    for name, prop in properties.items():
        if not TOOL.match(name):
            raise ImplementationError(f"invalid parameter name {name!r}")
        if not isinstance(prop, dict) or set(prop) - PROPERTY_KEYS:
            raise ImplementationError(f"parameter {name!r}: unsupported schema")
        kind = prop.get("type")
        if kind == "array":
            if prop.get("items") != {"type": "string"} or "enum" in prop:
                raise ImplementationError(f"parameter {name!r}: arrays must be of strings")
        elif kind in SCALARS:
            if "items" in prop:
                raise ImplementationError(f"parameter {name!r}: items only applies to arrays")
            enum = prop.get("enum")
            if enum is not None and (not isinstance(enum, list) or not enum or kind == "boolean"
                                     or not all(_is(v, kind) for v in enum)):
                raise ImplementationError(f"parameter {name!r}: enum must list {kind} values")
        else:
            raise ImplementationError(f"parameter {name!r}: unsupported type {kind!r}")
        if "description" in prop and not isinstance(prop["description"], str):
            raise ImplementationError(f"parameter {name!r}: description must be a string")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(r in properties for r in required):
        raise ImplementationError("params required must list declared properties")
    return schema


def check_params(schema: dict[str, Any], params: Any) -> None:
    """Validate request parameters against a schema accepted by check_schema."""
    if not isinstance(params, dict):
        raise ImplementationError("params must be an object")
    properties = schema.get("properties", {})
    if extra := set(params) - set(properties):
        raise ImplementationError(f"unknown params: {sorted(extra)}")
    if missing := [r for r in schema.get("required", []) if r not in params]:
        raise ImplementationError(f"missing params: {missing}")
    for name, value in params.items():
        prop = properties[name]
        if prop["type"] == "array":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ImplementationError(f"param {name!r} must be a list of strings")
        elif not _is(value, prop["type"]):
            raise ImplementationError(f"param {name!r} must be of type {prop['type']}")
        elif "enum" in prop and value not in prop["enum"]:
            raise ImplementationError(f"param {name!r} must be one of {prop['enum']}")


def _is(value: Any, kind: str) -> bool:
    if isinstance(value, bool):
        return kind == "boolean"
    return isinstance(value, SCALARS[kind])


# -- packages -------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """A tool, or a declared check: something the runtime can run for a package."""

    name: str
    run: tuple[str, ...]
    timeout: float
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    verify: tuple[str, ...] | None = None
    effects: str | None = None        # one of EFFECTS, or None (undeclared)
    idempotency: str | None = None    # one of IDEMPOTENCY, or None


@dataclass(frozen=True)
class Package:
    id: str
    path: Path
    description: str
    version: str | None
    guidance: Path | None
    commands: tuple[str, ...]
    env: dict[str, bool]                 # declared variable name -> secret?
    tools: tuple[Operation, ...]
    checks: tuple[Operation, ...]
    digest: str

    @property
    def secrets(self) -> list[str]:
        return [name for name, secret in self.env.items() if secret]

    def kinds(self) -> list[str]:
        kinds = [action_kind(self.id, t.name) for t in self.tools]
        if self.checks:
            kinds.append(action_kind(self.id, CHECK))
        return kinds


def action_kind(implementation: str, tool: str) -> str:
    kind = f"{PREFIX}{implementation}.{tool}"
    if len(kind) > MAX_KIND or not KIND.match(kind):
        raise ImplementationError(f"invalid action kind {kind!r}")
    return kind


def load_package(path: Path) -> Package:
    """Read and strictly validate one package. Never runs anything in it."""
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        raise ImplementationError(f"no {MANIFEST}")
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImplementationError(f"{MANIFEST} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ImplementationError(f"{MANIFEST} must be an object")
    if extra := set(data) - MANIFEST_FIELDS:
        raise ImplementationError(f"unknown manifest fields: {sorted(extra)}")
    if data.get("kairo_implementation") != FORMAT:
        raise ImplementationError(f"unsupported format {data.get('kairo_implementation')!r} "
                                  f"(expected kairo_implementation: {FORMAT})")
    pid = data.get("id")
    if not isinstance(pid, str) or not ID.match(pid):
        raise ImplementationError(f"invalid id {pid!r} (expected {ID.pattern})")
    if pid != path.name:
        raise ImplementationError(f"id {pid!r} does not match its directory {path.name!r}")
    description = _text(data, "description", required=True)
    version = _text(data, "version")
    guidance = _inside(path, data["guidance"], "guidance") if "guidance" in data else None
    if guidance is not None and not guidance.is_file():
        raise ImplementationError(f"guidance file {data['guidance']!r} does not exist")

    requires = data.get("requires", {})
    if not isinstance(requires, dict) or set(requires) - {"commands"}:
        raise ImplementationError("requires may only contain commands")
    commands = requires.get("commands", [])
    if not isinstance(commands, list) or not all(isinstance(c, str) and COMMAND.match(c)
                                                 for c in commands):
        raise ImplementationError("requires.commands must be a list of command names")

    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ImplementationError("env must be an object")
    declared: dict[str, bool] = {}
    for name, spec in env.items():
        if not ENV_NAME.match(name):
            raise ImplementationError(f"invalid env name {name!r}")
        if not isinstance(spec, dict) or set(spec) != {"secret"} or not isinstance(spec["secret"], bool):
            raise ImplementationError(f"env {name!r} must be {{\"secret\": true|false}}")
        declared[name] = spec["secret"]

    tools = tuple(_operation(path, t, TOOL_FIELDS, "tool") for t in _list(data, "tools", MAX_TOOLS))
    checks = tuple(_operation(path, c, CHECK_FIELDS, "check") for c in _list(data, "checks", MAX_CHECKS))
    for group, label in ((tools, "tool"), (checks, "check")):
        names = [o.name for o in group]
        if len(set(names)) != len(names):
            raise ImplementationError(f"duplicate {label} names")
    if any(t.name == CHECK for t in tools):
        raise ImplementationError(f"tool name {CHECK!r} is reserved for checks")
    package = Package(pid, path, description, version, guidance, tuple(commands), declared,
                      tools, checks, content_digest(path))
    for kind in package.kinds():  # every generated action kind must itself be valid
        action_kind(*kind[len(PREFIX):].split(".", 1))
    return package


def _operation(root: Path, spec: Any, allowed: set[str], label: str) -> Operation:
    if not isinstance(spec, dict) or set(spec) - allowed or "name" not in spec or "run" not in spec:
        raise ImplementationError(f"each {label} needs name and run, and only {sorted(allowed)}")
    name = spec["name"]
    if not isinstance(name, str) or not TOOL.match(name):
        raise ImplementationError(f"invalid {label} name {name!r} (expected {TOOL.pattern})")
    timeout = spec.get("timeout", DEFAULT_TOOL_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ImplementationError(f"{label} {name!r}: timeout must be in (0, {MAX_TIMEOUT:g}]")
    params = check_schema(spec["params"]) if "params" in spec else {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False}
    description = spec.get("description", "")
    if label == "tool" and (not isinstance(description, str) or not description.strip()
                            or len(description) > TEXT["description"]):
        raise ImplementationError(f"tool {name!r}: description must be 1-{TEXT['description']} characters")
    effects, idempotency = spec.get("effects"), spec.get("idempotency")
    if effects is not None and effects not in EFFECTS:
        raise ImplementationError(f"{label} {name!r}: effects must be one of {sorted(EFFECTS)}")
    if idempotency is not None and idempotency not in IDEMPOTENCY:
        raise ImplementationError(f"{label} {name!r}: idempotency must be one of "
                                  f"{sorted(IDEMPOTENCY)}")
    if idempotency is not None and effects != "external":
        raise ImplementationError(f"{label} {name!r}: idempotency applies only to "
                                  "effects: external")
    return Operation(name, _argv(root, spec["run"], f"{label} {name!r}"), float(timeout),
                     description, params,
                     _argv(root, spec["verify"], f"{label} {name!r} verify") if "verify" in spec else None,
                     effects, idempotency)


def _argv(root: Path, argv: Any, where: str) -> tuple[str, ...]:
    """A command line, no shell. The program is a bare command (found on PATH)
    or a package file; any argument containing '/' must be an existing package
    file. Nothing may point outside the package."""
    if (not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV
            or not all(isinstance(a, str) and 0 < len(a) <= MAX_ARG and "\x00" not in a for a in argv)):
        raise ImplementationError(f"{where}: run must be a list of 1-{MAX_ARGV} non-empty strings")
    if "/" not in argv[0] and not COMMAND.match(argv[0]):
        raise ImplementationError(f"{where}: {argv[0]!r} is not a command name or package path")
    for arg in argv:
        if "/" in arg and not arg.startswith("-"):
            target = _inside(root, arg, where)
            if not target.exists():
                raise ImplementationError(f"{where}: {arg!r} does not exist in the package")
    return tuple(argv)


def _inside(root: Path, relative: Any, where: str) -> Path:
    """A path in the package: relative, no '..', and not escaping via symlinks."""
    if not isinstance(relative, str) or not relative or relative.startswith("/") \
            or ".." in Path(relative).parts:
        raise ImplementationError(f"{where}: {relative!r} must be a relative path inside the package")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ImplementationError(f"{where}: {relative!r} leads outside the package")
    return target


def _text(data: dict[str, Any], key: str, required: bool = False) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > TEXT[key]:
        raise ImplementationError(f"{key} must be a non-empty string of at most {TEXT[key]} characters")
    return value


def _list(data: dict[str, Any], key: str, limit: int) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > limit:
        raise ImplementationError(f"{key} must be a list of at most {limit}")
    return value


_digests: dict[tuple[Any, ...], str] = {}


def content_digest(path: Path) -> str:
    """SHA-256 over sorted relative paths and file contents (symlinks by their
    target, not followed). Cached by the files' sizes and modification times."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            if name.endswith(SKIP_SUFFIXES):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(path).as_posix()
            stat = full.lstat()
            entries.append((rel, stat.st_size, stat.st_mtime_ns, full.is_symlink()))
    signature = (str(path), tuple(sorted(entries)))
    if signature in _digests:
        return _digests[signature]
    h = hashlib.sha256()
    for rel, _, _, is_link in sorted(entries):
        full = path / rel
        h.update(rel.encode() + b"\0")
        h.update(b"L" + os.readlink(full).encode() if is_link else b"F" + full.read_bytes())
        h.update(b"\0")
    _digests[signature] = h.hexdigest()
    return _digests[signature]


def check_action_params(package: Package) -> dict[str, Any]:
    return {"type": "object",
            "properties": {"name": {"type": "string", "enum": [c.name for c in package.checks]}},
            "required": ["name"], "additionalProperties": False}


def _protect_declared_secrets(path: Path) -> None:
    """Register a package's declared secret names before validating it, so even
    a broken package's secrets are kept away from other processes."""
    try:
        env = json.loads((path / MANIFEST).read_text()).get("env")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return
    if isinstance(env, dict):
        protect_env([n for n, spec in env.items() if isinstance(n, str) and ENV_NAME.match(n)
                     and isinstance(spec, dict) and spec.get("secret") is True],
                    owner=f"implementation:{path.name}")


# -- the derived catalog --------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One implementation as the runtime sees it now. Derived, never stored."""

    id: str
    state: str                    # available | disabled | unmet_requirements | broken | missing
    reason: str | None = None
    package: Package | None = None


class Implementations:
    """Reads the implementations directory and the operator's enablement, and
    says what is usable right now. Holds no state of its own."""

    def __init__(self, directory: str | Path | None, enabled: str | set[str] | frozenset[str] = frozenset()) -> None:
        self.directory = Path(directory) if directory is not None else None
        self.enabled = enabled if enabled == "all" else frozenset(enabled)  # type: ignore[arg-type]
        self.catalog()  # declare credentials before any action can run

    def _is_enabled(self, pid: str) -> bool:
        return self.enabled == "all" or pid in self.enabled

    def catalog(self) -> list[Entry]:
        entries: dict[str, Entry] = {}
        names = sorted(p.name for p in self.directory.iterdir() if p.is_dir()) \
            if self.directory is not None and self.directory.is_dir() else []
        for name in names:
            _protect_declared_secrets(self.directory / name)  # type: ignore[operator]
            try:
                package = load_package(self.directory / name)  # type: ignore[operator]
                owner = f"implementation:{package.id}"
                protect_env(package.secrets, owner=owner)
                if taken := [n for n in package.secrets if env_owner(n) != owner]:
                    raise ImplementationError(f"declares secrets owned elsewhere: {taken}")
            except ImplementationError as exc:
                entries[name] = Entry(name, "broken", str(exc))
                continue
            except OSError as exc:
                entries[name] = Entry(name, "broken", f"unreadable: {exc.strerror or exc}")
                continue
            if not self._is_enabled(package.id):
                entries[name] = Entry(package.id, "disabled", None, package)
            elif missing := [c for c in package.commands if shutil.which(c) is None]:
                entries[name] = Entry(package.id, "unmet_requirements",
                                      f"missing commands: {missing}", package)
            elif absent := [n for n in package.secrets if not os.environ.get(n)]:
                # Names only: a credential's value is never shown anywhere.
                entries[name] = Entry(package.id, "unmet_requirements",
                                      f"missing secrets: {absent}", package)
            else:
                entries[name] = Entry(package.id, "available", None, package)
        if self.enabled != "all":
            for pid in sorted(self.enabled - set(entries)):
                entries[pid] = Entry(pid, "missing", "enabled but not found")
        return [entries[k] for k in sorted(entries)]

    def available(self) -> dict[str, Package]:
        return {e.id: e.package for e in self.catalog() if e.state == "available" and e.package}

    def actions(self, core: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Executable action kinds of the available packages, for the catalogue."""
        actions: dict[str, dict[str, Any]] = {}
        for package in self.available().values():
            for tool in package.tools:
                actions[action_kind(package.id, tool.name)] = {
                    "description": tool.description, "params": tool.params,
                    "implementation": package.id,
                    **({"effects": tool.effects} if tool.effects else {}),
                    **({"idempotency": tool.idempotency} if tool.idempotency else {})}
            if package.checks:
                actions[action_kind(package.id, CHECK)] = {
                    "description": "Run one of this implementation's declared checks; "
                                   "exit 0 means it passed.",
                    "params": check_action_params(package), "implementation": package.id}
        if clash := set(actions) & set(core):
            raise ImplementationError(f"implementation actions collide with core actions: {clash}")
        return actions

    def resolve(self, kind: str) -> tuple[Package, Operation | None]:
        """(package, tool) for an executable kind, (package, None) for its check
        action; otherwise raise saying why not."""
        if not kind.startswith(PREFIX) or not KIND.match(kind):
            raise ImplementationError(f"not an implementation action: {kind!r}")
        pid, name = kind[len(PREFIX):].split(".", 1)
        entry = next((e for e in self.catalog() if e.id == pid), None)
        if entry is None:
            raise ImplementationError(f"implementation {pid!r} does not exist")
        if entry.state != "available" or entry.package is None:
            raise ImplementationError(f"implementation {pid!r} is {entry.state}"
                                      + (f": {entry.reason}" if entry.reason else ""))
        package = entry.package
        if name == CHECK and package.checks:
            return package, None
        tool = next((t for t in package.tools if t.name == name), None)
        if tool is None:
            raise ImplementationError(f"implementation {pid!r} has no tool {name!r}")
        return package, tool

    def view(self) -> list[dict[str, Any]]:
        """What cognition and status are shown: metadata only, plus guidance text
        (bounded when read; the situation bounds it further)."""
        view = []
        for entry in self.catalog():
            item: dict[str, Any] = {"id": entry.id, "state": entry.state, "reason": entry.reason}
            if entry.package is not None:
                p = entry.package
                item.update(description=p.description, version=p.version, digest=p.digest,
                            tools=[t.name for t in p.tools], checks=[c.name for c in p.checks])
                if entry.state == "available" and p.guidance is not None:
                    try:
                        item["guidance"] = p.guidance.read_text(errors="replace")[:GUIDANCE_READ]
                    except OSError:
                        item["guidance"] = None
            view.append(item)
        return view
