#!/usr/bin/env python3
"""Reads layout.json + sections.json, clones sources, builds targets,
and writes the generated sections into dist/index.html."""

import html
import json
import os
import posixpath
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

WORKSPACE = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd()))
DIST = WORKSPACE / "dist"
SOURCES = WORKSPACE / "sources"
PLACEHOLDER = "<!-- SECTIONS -->"


def log(msg=""):
    print(msg, flush=True)


def die(msg) -> NoReturn:
    log(f"::error::{msg}")
    sys.exit(1)


def run(cmd, cwd=None, env=None):
    printed = cmd if isinstance(cmd, str) else " ".join(cmd)
    log(f"$ {printed}")
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        die(f"command failed (exit {result.returncode}): {printed}")


# --------------------------------------------------------------------------
# load + validate
# --------------------------------------------------------------------------

def load():
    layout = json.loads((WORKSPACE / "layout.json").read_text())
    sections = json.loads((WORKSPACE / "sections.json").read_text())

    if not isinstance(layout, list):
        die("layout.json must be a list of entries")
    if not isinstance(sections, list):
        die("sections.json must be a list of sections (order matters)")

    section_ids = []
    for i, s in enumerate(sections):
        if "id" not in s or "name" not in s:
            die(f'sections.json[{i}]: needs at least "id" and "name"')
        if s["id"] in section_ids:
            die(f'sections.json[{i}]: duplicate id "{s["id"]}"')
        section_ids.append(s["id"])

    errors = []
    for i, e in enumerate(layout):
        tag = f'layout.json[{i}] ({e.get("source", "<no source>")})'

        if "source" not in e:
            errors.append(f'{tag}: missing "source"')

        if "features" in e and "build_commands" in e:
            errors.append(
                f'{tag}: "features" and "build_commands" are mutually '
                f'exclusive — a custom build owns the whole build')

        if "build_commands" not in e and "pathname" not in e:
            errors.append(f'{tag}: missing "pathname"')

        if "section" in e:
            if e["section"] not in section_ids:
                errors.append(f'{tag}: unknown section "{e["section"]}" '
                              f'(not in sections.json)')
            if not e.get("name"):
                errors.append(f'{tag}: entries with a "section" need a "name"')
            if not (e.get("href") or e.get("pathname")):
                errors.append(f'{tag}: entries with a "section" need a '
                              f'"href" or a "pathname" to link to')

    if errors:
        for err in errors:
            log(f"::error::{err}")
        sys.exit(1)

    return layout, sections


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def clone(repo):
    """repo is 'owner/name'. Returns the local checkout path."""
    srcdir = SOURCES / repo
    if not srcdir.is_dir():
        srcdir.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1",
               f"https://github.com/{repo}", str(srcdir)]
        log(f"$ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(result.stderr.rstrip())
            # git can't prompt for credentials in CI, so a private (or
            # nonexistent) repo shows up as a missing-username error
            haystack = result.stderr.lower()
            if any(s in haystack for s in ("could not read username",
                                           "authentication failed",
                                           "repository not found",
                                           "terminal prompts disabled")):
                die(f"cannot access github.com/{repo} — it is private, or the "
                    f"name in layout.json is wrong. Make the repo public, or "
                    f"add a SOURCES_TOKEN secret with read access to it.")
            die(f"git clone of {repo} failed (exit {result.returncode})")
    return srcdir


def cargo_dir(srcdir):
    """Directory holding Cargo.toml, so `cargo build` just works."""
    if (srcdir / "Cargo.toml").is_file():
        return srcdir
    for candidate in sorted(srcdir.rglob("Cargo.toml")):
        if "target" not in candidate.parts:
            return candidate.parent
    return srcdir


# --------------------------------------------------------------------------
# build kinds
# --------------------------------------------------------------------------

def build_custom(entry, srcdir):
    cmds = entry["build_commands"]
    if isinstance(cmds, list):
        cmds = "\n".join(cmds)

    workdir = cargo_dir(srcdir)
    env = dict(os.environ, SITE=str(DIST), SOURCE_DIR=str(srcdir))

    script = WORKSPACE / ".build_commands.sh"
    script.write_text("set -euo pipefail\n" + cmds + "\n")
    log(f"-- custom build in {workdir} (SITE={DIST})")
    run(["bash", str(script)], cwd=workdir, env=env)
    script.unlink()


def build_wasm(entry, srcdir):
    workdir = cargo_dir(srcdir)
    features = entry.get("features") or []

    cmd = ["cargo", "build", "--target", "wasm32-unknown-unknown", "--release",
           "--message-format", "json-render-diagnostics"]
    if features:
        cmd += ["--features", ",".join(features)]

    # diagnostics go to stderr (straight to the log); artifact paths come back
    # as JSON on stdout, so cargo tells us the filename instead of us guessing
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, text=True)
    if proc.stdout is None:
        die("could not capture cargo output")
    wasm = []
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") == "compiler-artifact":
            wasm += [Path(f) for f in msg.get("filenames", [])
                     if f.endswith(".wasm")]
    if proc.wait() != 0:
        die(f"cargo build failed (exit {proc.returncode}) in {workdir}")

    if not wasm:
        die(f"cargo reported no .wasm artifact in {workdir} — is the crate a "
            f"bin or cdylib target?")
    if len(wasm) > 1:
        log(f"-- note: {len(wasm)} wasm artifacts, using {wasm[-1].name}")
    copy_into_dist(wasm[-1], entry["pathname"])


