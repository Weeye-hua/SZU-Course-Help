from __future__ import annotations

from pathlib import Path

import app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static_dist"


def test_frontend_entrypoints_and_assets_exist():
    expected = {
        "login.html",
        "offline.html",
        "index.html",
        "styles.css",
        "login.js",
        "offline.js",
        "course-app.js",
        "bg.avif",
        "favicon.ico",
    }
    assert expected <= {path.name for path in STATIC.iterdir() if path.is_file()}


def test_login_and_course_pages_expose_required_controls():
    login = (STATIC / "login.html").read_text(encoding="utf-8")
    offline = (STATIC / "offline.html").read_text(encoding="utf-8")
    course = (STATIC / "index.html").read_text(encoding="utf-8")
    login_script = (STATIC / "login.js").read_text(encoding="utf-8")
    offline_script = (STATIC / "offline.js").read_text(encoding="utf-8")
    course_script = (STATIC / "course-app.js").read_text(encoding="utf-8")

    for control_id in (
        "studentId",
        "password",
        "captchaStage",
        "captchaStatusTitle",
        "captchaStatusDetail",
        "refreshCaptcha",
        "loginButton",
    ):
        assert f'id="{control_id}"' in login
    for control_id in (
        "categoryList",
        "courseList",
        "cartDialog",
        "openEnrollConfirm",
        "openMyCourses",
        "myCoursesDialog",
        "campusSelect",
        "enrollProgress",
        "progressState",
        "progressNotice",
        "taskControlButton",
        "stopEnroll",
        "sessionRecoveryBanner",
        "recoveryTitle",
        "recoveryDetail",
        "refreshPhase",
        "refreshCourses",
    ):
        assert f'id="{control_id}"' in course
    assert f"/styles.css?build={app.UI_ASSET_BUILD}" in login
    assert f"/styles.css?build={app.UI_ASSET_BUILD}" in course
    assert f"/login.js?build={app.UI_ASSET_BUILD}" in login
    assert 'href="/offline" target="_blank"' in login
    assert 'id="offlineCourseList"' in offline
    assert f"/offline.js?build={app.UI_ASSET_BUILD}" in offline
    assert "cache_mode=true" in offline_script
    assert f"/course-app.js?build={app.UI_ASSET_BUILD}" in course
    assert 'id="cacheModeSwitch"' in course
    assert "versionedPage" not in login_script
    assert "versionedPage" not in course_script
    assert "ui_cache_token" not in login_script
    assert "ui_cache_token" not in course_script
    assert "stripUiQuery" in login_script
    assert "stripUiQuery" in course_script
    assert "bootstrap.card_key" in login_script
    assert "SESSION_CREDENTIALS_STORAGE_KEY" not in login_script
    assert "sessionStorage" not in login_script
    assert "localStorage" not in login_script
    assert 'id="openTimetable"' not in course
    assert 'id="timetableDialog"' not in course


def test_login_captcha_ui_has_terminal_failure_states():
    login = (STATIC / "login.html").read_text(encoding="utf-8")
    script = (STATIC / "login.js").read_text(encoding="utf-8")

    assert 'data-state="idle"' in login
    assert 'id="captchaWebvpnAuthButton"' in login
    assert 'id="captchaActions"' in login
    assert "当前时段暂无验证码" in script
    assert 'code === "CAPTCHA_UNAVAILABLE"' in script
    assert 'loginState.backend === "webvpn"' in script
    assert "loginState.captcha = null" in script
    assert "当前选择 WebVPN，但验证码暂时无法获取，请先完成统一认证" in script
    assert "loginElements.captchaActions.hidden = webvpnFallback" in script
    assert "loginElements.stage.hidden = webvpnFallback" in script
    assert "WebVPN 统一认证完成，正在刷新验证码" in script
    assert "本次加载已经停止，不会在后台自动循环" in script
    assert "重新获取验证码" in script


