#!/usr/bin/env python3
"""Readable status and environment checks for the native AGY Codex plugin."""
import argparse
import json
import subprocess
import sys

from controller import Controller, WorkflowError, doctor
from product import active_id, report
from plans import load_plan, plan_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status")
    status.add_argument("--ticket")
    status.add_argument("--json", action="store_true")
    models = commands.add_parser("models")
    models.add_argument("--json", action="store_true")
    checks = commands.add_parser("doctor")
    checks.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            result = doctor(args.repo)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("执行环境检查通过。" if result["static_checks_passed"] else "执行环境需要处理：")
                print("Python: " + result["python_executable"])
                print("Git: " + (result["git_executable"] or "未找到"))
                for key, passed in result["checks"].items():
                    print(f"  {'通过' if passed else '未通过'}  {key}")
                if result.get("git_error"):
                    print("Git 诊断：" + result["git_error"])
                print("真实执行与会话恢复需由独立 smoke probe 验证。")
            return 0 if result["static_checks_passed"] else 2
        c = Controller(args.repo)
        if args.action == "models":
            from routing import probe
            print(json.dumps(probe(c), ensure_ascii=False, indent=2))
            return 0
        ticket_id = active_id(c, args.ticket)
        plan = load_plan(c) if args.ticket is None else None
        result = plan_report(c, plan) if plan else report(c, c.load(ticket_id) if ticket_id else None)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["message"])
            if result.get("summary"):
                print(result["summary"])
            if result.get("next_action"):
                print(result["next_action"])
            progress = result.get("progress")
            if progress and progress.get("running"):
                stage = {"worker": "Codex 执行", "validation": "独立验收", "evidence": "整理复审证据"}.get(progress['stage'], progress['stage'])
                print(f"最近记录的进度（{progress['updated_at']}）：{stage}，已用 {progress['elapsed_seconds']} 秒；"
                      f"命令 {progress['commands_completed']} 条，变更文件 {progress['changed_file_count']} 个。")
        return 0
    except (WorkflowError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if args.json:
            print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        else:
            print("无法完成检查：" + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
