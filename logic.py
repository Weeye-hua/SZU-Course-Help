"""School login, captcha, OCR, and cookie compatibility functions."""

from __future__ import annotations

import base64
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np  # used by the per-glyph OCR helpers

# OCR dependencies are imported lazily so manual first login can still start
# when optional recognition packages are unavailable.
import requests

import config
from project_paths import data_dir
from school_password import encrypt_school_password
from school_session import is_session_expired_response
from services import backend_service

REQUEST_TIMEOUT = (5, 15)
CAPTCHA_REQUEST_TIMEOUT = (3, 8)
MAX_CAPTCHA_BYTES = 2 * 1024 * 1024
CAPTCHA_WIDTH = 250
CAPTCHA_HEIGHT = 80
OCR_RETRY_DELAY_SECONDS = 0.25
# A structurally wrong captcha response (valid image but missing the required
# Set-Cookie, malformed token payload) should not burn the whole recognition
# budget. Three fresh consecutive failures still allow a brief transient issue
# to recover while stopping a changed school contract early (issue #9).
STRUCTURAL_CAPTCHA_ABORT_AFTER = 3
CAPTCHA_UNAVAILABLE_KEYWORDS = (
    "非选课时间",
    "不在选课时间",
    "未开放",
    "尚未开放",
    "未开始",
    "尚未开始",
    "已结束",
    "已截止",
    "暂停",
    "关闭",
    "停选",
    "维护",
    "无选课批次",
    "没有选课批次",
)
logger = logging.getLogger(__name__)


def _school_request(method: str, path: str, *, read_only: bool = False, **kwargs):
    """Send one school request through the shared backend policy."""
    request_function = getattr(requests, method.lower())

    def sender(**request_kwargs):
        request_kwargs.pop("method", None)
        return request_function(**request_kwargs)

    return backend_service.request_with_failover(
        method,
        path,
        sender=sender,
        read_only=read_only,
        **kwargs,
    )


class SchoolBatchSessionExpiredError(RuntimeError):
    """The school rejected a batch lookup because the session expired."""


class ElectiveBatchUnavailableError(RuntimeError):
    """The school did not expose an active elective batch."""


class CaptchaUnavailableError(RuntimeError):
    """The school explicitly reports that login captcha is currently unavailable."""


class CaptchaResponseError(RuntimeError):
    """The school captcha response is present but cannot be safely consumed."""


@dataclass(frozen=True, slots=True)
class ElectiveBatchResult:
    """Batch metadata plus the student's default campus from one response."""

    batch_code: str
    batch_name: str
    campus_code: str = ""
    campus_name: str = ""

    def __iter__(self):
        """Preserve the historical two-value unpacking contract."""
        yield self.batch_code
        yield self.batch_name


def _captcha_image_path() -> Path:
    return data_dir() / "img" / "image.jpg"


def _captcha_crop_dir() -> Path:
    return data_dir() / "img" / "crop"