def test_course_page_exposes_pause_and_relogin_states():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")

    assert '"/api/enroll/pause"' in script
    assert '"/api/enroll/resume"' in script
    assert '"/api/enroll/stop"' in script
    assert "appState.session?.task_running" in script
    assert "用户请求停止抢课任务" in script
    assert "停止抢课" in script or 'id="stopEnroll"' in script
    assert "正在自动重新登录" in script
    assert "自动重新登录成功" in script
    assert '"/api/session/recover"' in script
    assert "点击立即尝试自动重新登录" in script
    assert "loadBrowserRecoveryCredentials" not in script
    assert "SESSION_CREDENTIALS_STORAGE_KEY" not in script
    assert "automatic_relogin_available" in script
    assert "重新排队" in script
    assert "task_pause_acknowledged" in script
    assert "task_queue_revision" in script
    assert "queueChanged" in script
    assert "正在完成当前学校请求；安全暂停后即可增删课程或调整优先级" in script
    assert "taskStopping || (!terminalCourse && !canEditPausedTask)" in script
    assert (
        "updateLocalCartPreference(item, { priorityRank: currentPreference.priorityRank })"
        in script
    )
    assert (
        "updateLocalCartPreference(other, { priorityRank: otherPreference.priorityRank })" in script
    )


def test_course_groups_start_collapsed_and_my_courses_is_single_schedule_entry():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "details.open" not in script
    assert '"/api/session/campus"' in script
    assert '"/api/school/enrolled"' in script
    assert "renderMyCoursesSchedule" in script
    assert "myCoursesDialog.showModal()" in script
    assert "openTimetable" not in script
    assert "timetableDialog" not in script
    assert "timetableContent" not in script
    assert "timetable-grid" not in styles
    assert "timetable-course" not in styles


def test_my_courses_restores_schedule_and_list_views():
    course = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert 'id="scheduleViewGrid"' in course
    assert 'id="scheduleViewList"' in course
    assert 'id="showPendingSwitch"' in course
    assert 'id="myCoursesScheduleWrap"' in course
    assert "switchMyCoursesView" in script
    assert "renderMyCoursesSchedule" in script
    assert "showCartOnSchedule" in script
    assert "getPendingMyCourseItems" in script
    assert "myCoursesCreditSummary" in script
    assert "renderMyCoursesCreditSummary" in script
    assert "my-course-pending-badge" in script
    assert 'id="selectedCreditTotal"' in course
    assert 'id="pendingCreditTotal"' in course
    assert 'id="combinedCreditTotal"' in course
    assert ".my-courses-credit-summary" in styles
    assert (
        'const block = element("div", `schedule-course${stateClasses ? ` ${stateClasses}` : ""}`);'
        in script
    )
    assert "虚化块为选课清单中的待选课程" in script
    assert ".schedule-course.is-pending" in styles


def test_schedule_cards_expand_for_long_content_and_share_a_time_slot():
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    stack_rule = styles.split(".schedule-stack {", 1)[1].split("}", 1)[0]
    course_rule = styles.split(".schedule-course {", 1)[1].split("}", 1)[0]

    assert "grid-auto-flow: column;" in styles
    assert "grid-auto-columns: minmax(0, 1fr);" in styles
    assert "overflow: visible;" in stack_rule
    assert "overflow: visible;" in course_rule
    assert "flex:" not in stack_rule


def test_drawer_dialog_keeps_header_and_footer_outside_scroll_region():
    course = (STATIC / "index.html").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert '<div class="cart-dialog-body">' in course
    assert '<div class="my-courses-dialog-body">' in course
    assert ".drawer-dialog > .dialog-header" in styles
    assert ".drawer-dialog > .dialog-footer" in styles
    assert "display: flex;" in styles
    assert "overflow: hidden;" in styles
    assert ".cart-dialog-body," in styles
    assert ".my-courses-dialog-body" in styles


def test_course_filters_are_exclusion_filters_and_hide_empty_groups():
    course = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "不显示时间冲突" in course
    assert "不显示人数已满" in course
    assert "hideConflict" in script
    assert "hideFull" in script
    assert "if (classIsSelected(classInfo)) return true;" in script
    assert "FILTER_PREFERENCES_KEY" in script
    assert "saveFilterPreferences" in script
    assert ".course-group.is-selected" in styles
    # 筛选后没有可见教学班的课程连同选项卡一起隐藏，不再展示“0 / N 个教学班”。
    assert "courseHasVisibleClasses" in script
    assert ".filter((course) => courseHasVisibleClasses(course))" in script
    assert "appState.courses.filter((course) => courseHasVisibleClasses(course))" in script
    assert "当前筛选条件下没有可显示的课程" in script
    assert "当前筛选条件下没有符合条件的教学班" not in script