def copy_file(entry, srcdir, relpath):
    src = srcdir / relpath
    if not src.exists():
        die(f'{entry["source"]}: no such file in repo: {relpath}')
    copy_into_dist(src, entry["pathname"])


def copy_into_dist(src, pathname):
    dest = DIST / pathname
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    log(f"-- {src} -> dist/{pathname}")


def build(layout):
    for entry in layout:
        parts = entry["source"].split("/")
        repo, relpath = "/".join(parts[:2]), "/".join(parts[2:])
        srcdir = clone(repo)

        log(f"\n=== {entry['source']}")
        if "build_commands" in entry:
            build_custom(entry, srcdir)
        elif relpath:
            copy_file(entry, srcdir, relpath)
        else:
            build_wasm(entry, srcdir)


# --------------------------------------------------------------------------
# index.html
# --------------------------------------------------------------------------

def href_for(entry):
    if entry.get("href"):
        return entry["href"]
    pathname = entry["pathname"]
    directory = posixpath.dirname(pathname)
    if posixpath.basename(pathname).startswith("index."):
        return f"/{directory}/" if directory else "/"
    return f"/{pathname}"


def render_sections(layout, sections):
    out = []
    for section in sections:
        cards = [e for e in layout if e.get("section") == section["id"]]
        if not cards:
            log(f'-- section "{section["id"]}" has no entries, skipping')
            continue

        out.append("<section>")
        out.append(f'    <div class="section-title">'
                   f'{html.escape(section["name"])}</div>')
        if section.get("subtitle"):
            out.append(f'    <div class="section-subtitle">'
                       f'{html.escape(section["subtitle"])}</div>')
        for e in cards:
            out.append(f'    <a class="card" href="{html.escape(href_for(e))}">')
            out.append(f'        <div class="card-title">'
                       f'{html.escape(e["name"])}</div>')
            if e.get("description"):
                out.append(f'        <div class="card-desc">'
                           f'{html.escape(e["description"])}</div>')
            out.append("    </a>")
        out.append("</section>")
        out.append("")
    return "\n".join(out).rstrip("\n")


def write_index(layout, sections):
    index = DIST / "index.html"
    if not index.is_file():
        die("dist/index.html missing — was index/ copied into dist?")
    source = index.read_text()
    if PLACEHOLDER not in source:
        die(f"index.html has no {PLACEHOLDER} placeholder to fill")
    index.write_text(source.replace(PLACEHOLDER, render_sections(layout, sections)))
    log("-- wrote dist/index.html")


if __name__ == "__main__":
    layout, sections = load()
    log("layout.json and sections.json look fine")
    build(layout)
    log()
    write_index(layout, sections)