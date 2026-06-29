#!/usr/bin/env python3

# Copyright (c) 2022-2026, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

"""Patch a uv virtualenv activation script for Isaac Sim runtime loading.

Run this after installing dependencies into the uv environment, so the script can
resolve the torch-bundled libgomp path and preload it before Isaac Sim starts.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


BEGIN_MARKER = "# >>> Isaac Lab uv runtime fixes >>>"
END_MARKER = "# <<< Isaac Lab uv runtime fixes <<<"
DEACTIVATE_BEGIN_MARKER = "# >>> Isaac Lab uv deactivate cleanup >>>"
DEACTIVATE_END_MARKER = "# <<< Isaac Lab uv deactivate cleanup <<<"

ENV_VARS_TO_RESTORE = (
    "ISAACLAB_PATH",
    "RESOURCE_NAME",
    "CARB_APP_PATH",
    "EXP_PATH",
    "ISAAC_PATH",
    "PYTHONPATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
)

SHELL_VARS_TO_RESTORE = (
    "SCRIPT_DIR",
    "MY_DIR",
)


def _repo_root() -> Path:
    script_path = Path(__file__).resolve()
    if script_path.parent.name == "tools" and script_path.parent.parent.name == "scripts":
        return script_path.parents[2]
    return script_path.parent


def _find_torch_libgomp(env_path: Path) -> Path | None:
    matches = sorted((env_path / "lib").glob("python*/site-packages/torch/lib/libgomp*.so*"))
    return matches[-1] if matches else None


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _remove_marked_blocks(text: str, begin_marker: str, end_marker: str) -> str:
    while True:
        begin = text.find(begin_marker)
        if begin == -1:
            break

        end = text.find(end_marker, begin)
        if end == -1:
            raise RuntimeError(f"Found '{begin_marker}' without matching '{end_marker}'.")

        end = text.find("\n", end)
        if end == -1:
            end = len(text)
        else:
            end += 1
        text = text[:begin].rstrip() + "\n" + text[end:].lstrip("\n")

    return text


def _remove_existing_block(text: str) -> str:
    text = _remove_marked_blocks(text, BEGIN_MARKER, END_MARKER)
    text = _remove_marked_blocks(text, DEACTIVATE_BEGIN_MARKER, DEACTIVATE_END_MARKER)

    legacy_begin = text.find("_isaaclab_prepend_ld_path () {")
    if legacy_begin != -1:
        legacy_end_marker = "unset _isaaclab_extscache _isaaclab_ext_dir _isaaclab_torch_gomp"
        legacy_end = text.find(legacy_end_marker, legacy_begin)
        if legacy_end != -1:
            legacy_end = text.find("\n", legacy_end)
            if legacy_end == -1:
                legacy_end = len(text)
            else:
                legacy_end += 1
            text = text[:legacy_begin].rstrip() + "\n" + text[legacy_end:].lstrip("\n")

    return text.rstrip() + "\n"


def _save_var_block(var_name: str) -> str:
    old_var_name = f"_OLD_ISAACLAB_{var_name}"
    value_ref = f"${var_name}"
    return (
        f"unset {old_var_name} {old_var_name}_SET\n"
        f'if ! [ -z "${{{var_name}+_}}" ]; then\n'
        f'    {old_var_name}="{value_ref}"\n'
        f"    {old_var_name}_SET=1\n"
        "fi"
    )


def _restore_var_block(var_name: str, *, export: bool, indent: str = "    ") -> str:
    old_var_name = f"_OLD_ISAACLAB_{var_name}"
    export_line = f"{indent}    export {var_name}\n" if export else ""
    return (
        f'{indent}if ! [ -z "${{{old_var_name}_SET+_}}" ] ; then\n'
        f'{indent}    {var_name}="${{{old_var_name}}}"\n'
        f"{export_line}"
        f"{indent}else\n"
        f"{indent}    unset {var_name}\n"
        f"{indent}fi\n"
        f"{indent}unset {old_var_name} {old_var_name}_SET"
    )


def _saved_state_block() -> str:
    save_env = "\n".join(_save_var_block(var_name) for var_name in ENV_VARS_TO_RESTORE)
    save_shell = "\n".join(_save_var_block(var_name) for var_name in SHELL_VARS_TO_RESTORE)
    return f"""
