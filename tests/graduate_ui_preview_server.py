"""Graduate UI preview with synthetic data and all outbound HTTP blocked."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["COURSE_SELECT_PROGRAM"] = "graduate"
os.environ.setdefault("COURSE_SELECT_PORT", "8013")
os.environ["COURSE_SELECT_DATA_DIR"] = str(ROOT / "tmp" / "graduate-ui-preview")
os.environ["COURSE_SELECT_NO_BROWSER"] = "1"

import requests  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import app  # noqa: E402
import config  # noqa: E402
from services import graduate_service as graduate  # noqa: E402


def blocked_request(*args, **kwargs):
    raise AssertionError("UI preview blocks every external HTTP request")


requests.sessions.Session.request = blocked_request
window = graduate.parse_window(
    {
        "lcxx": {
            "WID": "preview-round",
            "MC": "研究生选课 · 本地预览",
            "XNXQDM": "20261",
            "XKCL": 0,
            "KFKSSJ": "2026-09-01 12:00:00",
            "KFJSSJ": "2026-09-30 12:00:00",
        },
        "xksfkf": 1,
        "dqsj": "2026-09-08 12:00:00",
    }
)
graduate.publish_window(window)
config.student_id = "26********"
config.token = "preview"
config.combined_cookie = "sid=preview"
config.elective_batch_code = "preview-round"
config.elective_batch_name = window.batch_name
app.refresh_elective_batch = lambda *args, **kwargs: config.elective_batch_name
ROWS = [
    {
        "BJDM": f"preview-{index}",
        "KCDM": f"CS{index}",
        "KCMC": name,
        "KCXF": 3,
        "XF": 3,
        "KXRS": 30,
        "DQRS": 25 if index % 2 else 30,
        "IS_CONFLICT": 0,
        "RKJS": "示例教师",
        "PKSJDDMS": "3-14周 星期五[3-4节]教学楼101",
        "RWKKDWMC": "计算机与软件学院",
    }
    for index, name in enumerate(
        [
            "高级计算机网络与分布式系统",
            "研究生学术写作",
            "数学方法",
            "高级算法",
            "机器学习",
            "工程伦理",
            "实验方法",
            "学术研讨",
            "数据分析",
            "智能系统",
            "控制理论",
            "项目实践",
        ]
    )
]


def fake_request(method, path, *, data=None, **kwargs):
    if path == "xsxkCourse/loadStdCourseInfo.do":
        payload = {"results": [ROWS[0], {"BJDM": "untimed", "KCMC": "学术实践", "XF": 1}]}
    elif path == "xsxkCourse/loadKbxx.do":
        payload = {
            "results": [
                {
                    "BJDM": "preview-0",
                    "XQ": 5,
                    "KSJCDM": 3,
                    "JSJCDM": 4,
                    "ZCMC": "3-14周",
                    "JASMC": "教学楼101",
                }
            ]
        }
    elif path == "xsxkHome/loadDwXb.do":
        payload = {"dwxb": [{"DM": "CS", "MC": "计算机与软件学院"}]}
    elif path in {"xsxkCourse/" + value[1] for value in graduate.CATEGORIES.values()}:
        rows = [] if path.endswith("loadWzyCourseInfo.do") else ROWS
        keyword = str((data or {}).get("query_keyword") or "")
        rows = [row for row in rows if keyword in row["KCMC"]]
        if data.get("query_sfym") == "0":
            rows = [row for row in rows if row["DQRS"] < row["KXRS"]]
        page = int(data["pageIndex"])
        payload = {
            "datas": rows[(page - 1) * 10 : page * 10],
            "total": len(rows),
            "pageIndex": page,
            "pageSize": 10,
        }
    else:
        raise AssertionError("Preview prohibits this endpoint")
    response = requests.Response()
    response.status_code = 200
    response.encoding = "utf-8"
    response._content = json.dumps(payload, ensure_ascii=False).encode()
    return response


graduate._request = fake_request


def fake_captcha(*args, **kwargs):
    image = Image.new("RGB", (160, 50), "#edf3f7")
    ImageDraw.Draw(image).text((28, 10), "AbC4", fill="#245439", font_size=30)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {
        "vtoken": "preview",
        "cookie": "sid=preview",
        "captcha_kind": "text",
        "imageUrl": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
    }


app.logic.fetch_vtoken_and_image = fake_captcha
graduate.recognize_captcha = lambda _: "AbC4"
if os.getenv("COURSE_SELECT_PREVIEW_LOGGED_OUT") == "1":
    config.token = config.combined_cookie = ""

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["COURSE_SELECT_PORT"]))
