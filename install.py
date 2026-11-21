#!/usr/bin/env python3
"""Install/remove the native AGY Codex plugin using AGY's documented directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

SOURCE = Path(__file__).resolve().parent
MARKER = "INSTALLATION.json"
PAYLOAD = ("plugin.json", "README.md", "install.py", "agents", "skills")


def files(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            and p.suffix != ".pyc" and p.name != MARKER}


def _linked(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    if os.name == "nt":
        try:
            tag = getattr(path.lstat(), "st_reparse_tag", 0)
        except FileNotFoundError:
            return False
        return tag in (getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
                       getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C))
    return False


def _owned(target):
    if _linked(target):
        raise ValueError("An existing linked destination was preserved: " + str(target))
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError("An existing file was preserved: " + str(target))
    try:
        if _linked(target/MARKER):
            raise ValueError("Linked ownership marker")
        value = json.loads((target/MARKER).read_text(encoding="utf-8"))
        owned = isinstance(value, dict) and value.get("package") == "agy-codex" and value.get("format") == "antigravity-native-plugin"
    except (OSError, ValueError):
        owned = False
    if not owned:
        raise ValueError("An existing directory was preserved; use its original installer or inspect it first: " + str(target))


def locations(home, project):
    home = Path(home or Path.home()).expanduser().resolve()
    if project:
        project = Path(project).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("Project directory does not exist: " + str(project))
        base = project/".agents"
    else:
        base = home/".gemini"/"config"
    target = base/"plugins"/"agy-codex"
    for path in ((base, target.parent) if project else (home/".gemini", base, target.parent)):
        if _linked(path):
            raise ValueError("An existing linked parent was preserved: " + str(path))
    return target, base/"agy-codex-install-backups"


def install(*, home=None, project=None, uninstall=False):
    target, backup_root = locations(home, project)
    _owned(target)
    result = {"installed": not uninstall, "changed": False, "plugin": str(target),
              "backup": None, "scope": "project" if project else "global"}
    if uninstall:
        if target.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root/uuid.uuid4().hex
            os.replace(target, backup)
            result.update(changed=True, backup=str(backup))
        return result
    manifest = json.loads((SOURCE/"plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != "agy-codex" or not (SOURCE/"agents/agy-codex.md").is_file():
        raise ValueError("Keep install.py beside the supplied native plugin files.")
    # Staging and backups are outside plugins/, so AGY cannot discover a second copy.
    backup_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="stage-", dir=backup_root))
    backup = None
    try:
        for name in PAYLOAD:
            source = SOURCE/name
            if source.is_dir():
                shutil.copytree(source, stage/name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, stage/name)
        expected = files(stage)
        if target.exists() and files(target) == expected:
            return result
        (stage/MARKER).write_text(json.dumps({"package": "agy-codex", "format": "antigravity-native-plugin",
                                            "version": manifest["version"], "files": expected}, indent=2), encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = backup_root/uuid.uuid4().hex
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except OSError:
            if backup:
                os.replace(backup, target)
            raise
        result.update(changed=True, backup=str(backup) if backup else None)
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--global", dest="global_install", action="store_true", help="Default: install for AGY Desktop")
    group.add_argument("--project", help="Install only in an existing project")
    parser.add_argument("--uninstall", action="store_true", help="Move this install outside plugin discovery; preserve task state")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install(project=args.project, uninstall=args.uninstall)
    except (OSError, ValueError) as exc:
        print("操作未完成：" + str(exc))
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("AGY Codex 已移出插件目录，任务记录保留。" if args.uninstall else
              "AGY Codex 已安装。" if result["changed"] else "AGY Codex 已是当前版本。")
        print("插件位置：" + result["plugin"])
        if not args.uninstall:
            print("在 Antigravity 的 Customizations → Installed 查看 AGY Codex，然后选择 agy-codex 主 Agent 和 Gemini 模型。")
        if result["backup"]:
            print("原文件完整保留在：" + result["backup"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
