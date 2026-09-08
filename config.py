"""
全局配置模块 - 存放选课系统的运行时配置和用户参数

本模块中的变量会在运行时被其他模块动态修改（如登录后设置 token、cookie 等），
因此使用模块级全局变量而非类封装。
"""

import os

from campus import DEFAULT_CAMPUS_CODE, DEFAULT_CAMPUS_NAME


def _positive_env_int(name: str, default: int) -> int:
    """Read a positive integer setting while keeping startup deterministic."""
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a positive integer setting clamped into an inclusive range."""
    return max(minimum, min(_positive_env_int(name, default), maximum))


# ====================================================================
# 选课系统基础 URL（深圳大学本科选课系统）
# ====================================================================
SCHOOL_BASE_URL = "http://bkxk.szu.edu.cn/xsxkapp/sys/xsxkapp/"

# Runtime backend selection.  ``auto`` always tries the primary backend first
# for each request; the active backend is the last backend that completed a
# request successfully.
BACKEND_AUTO = "auto"
BACKEND_PRIMARY = "primary"
BACKEND_WEBVPN = "webvpn"
backend_preference = BACKEND_AUTO
active_backend = BACKEND_PRIMARY

# ====================================================================
# 用户登录凭据（命令行模式使用，Web UI 模式下由前端传入）
# ====================================================================
password = ""
student_id = ""

# ====================================================================
# 选课批次代码（登录后由服务器返回，自动填充）
# ====================================================================
elective_batch_code = ""

# 当前批次名称（如预选、复选、补选或学校返回的其他名称）
elective_batch_name = ""

# 当前课程目录校区。首次登录后优先采用学校返回的学生默认校区，
# 用户在工作台切换后会一直保留到退出登录。
campus_code = DEFAULT_CAMPUS_CODE
campus_name = DEFAULT_CAMPUS_NAME

# ====================================================================
# 抢课参数
# ====================================================================
# Legacy fallback interval for callers that do not select a mode.
delay: int = 1000

# 抢课循环的最大次数（设置足够大以持续抢课）
count: int = 150000000

# 会话过期后，允许"连续"自动重登录失败的最大次数（达到后保护性暂停）。
# 只要有一次重登录成功，计数即清零，因此长时间的补选/复选阶段可以持续恢复会话。
relogin_max_retries: int = 5

# 自动重登录失败后的再次尝试间隔。单位为秒。
relogin_retry_interval_seconds: int = 60

# 每次自动重登录中，OCR 最多尝试的验证码图片数量。
ocr_relogin_max_attempts: int = 50

# 单门课程连续收到无法识别的学校返回时，达到该次数后保护性暂停。
# 任意一次已知响应都会立即清零该课程的连续计数。
DEFAULT_UNKNOWN_RESPONSE_PAUSE_THRESHOLD = 2000
unknown_response_pause_threshold: int = _positive_env_int(
    "COURSE_SELECT_UNKNOWN_RESPONSE_LIMIT",
    DEFAULT_UNKNOWN_RESPONSE_PAUSE_THRESHOLD,
)

# Full-catalog search pacing and scoped cache settings.
catalog_page_delay_ms: int = _bounded_env_int(
    "COURSE_SELECT_CATALOG_PAGE_DELAY_MS", 600, minimum=100, maximum=10000
)
catalog_throttle_max_retries: int = _bounded_env_int(
    "COURSE_SELECT_CATALOG_THROTTLE_RETRIES", 3, minimum=1, maximum=10
)
catalog_throttle_backoff_ms: int = _bounded_env_int(
    "COURSE_SELECT_CATALOG_THROTTLE_BACKOFF_MS", 2000, minimum=250, maximum=30000
)
catalog_cache_ttl_seconds: int = _bounded_env_int(
    "COURSE_SELECT_CATALOG_CACHE_TTL_SECONDS", 21600, minimum=300, maximum=604800
)

# ====================================================================
# 运行时状态（登录后由程序自动设置，不要手动修改）
# ====================================================================
# 合并后的完整 Cookie（包含 route、insert_cookie、JSESSIONID、_WEU）
combined_cookie = ""

# Cookies issued by the WebVPN and CAS hosts are kept separate from the
# school's own session cookies and are only sent to their matching backend.
webvpn_cookie = ""
authserver_cookie = ""

# 登录令牌（服务器返回的 token，用于后续接口鉴权）
token = ""


PHASE_PRESELECTION = "preselection"
PHASE_AUTOMATIC = "automatic"
PHASE_CLOSED = "closed"
PHASE_UNKNOWN = "unknown"
AUTOMATIC_ENROLL_PHASE_KEYWORDS = ("复选", "正选", "补选", "补退选")
CLOSED_PHASE_KEYWORDS = (
    "未开放",
    "不开放",
    "尚未开放",
    "未开始",
    "尚未开始",
    "暂停",
    "关闭",
    "结束",
    "截止",
    "停选",
    "维护",
)


def classify_elective_phase(batch_name: str) -> str:
    """Classify the school-provided batch name without guessing from dates."""
    from study_program import is_graduate

    if is_graduate():
        from services.graduate_service import window_payload

        return window_payload()["phase"]
    normalized = str(batch_name or "").strip()
    if any(keyword in normalized for keyword in CLOSED_PHASE_KEYWORDS):
        return PHASE_CLOSED
    if "预选" in normalized:
        return PHASE_PRESELECTION
    if any(keyword in normalized for keyword in AUTOMATIC_ENROLL_PHASE_KEYWORDS):
        return PHASE_AUTOMATIC
    return PHASE_UNKNOWN


def is_automatic_enroll_phase(batch_name: str) -> bool:
    """Return whether the school batch name permits automated enrollment."""
    return classify_elective_phase(batch_name) == PHASE_AUTOMATIC


def automatic_enroll_block_reason(batch_name: str) -> str | None:
    """Explain why a school batch cannot start automatic enrollment."""
    from study_program import is_graduate

    if is_graduate():
        from services.graduate_service import window_payload

        window = window_payload()
        return None if window["phase"] == PHASE_AUTOMATIC else window["phase_message"]
    normalized = str(batch_name or "").strip()
    phase = classify_elective_phase(normalized)
    display_name = normalized or "未知"
    if phase == PHASE_AUTOMATIC:
        return None
    if phase == PHASE_CLOSED:
        return f"当前批次“{display_name}”显示选课未开放或已结束，未启动抢课"
    if phase == PHASE_PRESELECTION:
        return f"当前批次“{display_name}”为预选阶段，未启动抢课"
    return f"当前批次“{display_name}”不在自动抢课白名单内，未启动抢课"
