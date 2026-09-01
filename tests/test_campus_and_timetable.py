from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app
import choose_course
import config
import course_list
import database
import logic
from campus import CAMPUS_OPTIONS
from database import DatabaseManager
from services import auth_service, cart_service, enroll_service
from services.timetable_service import build_timetable

client = TestClient(app.app)


class JsonResponse:
    status_code = 200
    text = "mocked"
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


def test_supported_campus_dictionary_matches_school_options():
    assert [(item.code, item.name) for item in CAMPUS_OPTIONS] == [
        ("01", "粤海校区"),
        ("02", "丽湖校区"),
        ("03", "深大附属医院"),
        ("04", "技术大学"),
        ("05", "香港校区"),
        ("06", "深理光明校区"),
    ]


def test_batch_response_carries_student_default_campus(monkeypatch):
    monkeypatch.setattr(
        logic.requests,
        "post",
        lambda *_args, **_kwargs: JsonResponse(
            {
                "code": "1",
                "data": {
                    "campus": "02",
                    "campusName": "丽湖校区",
                    "electiveBatch": {"code": "B1", "typeName": "正选"},
                },
            }
        ),
    )

    result = logic.fetch_elective_batch("2024110122", "token", "cookie")

    assert tuple(result) == ("B1", "正选")
    assert result.campus_code == "02"
    assert result.campus_name == "丽湖校区"


def test_manual_login_adopts_school_campus_but_later_refresh_preserves_switch(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "campus_code", "01")
    monkeypatch.setattr(config, "campus_name", "粤海校区")
    monkeypatch.setattr(
        logic,
        "fetch_elective_batch",
        lambda *_args: logic.ElectiveBatchResult("B1", "正选", "02", "丽湖校区"),
    )

    auth_service.refresh_elective_batch("2024110122", "token", True)
    assert auth_service.get_session_snapshot()["campus_code"] == "02"

    auth_service.set_current_campus("05")
    auth_service.refresh_elective_batch("2024110122", "token")
    snapshot = auth_service.get_session_snapshot()
    assert snapshot["campus_code"] == "05"
    assert snapshot["campus_name"] == "香港校区"


def test_old_cart_database_is_migrated_with_safe_default_campus(tmp_path):
    db_path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE courses (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO courses (id, type, name) VALUES ('old-1', 'FANKC', '旧课程')"
        )

    db = DatabaseManager(db_path)
    row = db.get_all_courses()[0]

    assert row["campus_code"] == "01"
    assert row["campus_name"] == "粤海校区"


def test_cart_campus_survives_until_enrollment_submission(tmp_path, monkeypatch):
    db = DatabaseManager(tmp_path / "campus-cart.db")
    monkeypatch.setattr(cart_service, "db", db)
    course = SimpleNamespace(
        id="class-lihu",
        type="FANKC",
        name="丽湖课程",
        campus_code="02",
        campus_name="伪造名称会被规范化",
        is_choose="",
        is_conflict="",
    )
    assert cart_service.add_course(course)["success"]
    row = db.get_all_courses()[0]
    assert row["campus_code"] == "02"
    assert row["campus_name"] == "丽湖校区"

    db.update_course_status(course.id, database.STATUS_IN_PROGRESS)
    captured = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda class_id, course_type, campus_code: (
            captured.append((class_id, course_type, campus_code))
            or JsonResponse({"code": "1", "msg": "添加选课志愿成功"})
        ),
    )
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: [{"teachingClassID": course.id}],
    )
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(config, "delay", 0)

    worker_course = enroll_service.EnrollmentCourse(
        id=row["id"],
        type=row["type"],
        name=row["name"],
        campus_code=row["campus_code"],
        campus_name=row["campus_name"],
    )
    assert enroll_service.grab_courses([worker_course]) == enroll_service.GrabOutcome.COMPLETED
    assert captured == [("class-lihu", "FANKC", "02")]


def test_catalog_and_enrollment_requests_use_selected_campus(monkeypatch):
    captured_catalog = {}
    captured_enrollment = {}

    def fake_catalog_post(url, **kwargs):
        captured_catalog.update(kwargs)
        return JsonResponse(
            {"totalCount": 0, "dataList": [], "msg": "", "code": "1", "timestamp": "0"}
        )

    def fake_enrollment_post(**kwargs):
        captured_enrollment.update(kwargs)
        return JsonResponse({"code": "1"})

    monkeypatch.setattr(config, "campus_code", "02")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "B1")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(course_list.requests, "post", fake_catalog_post)

    course_list.programmed_course(0)
    query = json.loads(captured_catalog["data"]["querySetting"])
    assert query["data"]["campus"] == "02"

    monkeypatch.setattr(choose_course.requests, "post", fake_enrollment_post)
    choose_course.submit_course_selection("10001", "FANKC", "02")
    assert '"campus":"02"' in captured_enrollment["data"]["addParam"]


