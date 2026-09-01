from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import config
import database
from database import DatabaseManager
from services import cart_service, enroll_service


def _course(**overrides):
    values = {
        "id": "class-1",
        "type": "FANKC",
        "name": "数据库系统 (陈老师)",
        "is_choose": "",
        "is_conflict": "",
        "is_full": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cart_blocks_chosen_and_conflicting_but_allows_full(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "cart.db"))
    monkeypatch.setattr(cart_service, "db", db)

    assert not cart_service.add_course(_course(is_choose="1"))["success"]
    assert not cart_service.add_course(_course(is_conflict="1"))["success"]
    assert cart_service.add_course(_course(is_full="1"))["success"]
    assert db.get_courses_by_status(database.STATUS_NOT_STARTED)[0]["id"] == "class-1"


def test_interrupted_enrollment_rows_can_be_recovered(tmp_path):
    db = DatabaseManager(str(tmp_path / "recovery.db"))
    course = _course(id="stale")
    assert db.add_course(course)
    assert db.update_course_status(course.id, database.STATUS_IN_PROGRESS)

    assert db.recover_interrupted_courses() == 1
    assert db.get_courses_by_status(database.STATUS_NOT_STARTED)[0]["id"] == "stale"


class FakeResponse:
    status_code = 200
    text = "添加选课志愿成功"

    def json(self):
        return {"code": "1"}


def test_grab_courses_uses_existing_request_function_and_marks_success(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "enroll.db"))
    monkeypatch.setattr(cart_service, "db", db)
    course = _course()
    assert cart_service.add_course(course)["success"]
    cart_service.update_status(course.id, database.STATUS_IN_PROGRESS)

    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda class_id, course_type, campus: (
            calls.append((class_id, course_type, campus)) or FakeResponse()
        ),
    )
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: [{"teachingClassID": course.id}],
    )
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(config, "delay", 0)

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.COMPLETED
    assert calls == [("class-1", "FANKC", "01")]
    assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == "class-1"


def test_teaching_place_is_persisted_and_retrieved(tmp_path, monkeypatch):
    """The cart stores teaching_place so the weekly schedule can show pending courses."""
    db = DatabaseManager(str(tmp_path / "place.db"))
    monkeypatch.setattr(cart_service, "db", db)

    course = _course(
        id="place-1",
        teaching_place="1-18周 星期三 9-10节 汇文楼H2-303",
        course_name="数据库系统",
        teacher_name="陈老师",
    )
    assert cart_service.add_course(course)["success"]

    rows = db.get_courses_by_status(database.STATUS_NOT_STARTED)
    assert len(rows) == 1
    assert rows[0]["teaching_place"] == "1-18周 星期三 9-10节 汇文楼H2-303"
    assert rows[0]["course_name"] == "数据库系统"
    assert rows[0]["teacher_name"] == "陈老师"

    # Re-adding with different values updates the stored data
    updated = _course(
        id="place-1",
        teaching_place="2-4周 星期五 11-12节 汇文楼H2-202",
        course_name="高等数学",
        teacher_name="王老师",
    )
    assert cart_service.add_course(updated)["success"]
    rows = db.get_courses_by_status(database.STATUS_NOT_STARTED)
    assert rows[0]["teaching_place"] == "2-4周 星期五 11-12节 汇文楼H2-202"
    assert rows[0]["course_name"] == "高等数学"
    assert rows[0]["teacher_name"] == "王老师"


def test_existing_db_migrates_teaching_place_column(tmp_path):
    """A database created before teaching_place existed gets the column added."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Create a legacy table without teaching_place
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.execute(
            "CREATE TABLE courses (id TEXT PRIMARY KEY, type TEXT, name TEXT, "
            "status TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO courses (id, type, name, status, created_at, updated_at) "
            "VALUES ('legacy-1', 'FANKC', '旧课程', 'PENDING', '2025-01-01', '2025-01-01')"
        )

    # Opening a DatabaseManager should migrate the table
    db = DatabaseManager(str(db_path))
    rows = db.get_courses_by_status("")
    assert len(rows) == 1
    assert rows[0]["teaching_place"] == ""
    assert rows[0]["course_name"] == ""
    assert rows[0]["teacher_name"] == ""
    assert rows[0]["auto_enabled"] == 1
    assert rows[0]["priority_group"] == "未分组课程"
    assert rows[0]["priority_rank"] == 0
    assert rows[0]["course_number"] == ""
    assert rows[0]["time_signature"] == ""

    # Adding a course with full info works
    db.add_course(
        _course(
            id="legacy-1",
            teaching_place="1-16周 星期一 1-2节 教学楼C201",
            course_name="线性代数",
            teacher_name="李老师",
        )
    )
    rows = db.get_courses_by_status("")
    assert rows[0]["teaching_place"] == "1-16周 星期一 1-2节 教学楼C201"
    assert rows[0]["course_name"] == "线性代数"
    assert rows[0]["teacher_name"] == "李老师"