def verify_vcode(
    max_attempts: int = config.ocr_relogin_max_attempts,
) -> tuple[str, str, str, str]:
    """Use OCR to solve a fresh click captcha with bounded retries."""
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError("max_attempts must be an integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    _captcha_crop_dir().mkdir(parents=True, exist_ok=True)
    if not config.student_id or not config.password:
        raise RuntimeError("缺少自动重登录所需的学号或密码")

    centers = []
    coordinates = ""
    solved_attempt = 0
    vtoken = ""
    cookie = ""
    last_error: Exception | None = None
    structural_failures = 0
    for attempt in range(1, max_attempts + 1):
        try:
            vtoken, cookie = get_new_image()
            # The school returned one complete captcha response. OCR failures
            # after this point are a separate category and break the streak.
            structural_failures = 0
            centers = recognize_captcha_centers()
            coordinates = serialize_captcha_coordinates(centers)
            if coordinates:
                solved_attempt = attempt
                break
            last_error = RuntimeError("OCR did not return four valid coordinates")
        except CaptchaUnavailableError:
            logger.info("School captcha is unavailable; OCR relogin stopped without retrying")
            raise
        except (ImportError, ModuleNotFoundError):
            raise
        except CaptchaResponseError as exc:
            structural_failures += 1
            last_error = exc
            if structural_failures >= STRUCTURAL_CAPTCHA_ABORT_AFTER:
                logger.error("Captcha contract kept failing; stopping OCR retries: %s", exc)
                raise RuntimeError(
                    f"验证码接口连续 {structural_failures} 次返回结构性异常，已提前终止: {exc}"
                ) from exc
            logger.warning(
                "OCR captcha contract failure %s/%s: %s",
                structural_failures,
                STRUCTURAL_CAPTCHA_ABORT_AFTER,
                exc,
            )
        except Exception as exc:
            structural_failures = 0
            last_error = exc
            logger.warning(
                "OCR captcha attempt %s/%s failed: %s",
                attempt,
                max_attempts,
                exc,
            )
        if attempt < max_attempts:
            time.sleep(min(OCR_RETRY_DELAY_SECONDS * attempt, 1.0))
    else:
        detail = type(last_error).__name__ if last_error else "unknown error"
        raise RuntimeError(f"OCR 连续 {max_attempts} 次识别失败 ({detail})")

    logger.info("OCR captcha solved on attempt %s/%s", solved_attempt, max_attempts)
    login_pwd = encrypt_school_password(config.password)

    parsed_cookie = parse_cookie(cookie)
    if not parsed_cookie:
        raise RuntimeError("验证码响应中缺少必要 Cookie")
    return vtoken, parsed_cookie, login_pwd, coordinates


def verify_vcode_login_flow(
    max_attempts: int = config.ocr_relogin_max_attempts,
) -> tuple[str, str, str, str]:
    """Run the same fetch-image/OCR flow used by the browser login page."""
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError("max_attempts must be an integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if not config.student_id or not config.password:
        raise RuntimeError("缺少自动重登录所需的学号或密码")

    last_error: Exception | None = None
    structural_failures = 0
    for attempt in range(1, max_attempts + 1):
        try:
            captcha = fetch_vtoken_and_image(1)
            # A valid token/image/cookie tuple ends any preceding school
            # response-contract streak, even when OCR cannot solve the image.
            structural_failures = 0
            _header, encoded = str(captcha["imageUrl"]).split(",", 1)
            image_path = _captcha_image_path()
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(base64.b64decode(encoded, validate=True))
            centers = recognize_captcha_centers()
            coordinates = serialize_captcha_coordinates(centers)
            parsed_cookie = parse_cookie(captcha["cookie"])
            if coordinates and parsed_cookie:
                return (
                    str(captcha["vtoken"]),
                    parsed_cookie,
                    encrypt_school_password(config.password),
                    coordinates,
                )
            last_error = RuntimeError("OCR did not return four valid coordinates")
        except CaptchaUnavailableError:
            raise
        except (ImportError, ModuleNotFoundError):
            raise
        except CaptchaResponseError as exc:
            structural_failures += 1
            last_error = exc
            if structural_failures >= STRUCTURAL_CAPTCHA_ABORT_AFTER:
                logger.error("Captcha contract kept failing; stopping OCR retries: %s", exc)
                raise RuntimeError(
                    f"验证码接口连续 {structural_failures} 次返回结构性异常，已提前终止: {exc}"
                ) from exc
            logger.warning(
                "Login-page captcha contract failure %s/%s: %s",
                structural_failures,
                STRUCTURAL_CAPTCHA_ABORT_AFTER,
                exc,
            )
        except Exception as exc:
            structural_failures = 0
            last_error = exc
            logger.warning(
                "Login-page OCR attempt %s/%s failed: %s",
                attempt,
                max_attempts,
                exc,
            )
        if attempt < max_attempts:
            time.sleep(min(OCR_RETRY_DELAY_SECONDS * attempt, 1.0))

    detail = type(last_error).__name__ if last_error else "unknown error"
    raise RuntimeError(f"OCR 连续 {max_attempts} 次识别失败 ({detail})")


def serialize_captcha_coordinates(centers: list) -> str:
    """Serialize exactly four validated click coordinates for the school form."""
    if not centers or len(centers) != 4:
        return ""

    coord_strings = []
    for coord in centers:
        if not isinstance(coord, (list, tuple)) or len(coord) != 2:
            return ""
        x, y = coord
        if isinstance(x, bool) or isinstance(y, bool):
            return ""
        if not isinstance(x, int) or not isinstance(y, int):
            return ""
        if not (0 <= x <= CAPTCHA_WIDTH and 0 <= y <= CAPTCHA_HEIGHT):
            return ""
        coord_strings.append(f"{x}-{y}")

    return ",".join(coord_strings)


def fetch_elective_batch(
    student_id: str,
    token: str,
    combined_cookie: str,
) -> ElectiveBatchResult:
    """Fetch the enrollment batch using one consistent session snapshot."""
    from study_program import is_graduate

    if is_graduate():
        from services import graduate_service

        try:
            return graduate_service.fetch_batch(student_id, combined_cookie)
        except graduate_service.GraduateSessionExpiredError as exc:
            raise SchoolBatchSessionExpiredError(str(exc)) from exc
    response = _school_request(
        "POST",
        f"student/{student_id}.do",
        read_only=True,
        timeout=REQUEST_TIMEOUT,
        token=token,
        cookie=combined_cookie,
    )
    response_text = response.text
    try:
        payload = response.json()
    except ValueError as exc:
        if is_session_expired_response(
            status_code=response.status_code,
            text=response_text,
        ):
            raise SchoolBatchSessionExpiredError("学校登录状态已过期") from exc
        response.raise_for_status()
        raise RuntimeError("学校选课批次接口返回了非 JSON 响应") from exc

    response_code = payload.get("code") if isinstance(payload, dict) else None
    if is_session_expired_response(
        status_code=response.status_code,
        code=response_code,
        text=response_text,
    ):
        raise SchoolBatchSessionExpiredError("学校登录状态已过期")
    response.raise_for_status()
    if not isinstance(payload, dict):
        raise RuntimeError("学校选课批次响应格式异常")
    response_data = payload.get("data") or {}
    if not isinstance(response_data, dict):
        raise RuntimeError("学校选课批次响应数据格式异常")
    batch = response_data.get("electiveBatch") or {}
    if not isinstance(batch, dict):
        raise RuntimeError("学校选课批次字段格式异常")
    batch_code = batch.get("code")
    batch_name = batch.get("typeName") or ""
    if not batch_code:
        raise ElectiveBatchUnavailableError(payload.get("msg") or "学校当前未返回有效的选课批次")
    normalized_code = str(batch_code)
    normalized_name = str(batch_name)
    campus_code = str(response_data.get("campus") or "").strip()
    campus_name = str(response_data.get("campusName") or "").strip()
    logger.info("Current enrollment batch: %s", normalized_name or "unknown")
    return ElectiveBatchResult(
        batch_code=normalized_code,
        batch_name=normalized_name,
        campus_code=campus_code,
        campus_name=campus_name,
    )


def login(
    student_id: str,
    vtoken: str,
    login_pwd: str,
    coordinate_string: str,
    parsed_cookie: str,
) -> dict[str, Any]:
    """Establish a school session using the legacy login form contract."""
    form_data = {
        "loginPwd": login_pwd,
        "loginName": student_id,
        "vtoken": vtoken,
        "verifyCode": coordinate_string,
    }

    # Authentication is state-changing and must never inherit a previous
    # read-only WebVPN fallback. The gateway remains query-only by design.
    #
    # The login POST submits only the cookie issued by this captcha round —
    # the browser contract (captcha Set-Cookie -> login request). Concatenating
    # the stale runtime session resubmits expired route/JSESSIONID values with
    # duplicate cookie names, whose ordering the school resolves
    # unpredictably (issue #9).
    response = _school_request(
        "POST",
        "student/check/login.do",
        data=form_data,
        timeout=REQUEST_TIMEOUT,
        content_type="application/x-www-form-urlencoded; charset=UTF-8",
        cookie=parsed_cookie or None,
        omit_cookie=not parsed_cookie,
        preference=config.BACKEND_PRIMARY,
    )
    profile = backend_service.get_profile(config.BACKEND_PRIMARY)
    set_cookie = (
        response.headers.get_list("set-cookie")
        if hasattr(response.headers, "get_list")
        else [response.headers.get("Set-Cookie", "")]
    )
    if set_cookie:
        backend_service.merge_set_cookie(set_cookie, profile.host)
    try:
        payload = response.json()
    except ValueError:
        return {
            "success": False,
            "error_msg": "学校登录接口返回了非 JSON 响应",
            "cookie": None,
            "name": None,
        }
    if not isinstance(payload, dict):
        return {
            "success": False,
            "error_msg": "学校登录接口响应格式异常",
            "cookie": None,
            "name": None,
        }

    if payload.get("code") != "1":
        return {
            "success": False,
            "error_msg": payload.get("msg"),
            "cookie": None,
            "name": None,
        }

    login_cookie = response.headers.get("Set-Cookie")
    login_parsed_cookie = parse_login_cookie(login_cookie)
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    token = data.get("token") or ""
    name = data.get("name")
    if not token or not login_parsed_cookie:
        return {
            "success": False,
            "error_msg": "学校登录响应缺少会话信息",
            "cookie": None,
            "name": None,
        }
    logger.info("School session established for student ending in %s", student_id[-4:])
    return {
        "success": True,
        "error_msg": None,
        "cookie": login_parsed_cookie,
        "name": name,
        "token": token,
    }


def _extract_paddle_text(value: Any) -> str | None:
    """Best-effort extraction across PaddleOCR 2.x/3.x result shapes."""
    if isinstance(value, dict):
        texts = value.get("rec_texts")
        if isinstance(texts, list) and texts:
            return str(texts[0])
        for child in value.values():
            result = _extract_paddle_text(child)
            if result:
                return result
    elif isinstance(value, (list, tuple)):
        for child in value:
            result = _extract_paddle_text(child)
            if result:
                return result
    elif hasattr(value, "json"):
        raw = value.json
        result = _extract_paddle_text(raw() if callable(raw) else raw)
        if result:
            return result
    return None


def _recognize_target_with_paddle(image_path: str | Path) -> str | None:
    """Use PaddleOCR only when explicitly enabled to avoid model downloads."""
    if os.getenv("COURSE_SELECT_USE_PADDLE_OCR", "").strip() != "1":
        return None
    return _extract_paddle_text(_paddle_engine().predict(str(image_path)))


@lru_cache(maxsize=1)
def _paddle_engine():
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang="ch",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


@lru_cache(maxsize=1)
def _ddddocr_engines():
    """Create OCR engines across the two ddddocr 1.6.1 export layouts."""
    try:
        from ddddocr.core import DetectionEngine, OCREngine
    except (ImportError, AttributeError):
        try:
            from ddddocr import DetectionEngine, OCREngine
        except (ImportError, AttributeError):
            from ddddocr import DdddOcr

            class _DetectorAdapter:
                def __init__(self):
                    self._engine = DdddOcr(det=True, ocr=False, show_ad=False)

                def predict(self, image):
                    return self._engine.detection(image)

            class _RecognizerAdapter:
                def __init__(self):
                    self._engine = DdddOcr(ocr=True, det=False, beta=True, show_ad=False)

                def predict(self, image):
                    return self._engine.classification(image)

            return _DetectorAdapter(), _RecognizerAdapter()

    return DetectionEngine(), OCREngine(beta=True)


def check_ocr_runtime() -> tuple[bool, str]:
    """Initialize OCR early and verify the adapter contract used by relogin."""
    try:
        detector, recognizer = _ddddocr_engines()
    except Exception as exc:
        return False, f"OCR 依赖不可用或版本不兼容: {exc}"
    missing = [
        name
        for name, engine in (("检测", detector), ("识别", recognizer))
        if not callable(getattr(engine, "predict", None))
    ]
    if missing:
        return False, f"OCR {'/'.join(missing)}引擎缺少 predict 接口"
    return True, "OCR 检测与识别引擎已就绪"


def _normalize_ocr_text(value: Any) -> str:
    """Normalize one OCR result and reject accidental multi-character reads."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(normalized.split())
    return normalized if len(normalized) == 1 else ""


def _ocr_glyph(ocr: Any, image: Any, upscale: int = 5, padding: int = 14) -> str:
    """Recognise a single glyph: upscale, pad with white, then run one-char OCR.

    The target glyphs in the top band are only ~12px tall; running OCR on the raw
    compressed glyph (or the whole strip as one image) bleeds characters together.
    Upscaling each separated glyph individually produces far more stable reads.
    """
    import io

    import cv2
    from PIL import Image

    glyph = image
    if len(glyph.shape) == 2:  # grayscale -> BGR
        glyph = cv2.cvtColor(glyph, cv2.COLOR_GRAY2BGR)
    upscaled = cv2.resize(glyph, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_LANCZOS4)
    canvas = np.full(
        (upscaled.shape[0] + 2 * padding, upscaled.shape[1] + 2 * padding, 3),
        255,
        dtype=np.uint8,
    )
    canvas[padding : padding + upscaled.shape[0], padding : padding + upscaled.shape[1]] = upscaled
    buffer = io.BytesIO()
    Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)).save(buffer, format="PNG")
    return _normalize_ocr_text(ocr.predict(buffer.getvalue()))


def _ocr_glyph_options(ocr: Any, image: Any) -> list[str]:
    """Return distinct OCR reads from the raw and two OTSU image variants."""
    import cv2

    options = [_ocr_glyph(ocr, image)]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    for threshold_type in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, binary = cv2.threshold(gray, 0, 255, threshold_type + cv2.THRESH_OTSU)
        variant = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
        options.append(_ocr_glyph(ocr, variant, upscale=6, padding=16))
    return list(dict.fromkeys(option for option in options if option))


def _segment_columns(
    binary: Any,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    merge_gap: int = 1,
    min_width: int = 3,
) -> list[list[int]]:
    """Split a horizontal band into glyph x-intervals using column-ink gaps.

    Returns [[x_start, x_end], ...] ordered left to right. Runs with a gap of at
    most ``merge_gap`` columns are merged; leftover segments narrower than
    ``min_width`` (e.g. stray border pixels or noise) are discarded.
    """
    band = binary[y0:y1, x0:x1]
    col_ink = (band > 0).sum(axis=0)
    runs = []
    start = None
    for col, value in enumerate(col_ink):
        if value > 0 and start is None:
            start = col
        elif value == 0 and start is not None:
            runs.append([start, col - 1])
            start = None
    if start is not None:
        runs.append([start, len(col_ink) - 1])
    if not runs:
        return []

    merged = [runs[0]]
    for run in runs[1:]:
        if run[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [[x0 + left, x0 + right] for left, right in merged if right - left >= min_width]


def _binary_image(image: Any) -> Any:
    """Return a binary (black=&gt;255 text) mask for glyph segmentation."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return binary


def _candidate_boxes(image: Any) -> list[list[int]]:
    """Detect and sort the bottom candidate glyph boxes (relative to image coords)."""
    import io

    import cv2
    from PIL import Image

    detector, _ocr = _ddddocr_engines()

    buffer = io.BytesIO()
    Image.fromarray(cv2.cvtColor(image[25:80, 0:250], cv2.COLOR_BGR2RGB)).save(buffer, format="PNG")
    boxes = _sanitize_candidate_boxes(detector.predict(buffer.getvalue()), image.shape)
    # Convert crop-relative box coordinates back to full-image coordinates.
    return [[x1, y1 + 25, x2, y2 + 25] for x1, y1, x2, y2 in boxes]


def _sanitize_candidate_boxes(raw_boxes: Any, image_shape: tuple[int, ...]) -> list[list[int]]:
    """Clamp, discard noise, and de-duplicate detector boxes before OCR."""
    height, width = image_shape[:2]
    sanitized: list[list[int]] = []
    for raw_box in raw_boxes or []:
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(round(value)) for value in raw_box)
        except (TypeError, ValueError):
            continue
        x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
        y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
        if x2 <= x1 or y2 <= y1 or x2 - x1 < 8 or y2 - y1 < 10:
            continue
        area = (x2 - x1) * (y2 - y1)
        if any(
            (min(x2, old_x2) - max(x1, old_x1))
            * (min(y2, old_y2) - max(y1, old_y1))
            / min(area, (old_x2 - old_x1) * (old_y2 - old_y1))
            >= 0.95
            for old_x1, old_y1, old_x2, old_y2 in sanitized
            if min(x2, old_x2) > max(x1, old_x1) and min(y2, old_y2) > max(y1, old_y1)
        ):
            continue
        sanitized.append([x1, y1, x2, y2])
    return sorted(sanitized, key=lambda item: (item[0], item[1]))


def _recognize_candidate_glyphs(
    ocr: Any,
    image: Any,
    boxes: list[list[int]],
) -> list[str]:
    """OCR each bottom candidate box individually, aligned with ``boxes``."""
    chars = []
    for x1, y1, x2, y2 in boxes:
        glyph = image[max(y1, 0) : y2, max(x1, 0) : x2]
        if glyph.shape[0] < 1 or glyph.shape[1] < 1:
            chars.append("")
            continue
        chars.append(_ocr_glyph(ocr, glyph))
    return chars


def _segmented_target_glyphs(binary: Any) -> list[list[int]]:
    """Segment the top target band (rows 2-14, cols 82-132) into four glyphs.

    The first two image rows are a full-width solid border that the school draws;
    they carry no character information, so the target band starts at row 2.  A
    simple 4-column grid split (as the previous template matcher assumed) does not
    align to the real glyph extents; column-gap segmentation does.
    """
    return _segment_columns(binary, 2, 14, 82, 132)


def _template_match_targets(
    image: Any,
    bottom_boxes: list[list[int]],
    target_intervals: list[list[int]] | None = None,
) -> list[list[int]]:
    """Match 4 target characters (top region) to bottom candidates by image similarity.

    Last-resort fallback only: the top glyphs are only ~12px tall, so image
    similarity is a weak discriminator and should never be the primary path.  When
    the real segmented target intervals are supplied they are used instead of the
    previous fixed 4-column grid over the whole 55px top crop.
    """
    import cv2

    if len(bottom_boxes) < 4:
        return []

    bottom_img = image[25:80, 0:250]

    if target_intervals and len(target_intervals) == 4:
        targets = [image[2:14, left:right] for left, right in target_intervals]
    else:
        top_region = image[2:14, 82:132]
        top_region_w = top_region.shape[1]
        char_width = top_region_w // 4
        targets = [
            top_region[:, 0:char_width],
            top_region[:, char_width : 2 * char_width],
            top_region[:, 2 * char_width : 3 * char_width],
            top_region[:, 3 * char_width :],
        ]

    result: list[list[int]] = []
    used: set[int] = set()
    for target in targets:
        th, tw = target.shape[:2]
        if th < 1 or tw < 1:
            return []

        best_score = -2.0
        best_idx = -1
        for bi, (bx1, by1, bx2, by2) in enumerate(bottom_boxes):
            if bi in used:
                continue
            candidate = bottom_img[by1 - 25 : by2 - 25, bx1:bx2]
            ch, cw = candidate.shape[:2]
            if ch < 1 or cw < 1:
                continue
            target_resized = cv2.resize(target, (cw, ch))
            score = float(cv2.matchTemplate(candidate, target_resized, cv2.TM_CCOEFF_NORMED).max())
            if score > best_score:
                best_score = score
                best_idx = bi

        if best_idx < 0:
            return []
        used.add(best_idx)
        bx1, by1, bx2, by2 = bottom_boxes[best_idx]
        cx = (bx1 + bx2) // 2
        cy = (by1 + by2) // 2
        result.append([cx, cy])

    return result


def recognize_captcha_centers() -> list[list[int]]:
    """Recognize the four captcha targets and return click coordinates.

    Pipeline (best-effort, never guesses with low confidence):
      1. Segment the top target band into four glyphs (rows 2-14, excl. border).
      2. Detect the bottom candidate boxes and OCR each one individually so the
         candidate character list stays aligned with the boxes (fixes the previous
         "6 boxes vs 5 OCR chars" count mismatch that forced weak template matching).
      3. Exact-string-match each target glyph to an unused candidate box.
      4. For a target left unmatched, re-OCR the remaining candidate boxes with a
         second binarization to try to recover its true character.
      5. Return exactly four distinct, in-range coordinates; otherwise return [] so
         the caller retries with a fresh captcha rather than submitting a guess.
    """
    import cv2

    image = cv2.imread(str(_captcha_image_path()))
    if image is None or image.shape[0] < CAPTCHA_HEIGHT or image.shape[1] < CAPTCHA_WIDTH:
        raise RuntimeError("验证码图片为空或尺寸异常")

    detector, ocr = _ddddocr_engines()

    binary = _binary_image(image)
    target_intervals = _segmented_target_glyphs(binary)
    if len(target_intervals) != 4:
        logger.warning("Target band segmented into %s glyphs (expected 4)", len(target_intervals))
        return []

    target_chars = [_ocr_glyph(ocr, image[2:14, left:right]) for left, right in target_intervals]
    if any(not char for char in target_chars) or len(target_chars) != 4:
        logger.warning("Top target OCR incomplete: %s", target_chars)
        return []

    boxes = _candidate_boxes(image)
    if len(boxes) < 4:
        logger.warning("OCR detected only %s candidate boxes", len(boxes))
        return []

    candidate_chars = _recognize_candidate_glyphs(ocr, image, boxes)
    logger.debug("OCR targets=%s candidates=%s", target_chars, candidate_chars)

    # Exact matching: each target picks the first unused candidate box holding the
    # same character.  Two coordinates never share a box.
    matched_indexes: list[int | None] = [None] * len(target_chars)
    used_indexes: set[int] = set()
    unmatched_targets: list[tuple[int, str]] = []
    for target_position, target_char in enumerate(target_chars):
        matched_index = next(
            (
                index
                for index, candidate in enumerate(candidate_chars)
                if index not in used_indexes and candidate == target_char
            ),
            None,
        )
        if matched_index is not None:
            used_indexes.add(matched_index)
            matched_indexes[target_position] = matched_index
        else:
            unmatched_targets.append((target_position, target_char))

    # Re-OCR the still-unused candidate boxes with a second binarization to try to
    # recover characters the default path misread.  This honours the invariant that
    # every target is one of the candidate characters.  If a target still cannot be
    # matched we return [] and let the caller retry with a fresh captcha: the top
    # glyphs are only ~12px tall so image-template matching cannot reliably
    # disambiguate them, and submitting a guessed coordinate wastes an attempt
    # exactly like a clean retry does anyway.
    if unmatched_targets:
        passed_bgr = [image[y1:y2, x1:x2] for x1, y1, x2, y2 in boxes]
        rechars = _re_ocr_remaining_candidates(ocr, passed_bgr, used_indexes)
        for target_position, target_char in unmatched_targets[:]:
            matched_index = next(
                (
                    index
                    for index, candidate in enumerate(rechars)
                    if index not in used_indexes and candidate == target_char
                ),
                None,
            )
            if matched_index is not None:
                used_indexes.add(matched_index)
                matched_indexes[target_position] = matched_index
                unmatched_targets.remove((target_position, target_char))

        # If the first OCR pass read a character differently in the top and
        # bottom regions, compare the raw and binarized reads without guessing.
        if unmatched_targets and any(rechars):
            candidate_options = {
                index: _ocr_glyph_options(ocr, passed_bgr[index])
                for index in range(len(boxes))
                if index not in used_indexes
            }
            target_options = {}
            for position, _target_char in unmatched_targets:
                left, right = target_intervals[position]
                target_options[position] = _ocr_glyph_options(ocr, image[2:14, left:right])
            for target_position, _target_char in unmatched_targets[:]:
                target_reads = set(target_options.get(target_position, ()))
                matched_index = next(
                    (
                        index
                        for index, candidate_reads in candidate_options.items()
                        if index not in used_indexes and target_reads.intersection(candidate_reads)
                    ),
                    None,
                )
                if matched_index is not None:
                    used_indexes.add(matched_index)
                    matched_indexes[target_position] = matched_index
                    unmatched_targets.remove((target_position, _target_char))

        if unmatched_targets:
            logger.info(
                "OCR unmatched targets=%s (candidates=%s); returning [] to retry",
                unmatched_targets,
                candidate_chars,
            )
            return []

    if any(index is None for index in matched_indexes):
        return []
    result = [
        [(boxes[index][0] + boxes[index][2]) // 2, (boxes[index][1] + boxes[index][3]) // 2]
        for index in matched_indexes
    ]
    if len(result) != 4 or not _all_distinct(result) or not _all_in_range(result):
        logger.warning(
            "OCR produced %s points (targets=%s candidates=%s)",
            len(result),
            target_chars,
            candidate_chars,
        )
        return []

    return result


def _re_ocr_remaining_candidates(
    ocr: Any,
    candidate_images: list[Any],
    used_indexes: set[int],
) -> list[str]:
    """Re-OCR unused candidate glyphs with an OTSU binarization and higher upscale."""
    results: list[str] = []
    for index, glyph in enumerate(candidate_images):
        if index in used_indexes:
            results.append("")
            continue
        options = _ocr_glyph_options(ocr, glyph)
        results.append(options[0] if options else "")
    return results


def _all_distinct(points: list[list[int]]) -> bool:
    return len({tuple(point) for point in points}) == 4


def _all_in_range(points: list[list[int]]) -> bool:
    return all(
        isinstance(point, (list, tuple))
        and len(point) == 2
        and 0 <= int(point[0]) <= CAPTCHA_WIDTH
        and 0 <= int(point[1]) <= CAPTCHA_HEIGHT
        for point in points
    )


def _extract_captcha_message(payload: Any, response_text: str = "") -> str:
    """Extract a short school status message without depending on one response schema."""
    containers = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("msg", "message", "errorMessage", "error", "detail"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(response_text or "").strip()[:2000]


def _looks_like_captcha_unavailable(message: str) -> bool:
    normalized = str(message or "").strip()
    return any(keyword in normalized for keyword in CAPTCHA_UNAVAILABLE_KEYWORDS)


def _parse_captcha_token_response(response: requests.Response) -> str:
    """Return a validated token while preserving closed-window and transport failures."""
    response_text = str(getattr(response, "text", "") or "")
    try:
        payload = response.json()
    except ValueError:
        payload = None

    school_message = _extract_captcha_message(payload, response_text)
    if _looks_like_captcha_unavailable(school_message):
        raise CaptchaUnavailableError("school captcha endpoint is not open")

    response.raise_for_status()
    if not isinstance(payload, dict):
        raise CaptchaResponseError("school captcha token response is not JSON object")

    data = payload.get("data")
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip() or len(token) > 512:
        raise CaptchaResponseError("school captcha token is missing or invalid")
    return token.strip()


def _captcha_backend_preference(preference: str | None = None) -> str:
    """Resolve one explicit backend for a complete captcha round."""
    selected = backend_service.normalize_preference(preference or backend_service.get_preference())
    return config.BACKEND_WEBVPN if selected == config.BACKEND_WEBVPN else config.BACKEND_PRIMARY


def get_vtoken(*, preference: str | None = None) -> str:
    selected_preference = _captcha_backend_preference(preference)
    time_stamp = int(time.time() * 1000)
    response = _school_request(
        "POST",
        f"student/4/vcode.do?timestamp={time_stamp}",
        read_only=True,
        preference=selected_preference,
        omit_cookie=True,
        timeout=CAPTCHA_REQUEST_TIMEOUT,
    )
    return _parse_captcha_token_response(response)


def _validate_captcha_image(image_data: bytes, content_type: str = "") -> None:
    normalized_type = content_type.lower()
    if (
        not image_data
        or len(image_data) > MAX_CAPTCHA_BYTES
        or not image_data.startswith(b"\xff\xd8\xff")
        or (normalized_type and not normalized_type.startswith("image/"))
    ):
        raise CaptchaResponseError(
            f"验证码图片响应异常(bytes={len(image_data)}, type={normalized_type or 'unknown'})"
        )


def get_new_image() -> tuple[str, str]:
    preference = _captcha_backend_preference()
    vtoken = get_vtoken(preference=preference)
    response = _school_request(
        "GET",
        f"student/vcode/image.do?vtoken={vtoken}",
        read_only=True,
        preference=preference,
        omit_cookie=True,
        timeout=CAPTCHA_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    cookie = response.headers.get("Set-Cookie", "")
    if not parse_cookie(cookie):
        raise CaptchaResponseError("验证码图片响应缺少必要 Cookie")
    _validate_captcha_image(
        response.content,
        response.headers.get("Content-Type", ""),
    )
    image_path = _captcha_image_path()
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(response.content)
    return vtoken, cookie


def _fetch_vtoken_and_image_once() -> dict[str, str]:
    preference = _captcha_backend_preference()
    timestamp = int(time.time() * 1000)
    token_response = _school_request(
        "POST",
        f"student/4/vcode.do?timestamp={timestamp}",
        read_only=True,
        preference=preference,
        omit_cookie=True,
        timeout=CAPTCHA_REQUEST_TIMEOUT,
        accept="application/json, text/javascript, */*; q=0.01",
    )
    vtoken = _parse_captcha_token_response(token_response)

    image_response = _school_request(
        "GET",
        f"student/vcode/image.do?vtoken={vtoken}",
        read_only=True,
        preference=preference,
        omit_cookie=True,
        timeout=CAPTCHA_REQUEST_TIMEOUT,
        accept="application/json, text/javascript, */*; q=0.01",
    )
    image_response.raise_for_status()
    image_data = image_response.content
    content_type = image_response.headers.get("Content-Type", "").lower()
    _validate_captcha_image(image_data, content_type)

    cookie = image_response.headers.get("Set-Cookie", "")
    if not parse_cookie(cookie):
        raise CaptchaResponseError("验证码图片响应缺少必要 Cookie")

    encoded = base64.b64encode(image_data).decode("ascii")
    return {
        "vtoken": vtoken,
        "cookie": cookie,
        "imageUrl": f"data:image/jpeg;base64,{encoded}",
    }


def fetch_vtoken_and_image(max_attempts: int = 3) -> dict[str, str]:
    """Fetch the selected program's captcha with bounded attempts."""
    from study_program import is_graduate

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError("max_attempts must be an integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if is_graduate():
        from services.graduate_service import fetch_captcha

        return fetch_captcha()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _fetch_vtoken_and_image_once()
        except CaptchaUnavailableError:
            raise
        except (requests.RequestException, CaptchaResponseError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(0.25 * attempt)
    logger.warning("Captcha fetch failed after %s attempts: %s", max_attempts, last_error)
    if last_error is None:  # Defensive guard; max_attempts validation makes this unreachable.
        raise CaptchaResponseError("captcha fetch ended without a result")
    raise last_error


def _extract_named_cookies(cookie_string: str | None, names: tuple[str, ...]) -> str:
    """Extract named cookies without breaking on commas in Expires values."""
    if not cookie_string:
        return ""
    values = {}
    name_pattern = "|".join(re.escape(name) for name in names)
    for match in re.finditer(rf"(?:^|[,;]\s*)({name_pattern})=([^;,]+)", cookie_string):
        values[match.group(1)] = match.group(2).strip()
    return "; ".join(f"{name}={values[name]}" for name in names if name in values)


def parse_cookie(cookie_string: str | None) -> str:
    """解析 cookie 字符串，提取 route 和 insert_cookie。"""
    return _extract_named_cookies(cookie_string, ("route", "insert_cookie"))


def parse_login_cookie(login_cookie: str | None) -> str:
    """解析登录响应中的 cookie，提取 JSESSIONID 和 _WEU。"""
    return _extract_named_cookies(login_cookie, ("JSESSIONID", "_WEU"))