_OLD_ISAACLAB_UV_ACTIVE=1
{save_env}
{save_shell}
unset _OLD_ISAACLAB_ALIAS_ISAACLAB _OLD_ISAACLAB_ALIAS_ISAACLAB_SET
if alias isaaclab >/dev/null 2>&1; then
    _OLD_ISAACLAB_ALIAS_ISAACLAB="$(alias isaaclab)"
    _OLD_ISAACLAB_ALIAS_ISAACLAB_SET=1
fi
"""


def _deactivate_cleanup_block() -> str:
    restore_env = "\n".join(
        _restore_var_block(var_name, export=True, indent="        ") for var_name in ENV_VARS_TO_RESTORE
    )
    restore_shell = "\n".join(
        _restore_var_block(var_name, export=False, indent="        ") for var_name in SHELL_VARS_TO_RESTORE
    )
    return f"""    {DEACTIVATE_BEGIN_MARKER}
    if ! [ -z "${{_OLD_ISAACLAB_UV_ACTIVE+_}}" ] ; then
        if ! [ -z "${{_OLD_ISAACLAB_ALIAS_ISAACLAB_SET+_}}" ] ; then
            eval "$_OLD_ISAACLAB_ALIAS_ISAACLAB"
        else
            unalias isaaclab >/dev/null 2>&1 || true
        fi
        unset _OLD_ISAACLAB_ALIAS_ISAACLAB _OLD_ISAACLAB_ALIAS_ISAACLAB_SET

{restore_env}
{restore_shell}
        unset _OLD_ISAACLAB_UV_ACTIVE
    fi
    {DEACTIVATE_END_MARKER}
"""


def _insert_deactivate_cleanup(text: str) -> str:
    deactivate_start = text.find("deactivate () {\n")
    if deactivate_start == -1:
        raise RuntimeError("Could not find the deactivate function in the activation script.")

    insert_at = deactivate_start + len("deactivate () {\n")
    if text.startswith("unset -f pydoc", insert_at):
        text = text[:insert_at] + "    " + text[insert_at:]
    return text[:insert_at] + _deactivate_cleanup_block() + text[insert_at:]


def _runtime_block(isaaclab_relpath: str, torch_libgomp_relpath: str | None) -> str:
    torch_libgomp_value = (
        f'"${{VIRTUAL_ENV}}"/{_shell_quote(torch_libgomp_relpath)}' if torch_libgomp_relpath else '""'
    )
    saved_state = _saved_state_block()
    return f"""
{BEGIN_MARKER}
{saved_state}
_isaaclab_prepend_ld_path () {{
    [ -d "$1" ] || return 0
    case ":${{LD_LIBRARY_PATH:-}}:" in
        *":$1:"*) ;;
        *) LD_LIBRARY_PATH="$1${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}" ;;
    esac
}}

_isaaclab_newest_ext_dir () {{
    find "$_isaaclab_extscache" -maxdepth 2 -type d -path "$_isaaclab_extscache/$1" 2>/dev/null | sort -V | tail -n 1
}}

_isaaclab_set_path () {{
    if [ -n "${{BASH_VERSION:+x}}" ]; then
        _isaaclab_activate_path="${{BASH_SOURCE[0]}}"
    elif [ -n "${{ZSH_VERSION:+x}}" ]; then
        _isaaclab_activate_path="${{(%):-%x}}"
    elif [ -n "${{KSH_VERSION:+x}}" ]; then
        _isaaclab_activate_path="${{.sh.file}}"
    else
        _isaaclab_activate_path="${{VIRTUAL_ENV}}/bin/activate"
    fi

    _isaaclab_activate_dir="$(CDPATH= cd "$(dirname "$_isaaclab_activate_path")" >/dev/null && pwd -P)" || return 0
    _isaaclab_path="$(CDPATH= cd "$_isaaclab_activate_dir/$_isaaclab_relpath" >/dev/null && pwd -P)" && return 0

    if [ -n "${{ISAACLAB_PATH:-}}" ]; then
        _isaaclab_path="${{ISAACLAB_PATH}}"
    fi
}}

_isaaclab_relpath={_shell_quote(isaaclab_relpath)}
_isaaclab_path=""
_isaaclab_set_path

if [ -n "$_isaaclab_path" ]; then
    export ISAACLAB_PATH="$_isaaclab_path"
    alias isaaclab="$_isaaclab_path/isaaclab.sh"
    export RESOURCE_NAME="IsaacSim"

    _isaaclab_setup_conda_env_script="$_isaaclab_path/_isaac_sim/setup_conda_env.sh"
    if [ -f "$_isaaclab_setup_conda_env_script" ]; then
        . "$_isaaclab_setup_conda_env_script"
    fi