def test_campus_switch_api_returns_canonical_session(monkeypatch):
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "campus_code", "01")
    monkeypatch.setattr(config, "campus_name", "粤海校区")

    response = client.post("/api/session/campus", json={"campus_code": "06"})

    assert response.status_code == 200
    assert response.json()["campus_code"] == "06"
    assert response.json()["campus_name"] == "深理光明校区"
    assert len(response.json()["campus_options"]) == 6


def test_timetable_maps_standard_schedule_and_keeps_nonstandard_courses():
    timetable = build_timetable(
        [
            {
                "teaching_class_id": "A",
                "course_name": "数据库系统",
                "teacher_name": "陈老师",
                "teaching_place": "1-18周 星期三 3-4节 致信楼S308",
            },
            {
                "teaching_class_id": "B",
                "course_name": "课程设计",
                "teacher_name": "林老师",
                "teaching_place": "1-8周 星期一 11-12节 H201\n9-16周 星期五 11-12节 H202",
            },
            {
                "teaching_class_id": "C",
                "course_name": "实习",
                "teacher_name": "王老师",
                "teaching_place": "",
            },
            {
                "teaching_class_id": "D",
                "course_name": "讲座",
                "teacher_name": "李老师",
                "teaching_place": "时间另行通知",
            },
        ]
    )

    assert timetable["total_count"] == 4
    assert timetable["scheduled_count"] == 2
    assert timetable["unscheduled_count"] == 2
    assert len(timetable["entries"]) == 3
    database_entry = next(item for item in timetable["entries"] if item["course_id"] == "A")
    assert database_entry["day"] == 3
    assert database_entry["start_period"] == 3
    assert database_entry["end_period"] == 4
    assert database_entry["weeks"] == "1-18周"
    assert database_entry["location"] == "致信楼S308"
    reasons = {item["course_name"]: item["reason"] for item in timetable["unscheduled"]}
    assert reasons["实习"] == "暂未安排上课时间"
    assert reasons["讲座"] == "未提供具体上课节次"


def test_timetable_places_multiple_weekdays_but_rejects_out_of_range_periods():
    timetable = build_timetable(
        [
            {
                "teaching_class_id": "A",
                "course_name": "含混课程",
                "teaching_place": "1-18周 星期一、星期三 3-4节 H101",
            },
            {
                "teaching_class_id": "B",
                "course_name": "异常节次",
                "teaching_place": "1-18周 星期五 14-15节 H102",
            },
        ]
    )

    assert [
        (item["day"], item["start_period"], item["end_period"]) for item in timetable["entries"]
    ] == [
        (1, 3, 4),
        (3, 3, 4),
    ]
    assert timetable["scheduled_count"] == 1
    assert timetable["unscheduled_count"] == 1
    assert timetable["unscheduled"][0]["course_name"] == "异常节次"
    assert "1-14" in timetable["unscheduled"][0]["reason"]


def test_timetable_splits_comma_joined_schedules_and_ignores_week_rules():
    timetable = build_timetable(
        [
            {
                "teaching_class_id": "graphics",
                "course_name": "计算机图形学",
                "teaching_place": (
                    "1-18周 星期二 7-8节 致理楼L1-509,1-18周 星期二 9-10节 致腾楼241"
                ),
            },
            {
                "teaching_class_id": "database",
                "course_name": "数据库系统",
                "teaching_place": (
                    "1-18周 星期二 1-2节 致理楼L3-709,1-18周 星期二 3-4节 致腾楼241"
                ),
            },
            {
                "teaching_class_id": "architecture",
                "course_name": "计算机系统（3）-组成设计",
                "teaching_place": (
                    "1-18周 星期二 1-2节 致理楼L1-603,1-17周(单) 星期三 3-4节 致腾楼326"
                ),
            },
            {
                "teaching_class_id": "robot",
                "course_name": "微处理器与机器人",
                "teaching_place": (
                    "1-18周 星期一 11-12节 致腾楼327,1-18周 星期一 13-13节 致腾楼327"
                ),
            },
            {
                "teaching_class_id": "engineering",
                "course_name": "软件工程",
                "teaching_place": (
                    "1-18周 星期五 1-2节 致理楼L1-703,1-18周 星期五 3-4节 致腾楼324"
                ),
            },
            {
                "teaching_class_id": "security",
                "course_name": "计算机安全导论",
                "teaching_place": (
                    "1-18周 星期一 7-8节 致理楼L1-303,2-18周(双) 星期一 9-10节 致腾楼318"
                ),
            },
        ]
    )

    assert timetable["total_count"] == 6
    assert timetable["scheduled_count"] == 6
    assert timetable["unscheduled_count"] == 0
    assert len(timetable["entries"]) == 12

    security_entries = [item for item in timetable["entries"] if item["course_id"] == "security"]
    assert [
        (item["day"], item["start_period"], item["end_period"], item["weeks"])
        for item in security_entries
    ] == [
        (1, 7, 8, "1-18周"),
        (1, 9, 10, "2-18周(双)"),
    ]

    architecture_entries = [
        item for item in timetable["entries"] if item["course_id"] == "architecture"
    ]
    assert [item["day"] for item in architecture_entries] == [2, 3]
    assert architecture_entries[1]["weeks"] == "1-17周(单)"
