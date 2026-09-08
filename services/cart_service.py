"""
购物车服务模块

职责：
    管理本地课程购物车的增删查操作。
    购物车数据存储在 SQLite 数据库中（course_enroll.db）。

核心约束：
    - 不修改 database.py 中的表结构和状态语义
    - 已选课程（is_choose="1"）禁止加入购物车
    - 与当前课表冲突（is_conflict="1"）的教学班禁止加入购物车
"""

from __future__ import annotations

import hashlib
from contextlib import suppress
from typing import Any

import database
from campus import get_campus
from course_models import priority_group_key, time_signature
from database import DatabaseManager
from project_paths import data_dir
from study_program import is_graduate

# 全局数据库实例
db = DatabaseManager()
db.recover_interrupted_courses()
_graduate_account = ""


def bind_graduate_account(student_id: str) -> None:
    """Keep each graduate account's queue separate, including after restart."""
    global db, _graduate_account
    if not is_graduate() or _graduate_account == student_id:
        return
    digest = hashlib.sha256(student_id.encode("utf-8")).hexdigest()[:24]
    new_db = DatabaseManager(data_dir() / "accounts" / digest / "course_enroll.db")
    new_db.recover_interrupted_courses()
    db.close()
    db = new_db
    _graduate_account = student_id


def add_course(course: Any) -> dict[str, bool | str]:
    """
    添加课程到购物车

    校验规则：
        - is_choose="1" 的课程（已被学号选中的教学班）禁止加入
        - is_conflict="1" 的课程禁止加入
        - 重复添加时更新为 PENDING 状态

    参数：
        course: Cart 对象（需有 id, type, name, is_choose 属性）

    返回：
        {"success": bool, "message": str}
    """
    if getattr(course, "is_choose", "") == "1":
        return {"success": False, "message": "该课程已选，无法加入购物车"}
    if getattr(course, "is_conflict", "") == "1":
        return {"success": False, "message": "该教学班与已选课程冲突，无法加入购物车"}

    if not all(
        isinstance(getattr(course, field, None), str) and getattr(course, field).strip()
        for field in ("id", "type", "name")
    ):
        return {"success": False, "message": "课程信息不完整，无法加入购物车"}

    campus_code = str(getattr(course, "campus_code", "01") or "01").strip()
    if not is_graduate() and get_campus(campus_code) is None:
        return {"success": False, "message": "校区信息无效，无法加入购物车"}
    if is_graduate() and getattr(course, "type", "") not in {"GPN", "GCROSS"}:
        return {
            "success": False,
            "message": "请从研究生方案内或跨专业课程加入清单，开课查询仅供查阅",
        }

    if not getattr(course, "course_number", ""):
        with suppress(AttributeError):
            course.course_number = str(getattr(course, "id", ""))
    if not getattr(course, "time_signature", ""):
        with suppress(AttributeError):
            course.time_signature = time_signature(getattr(course, "teaching_place", ""))
    if not getattr(course, "priority_group", ""):
        with suppress(AttributeError):
            course.priority_group = priority_group_key(
                course_number=getattr(course, "course_number", ""),
                schedule_signature=getattr(course, "time_signature", ""),
                course_id=getattr(course, "id", ""),
            )

    if db.add_course(course):
        return {"success": True, "message": "成功加入购物车"}
    return {"success": False, "message": "加入购物车失败"}


def delete_course(course_id: str) -> dict[str, bool | str]:
    """
    从购物车删除课程

    参数：
        course_id: 教学班 ID

    返回：
        {"success": bool, "message": str}
    """
    course_id = str(course_id or "").strip()
    if course_id and db.delete_course(course_id):
        return {"success": True, "message": "删除成功"}
    return {"success": False, "message": "删除失败或ID不存在"}


def get_courses_by_status(status: str) -> list[dict]:
    """
    按状态查询购物车课程

    参数：
        status: 课程状态（PENDING/ENROLLING/SUCCESS/FAILED，空字符串查全部）

    返回：
        课程字典列表
    """
    return db.get_courses_by_status(status)


def get_all_sorted() -> list[dict]:
    """按创建时间排序获取所有购物车课程"""
    return db.get_all_courses_sorted_by_time()


def get_active_courses() -> list[dict]:
    """获取仍需抢课（PENDING/ENROLLING）的课程，供抢课循环使用。"""
    return db.get_active_courses()


def update_course_preferences(course_id: str, **fields) -> bool:
    """Update safe, user-controlled queue preferences for one course."""
    return db.update_course_preferences(str(course_id or "").strip(), **fields)


def update_course_priorities(updates: list[tuple[str, int]]) -> bool:
    """Atomically update the order of several local queue rows."""
    return db.update_course_priorities(updates)


def update_status(course_id: str, status: str) -> bool:
    """更新课程状态"""
    return db.update_course_status(course_id, status)


def retry_failed_course(course_id: str) -> dict[str, bool | str]:
    """Return one explicitly failed course to the pending queue."""
    normalized_id = str(course_id or "").strip()
    if not normalized_id:
        return {"success": False, "message": "课程 ID 不能为空"}
    row = next(
        (item for item in db.get_courses_by_status("") if item.get("id") == normalized_id),
        None,
    )
    if row is None:
        return {"success": False, "message": "课程不在本地清单中"}
    if row.get("status") != database.STATUS_FAILED:
        return {"success": False, "message": "只有已停止的课程可以重新排队"}
    if db.update_course_status(normalized_id, database.STATUS_NOT_STARTED):
        return {"success": True, "message": "课程已重新加入待抢队列"}
    return {"success": False, "message": "重新排队失败，请稍后重试"}