def test_conflict_timetable_context_is_rendered_and_highlighted():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "openConflictTimetable" in script
    assert "scheduleEntriesOverlap" in script
    assert "is-conflict-highlight" in script
    assert "is-focused" in script
    assert "scheduleConflict = null" in script
    assert ".schedule-course.is-conflict-highlight" in styles
    assert 'switchMyCoursesView("grid")' in script


def test_course_search_filters_full_catalog_and_repaginates():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    # 搜索不再局限于当前分页：旧的页内过滤文案必须移除
    assert "本页没有匹配结果" not in script
    assert "筛选本页课程" not in index
    assert "搜索全部课程" in index
    # 在完整目录上过滤，并按匹配结果重新分页
    assert "没有匹配的课程" in script
    assert "正在加载全部课程" in script
    assert "appState.searchPage" in script
    assert "results.slice(start, start + FILTER_PAGE_SIZE)" in script
    assert "匹配" in script
    # 完整目录缓存必须受会话范围约束，并能主动失效。
    assert "catalogScopeKey" in script
    assert "invalidateCatalogCache" in script
    assert "scopeKey !== catalogScopeKey()" in script
    # 多页读取有节流和明确上限，不能把截断响应当作完整目录。
    assert "CATALOG_PAGE_DELAY_MS" in script
    assert 'code: "CATALOG_PAGE_LIMIT"' in script
    assert "cache.courses.length !== cache.totalCount" in script


def test_course_cache_mode_keeps_cached_view_and_refreshes_in_background():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")

    assert "cacheMode" in script
    assert "cacheRefreshTimer" in script
    assert "CACHE_REFRESH_INTERVAL_MS = 30000" in script
    assert 'params.set("cache_mode", "true")' in script
    assert "refreshCoursesFromNetwork" in script
    assert "实时刷新返回空列表，仍显示上次成功结果" in script
    assert "实时刷新暂不可用，仍显示上次成功结果" in script
    assert "clearCacheRefreshTimer" in script


def test_enrollment_interval_ui_uses_seconds_and_api_uses_milliseconds():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "intervalSecondsToMilliseconds" in script
    assert "intervalMillisecondsToSeconds" in script
    assert "scan_interval_ms: intervalSecondsToMilliseconds" in script
    assert 'id="boostInterval"' in index and 'value="1"' in index
    assert 'id="normalInterval"' in index and 'value="10"' in index
    assert 'id="scanInterval"' in index and 'value="60"' in index


def test_enrollment_failure_limits_support_finite_and_unlimited_values():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'id="boostFailureLimit"' in index and 'value="5"' in index
    assert 'id="normalFailureLimit"' in index and 'value="10"' in index
    assert 'id="boostFailureUnlimited" type="checkbox" aria-label="爆发模式失败次数无限"' in index
    assert 'id="normalFailureUnlimited" type="checkbox" aria-label="一般模式失败次数无限"' in index
    assert "boost_failure_limit: failureLimitFromInputs" in script
    assert "normal_failure_limit: failureLimitFromInputs" in script
    assert "source.boost_failure_limit === null" in script
    assert "source.normal_failure_limit === null" in script
    assert "function failureDowngradeHint" in script
    assert "不自动降为${targetName}模式" in script
    assert 'failureDowngradeHint("爆发"' in script
    assert 'failureDowngradeHint("一般"' in script


def test_cart_auto_enroll_checkbox_reconciles_server_state():
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")

    assert "saved.auto_enabled" in script
    assert "item.auto_enabled !== undefined" in script
    assert "current.autoEnabled = Boolean(item.auto_enabled)" in script


def test_school_raw_entry_is_a_real_public_link():
    """The fixed public school entry opens directly in the user's browser.

    A real anchor always works — the workbench itself runs in a browser — so
    the desktop opener chain (xdg-open → kde-open) cannot break this entry,
    and the URL must stay in sync with app.OFFICIAL_SCHOOL_HOME_URL.
    """
    course = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "course-app.js").read_text(encoding="utf-8")

    assert 'id="openSchoolRaw"' in course
    assert f'href="{app.OFFICIAL_SCHOOL_HOME_URL}"' in course
    assert 'target="_blank"' in course
    assert 'rel="noopener noreferrer"' in course
    # The old backend-spawn path is gone from the frontend.
    assert "openSchoolRawPage" not in script
    assert "/api/school/open" not in script
