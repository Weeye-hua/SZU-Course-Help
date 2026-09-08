"""Terminal entry point for the Shenzhen University course helper."""

from __future__ import annotations

import os
import re
import sys

from logging_config import configure_logging
from security.key_manager import (
    KeyManagementError,
    generate_card_key,
    get_or_create_key_pair,
    get_public_key_fingerprint,
)
from services.data_migration import MigrationResult, migrate_legacy_runtime_data
from study_program import GRADUATE, PROGRAM_ENV, UNDERGRADUATE, current_program, is_graduate

STUDENT_ID_PATTERN = re.compile(r"^\d{6,12}$")
STUDENT_ID_FORMAT_HINT = "纯数字，长度 6 至 12 位"


def safe_input(prompt: str) -> str:
    """Read terminal input while handling Ctrl+C and closed stdin cleanly."""
    try:
        return input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，程序安全退出。")
        raise SystemExit(0) from None


def confirm_input(prompt: str) -> bool:
    """Read a strict yes/no answer."""
    while True:
        choice = safe_input(prompt).lower()
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("输入无效，请输入 Y 或 N。")


def print_separator(char: str = "=", width: int = 64) -> None:
    print(char * width)


def print_banner() -> None:
    print_separator()
    print("  深大抢课助手")
    print("  本地 WebUI · 手动首登 · OCR 自动重登 · Card Key V3")
    print("  作者：Weeye · Misakait")
    print_separator()


def read_student_id() -> str:
    """Prompt until a syntactically valid student number is entered."""
    while True:
        student_id = safe_input("\n请输入需要生成卡密的学号: ")
        if STUDENT_ID_PATTERN.fullmatch(student_id):
            return student_id
        print(f"学号格式无效，要求为{STUDENT_ID_FORMAT_HINT}。")


def select_study_program() -> str:
    """Select once before importing services that own school sessions or data."""
    if os.getenv(PROGRAM_ENV, "").strip():
        return current_program()
    while True:
        choice = safe_input("\n请选择选课系统 [1 本科生 / 2 研究生]（默认 1）: ")
        if choice in {"", "1", "2"}:
            program = GRADUATE if choice == "2" else UNDERGRADUATE
            os.environ[PROGRAM_ENV] = program
            return program
        print("请输入 1 或 2。")


def issue_card_key() -> tuple[str, str]:
    """Issue and display one student-bound card key at startup."""
    student_id = read_student_id()
    print("\n正在加载本机签名身份并生成卡密...")
    private_key = get_or_create_key_pair()
    card_key = generate_card_key(student_id, private_key)

    print()
    print_separator()
    print(f"  学号: {student_id}")
    print(f"  密钥指纹: {get_public_key_fingerprint()}")
    print("  卡密:")
    print(f"  {card_key}")
    print()
    print("  卡密与该学号绑定，长期有效；旧版 v2 卡密不再兼容。")
    print_separator()
    return student_id, card_key


def start_course_system(student_id: str, card_key: str) -> None:
    """Start the local-only FastAPI service and prefill the login screen."""
    import logic

    print("\n正在检查 OCR 自动重登录环境...")
    ocr_ready, ocr_message = logic.check_ocr_runtime()
    if ocr_ready:
        print(f"OCR 检查通过: {ocr_message}")
    else:
        print(f"OCR 检查警告: {ocr_message}")
        print("手动登录仍可使用，但学校会话过期后需要再次手动登录。")

    import app

    app.configure_runtime_prefill(student_id, card_key)
    print("\n正在启动本地选课界面...")
    print(f"访问地址: {app.get_login_url()}")
    print(
        "当前模式：研究生；按学校开放时间和选课策略启用抢课。"
        if is_graduate()
        else "当前为预选阶段时，请只浏览和整理课程，不要启动抢课。"
    )
    print("保持本终端窗口开启。\n")
    print_separator("-")
    app.start_server()


def main() -> None:
    """Generate a card key first, then optionally enter the Web UI."""
    configure_logging()
    print_banner()
    select_study_program()
    try:
        migration = MigrationResult() if is_graduate() else migrate_legacy_runtime_data()
    except (OSError, TimeoutError) as exc:
        print(f"\n旧版数据迁移失败: {exc}")
        print("为避免覆盖卡密或课程清单，程序已停止；请检查数据目录权限后重试。")
        raise SystemExit(1) from exc
    if migration.changed:
        print(f"\n已从旧版目录迁移: {migration.source}")
        print(f"迁移内容: {', '.join(migration.migrated)}")
    for warning in migration.warnings:
        print(f"迁移警告: {warning}")
    try:
        student_id, card_key = issue_card_key()
    except (KeyManagementError, OSError, ValueError) as exc:
        print(f"\n卡密生成失败: {exc}")
        raise SystemExit(1) from exc

    if not confirm_input("\n是否进入选课系统？(Y/N): "):
        print("\n已生成卡密，未启动选课系统。")
        return
    start_course_system(student_id, card_key)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n程序已停止。")
        sys.exit(0)
