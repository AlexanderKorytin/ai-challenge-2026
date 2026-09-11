#!/usr/bin/env python3
"""Synchronize native OMP agents from the canonical Claude definitions."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_SOURCE_DIRECTORY = Path(".claude/agents")
_TARGET_DIRECTORY = Path(".omp/agents")
_GENERATED_FROM = "generated-from"
_REQUIRED_FIELDS = ("name", "description", "tools", "model")
_GENERATED_TOOLS = "tools: read, grep, glob, web_search\n"
_GENERATED_MODEL = 'model: "@architecture"\n'
_GENERATED_MARKER_RE = re.compile(
    rf"^{re.escape(_GENERATED_FROM)}: \.claude/agents/([^/]+\.md)$"
)


def _line_value(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def _read_source(source: Path) -> str:
    try:
        return source.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{source}: source is not valid UTF-8") from error


def _front_matter(text: str, source: Path) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or _line_value(lines[0]) != "---":
        raise ValueError(f"{source}: expected an opening '---' line")

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if _line_value(line) == "---"
        ),
        None,
    )
    if closing_index is None:
        raise ValueError(f"{source}: expected a closing '---' line")

    fields: dict[str, str] = {}
    front_matter = lines[1:closing_index]
    for field in _REQUIRED_FIELDS:
        matches = [line for line in front_matter if _line_value(line).startswith(f"{field}:")]
        if len(matches) != 1:
            raise ValueError(
                f"{source}: expected exactly one '{field}' field, found {len(matches)}"
            )
        fields[field] = matches[0]

    return fields, "".join(lines[closing_index + 1 :])


def _description_line(field: str, source: Path) -> str:
    encoded = _line_value(field).removeprefix("description:")
    try:
        description = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{source}: 'description' must be a valid double-quoted JSON string"
        ) from error
    if not isinstance(description, str):
        raise ValueError(
            f"{source}: 'description' must be a double-quoted JSON string"
        )
    return f"description: {json.dumps(description, ensure_ascii=False)}\n"


def build_agent(source: Path) -> str:
    """Build one native OMP definition from a canonical Claude definition."""
    fields, body = _front_matter(_read_source(source), source)
    marker = f"{_GENERATED_FROM}: .claude/agents/{source.name}\n"
    return (
        "---\n"
        + fields["name"]
        + _description_line(fields["description"], source)
        + _GENERATED_TOOLS
        + _GENERATED_MODEL
        + marker
        + "---\n"
        + body
    )


def _source_files(root: Path) -> list[Path]:
    source_directory = root / _SOURCE_DIRECTORY
    if not source_directory.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source_directory}")
    return sorted(source_directory.glob("*.md"), key=lambda path: path.name)


def _expected_agents(root: Path) -> dict[str, str]:
    return {source.name: build_agent(source) for source in _source_files(root)}

def _target_directory(root: Path, *, create: bool) -> Path:
    target_directory = root / _TARGET_DIRECTORY
    if target_directory.is_symlink():
        raise ValueError(
            f"{target_directory}: refusing to use a symbolic link as target directory"
        )
    if create and not target_directory.exists():
        target_directory.mkdir(parents=True)
    if target_directory.is_symlink():
        raise ValueError(
            f"{target_directory}: refusing to use a symbolic link as target directory"
        )
    if not target_directory.is_dir():
        raise ValueError(f"{target_directory}: target path is not a directory")
    return target_directory


def _write_atomically(destination: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        temporary_path.replace(destination)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _generation_source_name(path: Path) -> str | None:
    if path.is_symlink():
        return None

    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    if not lines or lines[0] != "---":
        return None
    try:
        closing_index = lines.index("---", 1)
    except ValueError:
        return None

    markers = [
        match
        for line in lines[1:closing_index]
        if (match := _GENERATED_MARKER_RE.fullmatch(line)) is not None
    ]
    if len(markers) != 1:
        return None
    return markers[0].group(1)


def synchronize(root: Path) -> None:
    """Create or update expected mirrors and remove only stale generated mirrors."""
    target_directory = _target_directory(root, create=True)
    expected = _expected_agents(root)

    for name, content in expected.items():
        destination = target_directory / name
        encoded = content.encode("utf-8")
        if destination.is_symlink():
            raise ValueError(f"{destination}: refusing to overwrite a symbolic link")
        if destination.exists():
            if destination.read_bytes() == encoded:
                continue
            if _generation_source_name(destination) != destination.name:
                raise ValueError(f"{destination}: refusing to overwrite an unmanaged file")
        _write_atomically(destination, encoded)

    for destination in sorted(target_directory.glob("*.md"), key=lambda path: path.name):
        if (
            destination.name not in expected
            and not destination.is_symlink()
            and _generation_source_name(destination) == destination.name
        ):
            destination.unlink()


def check(root: Path) -> list[str]:
    """Return deterministic descriptions of missing, changed, and extra mirrors."""
    target_directory = _target_directory(root, create=False)
    expected = _expected_agents(root)
    actual = {
        path.name: path
        for path in sorted(target_directory.glob("*.md"), key=lambda path: path.name)
    }
    problems: list[str] = []

    for name, content in expected.items():
        destination = actual.get(name)
        relative = (_TARGET_DIRECTORY / name).as_posix()
        if destination is None:
            problems.append(f"missing: {relative}")
        elif destination.read_bytes() != content.encode("utf-8"):
            problems.append(f"different: {relative}")

    for name in sorted(actual.keys() - expected.keys()):
        problems.append(f"extra: {(_TARGET_DIRECTORY / name).as_posix()}")

    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested synchronization mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write generated definitions")
    mode.add_argument("--check", action="store_true", help="report synchronization drift")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]

    try:
        if arguments.write:
            synchronize(root)
            return 0

        problems = check(root)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