fi

if [ -d "${{ISAAC_PATH:-}}/extscache" ]; then
    _isaaclab_extscache="${{ISAAC_PATH}}/extscache"
elif [ -n "$_isaaclab_path" ] && [ -d "$_isaaclab_path/_isaac_sim/extscache" ]; then
    _isaaclab_extscache="$_isaaclab_path/_isaac_sim/extscache"
elif [ -d "${{HOME}}/isaacsim/extscache" ]; then
    _isaaclab_extscache="${{HOME}}/isaacsim/extscache"
else
    _isaaclab_extscache=""
fi

if [ -n "$_isaaclab_extscache" ]; then
    for _isaaclab_ext_dir in \\
        "$(_isaaclab_newest_ext_dir "omni.usd.schema.audio-*/lib")" \\
        "$(_isaaclab_newest_ext_dir "omni.usd.libs-*/bin")" \\
        "$(_isaaclab_newest_ext_dir "omni.usd.core-*/bin")"
    do
        _isaaclab_prepend_ld_path "$_isaaclab_ext_dir"
    done
    export LD_LIBRARY_PATH
fi

_isaaclab_torch_gomp={torch_libgomp_value}
if [ -z "$_isaaclab_torch_gomp" ]; then
    _isaaclab_torch_gomp="$(find "${{VIRTUAL_ENV}}/lib" -path "*/site-packages/torch/lib/libgomp*.so*" -type f 2>/dev/null | sort -V | tail -n 1)"
fi
if [ -n "$_isaaclab_torch_gomp" ]; then
    case ":${{LD_PRELOAD:-}}:" in
        *":$_isaaclab_torch_gomp:"*) ;;
        *) export LD_PRELOAD="$_isaaclab_torch_gomp${{LD_PRELOAD:+:$LD_PRELOAD}}" ;;
    esac
fi

unset -f _isaaclab_prepend_ld_path _isaaclab_newest_ext_dir _isaaclab_set_path
unset _isaaclab_activate_dir _isaaclab_activate_path _isaaclab_extscache _isaaclab_ext_dir
unset _isaaclab_path _isaaclab_relpath _isaaclab_setup_conda_env_script _isaaclab_torch_gomp
{END_MARKER}
"""


def patch_activate(env_path: Path, isaaclab_path: Path) -> Path:
    activate_path = env_path / "bin" / "activate"
    if not activate_path.is_file():
        raise FileNotFoundError(f"Activation script not found: {activate_path}")

    text = activate_path.read_text()
    text = _remove_existing_block(text)
    text = _insert_deactivate_cleanup(text)

    torch_libgomp = _find_torch_libgomp(env_path)
    isaaclab_relpath = os.path.relpath(isaaclab_path, activate_path.parent).replace(os.sep, "/")
    torch_libgomp_relpath = (
        torch_libgomp.relative_to(env_path).as_posix()
        if torch_libgomp is not None and torch_libgomp.is_relative_to(env_path)
        else None
    )
    text = text.rstrip() + "\n" + _runtime_block(isaaclab_relpath, torch_libgomp_relpath).lstrip()
    activate_path.write_text(text)
    return activate_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch uv venv activate script for Isaac Lab runtime fixes.")
    parser.add_argument(
        "env_path",
        nargs="?",
        default=".venv",
        help="Path to the uv virtual environment. Defaults to '.venv'.",
    )
    parser.add_argument(
        "--isaaclab-path",
        default=str(_repo_root()),
        help="Isaac Lab repository path. Defaults to this script's repository root.",
    )
    args = parser.parse_args()

    env_path = Path(args.env_path).expanduser()
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    isaaclab_path = Path(args.isaaclab_path).expanduser().resolve()

    activate_path = patch_activate(env_path.resolve(), isaaclab_path)
    torch_libgomp = _find_torch_libgomp(env_path.resolve())
    print(f"[INFO] Patched uv activation script: {activate_path}")
    if torch_libgomp is None:
        print("[WARN] torch libgomp was not found yet. Re-run this script after installing torch.")
    else:
        print(f"[INFO] Using torch libgomp: {torch_libgomp}")


if __name__ == "__main__":
    main()
