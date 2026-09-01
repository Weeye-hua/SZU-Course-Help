"use strict";

const REQUEST_TIMEOUT_MS = 30000;
const SESSION_RECOVERY_TIMEOUT_MS = 180000;
const FILTER_PAGE_SIZE = 10;
const MAX_CATALOG_PAGES = 1000;
const CATALOG_PAGE_DELAY_MS = 150;
const SEARCH_DEBOUNCE_MS = 250;
const SESSION_POLL_INTERVAL_MS = 5000;
const CACHE_REFRESH_INTERVAL_MS = 30000;
const FILTER_PREFERENCES_KEY = "szu-course-help.course-filters.v1";
const OFFLINE_CACHE_TYPES = new Set(["TJKC", "FANKC"]);

const enrollModeNames = {
  boost: "爆发模式",
  normal: "一般模式",
  scan: "扫描模式",
};

const categoryNames = {
  TJKC: "本班推荐",
  FANKC: "方案内课程",
  FAWKC: "方案外课程",
  XGXK: "校公选课",
  TYKC: "体育课程",
  MOOC: "慕课",
  FXKC: "辅修课程",
};

const statusNames = {
  PENDING: "待启动",
  ENROLLING: "抢课中",
  SUCCESS: "已抢到",
  FAILED: "已停止",
};

const appState = {
  type: "TJKC",
  page: 1,
  totalCount: 0,
  courses: [],
  searchKeyword: "",
  searchResults: [],
  searchPage: 1,
  catalogCaches: {},
  loadingCatalog: false,
  catalogLoadingType: "",
  catalogRequestController: null,
  catalogRequestId: 0,
  filters: {
    hideConflict: false,
    hideFull: false,
  },
  cart: [],
  cartPreferences: {},
  session: null,
  loadingCourses: false,
  courseRequestController: null,
  courseRequestId: 0,
  courseDataKey: "",
  courseCacheMeta: null,
  catalogBlockedCode: "",
  loadingSession: false,
  sessionTimer: null,
  refreshingPhase: false,
  preselection: false,
  closedPhase: false,
  grabPhase: false,
  myCourses: [],
  myCoursesLoaded: false,
  loadingMyCourses: false,
  cacheMode: false,
  cacheRefreshTimer: null,
  enroll: {
    running: false,
    paused: false,
    mode: "boost",
    boostIntervalMs: 1000,
    normalIntervalMs: 10000,
    scanIntervalMs: 60000,
    boostFailureLimit: 5,
    normalFailureLimit: 10,
  },
  myCoursesView: "grid",
  showCartOnSchedule: true,
  scheduleConflict: null,
  switchingCampus: false,
  progress: null,
  progressTimer: null,
  loadingProgress: false,
  knownSuccessIds: new Set(),
  wasTaskRunning: false,
  taskControlPending: false,
  recoveryHideTimer: null,
  recoveryDismissedAt: "",
  lastReloginStatus: "idle",
  reloginRequestPending: false,
};

const appElements = {
  categoryList: document.querySelector("#categoryList"),
  phaseBanner: document.querySelector("#phaseBanner"),
  phaseTitle: document.querySelector("#phaseTitle"),
  phaseDescription: document.querySelector("#phaseDescription"),
  phaseBadge: document.querySelector("#phaseBadge"),
  refreshPhase: document.querySelector("#refreshPhase"),
  studentLabel: document.querySelector("#studentLabel"),
  taskIndicator: document.querySelector("#taskIndicator"),
  sessionRecoveryBanner: document.querySelector("#sessionRecoveryBanner"),
  recoveryTitle: document.querySelector("#recoveryTitle"),
  recoveryDetail: document.querySelector("#recoveryDetail"),
  recoveryLoginLink: document.querySelector("#recoveryLoginLink"),
  courseTypeCode: document.querySelector("#courseTypeCode"),
  courseTitle: document.querySelector("#courseTitle"),
  courseSummary: document.querySelector("#courseSummary"),
  courseSearch: document.querySelector("#courseSearch"),
  filterConflictSwitch: document.querySelector("#filterConflictSwitch"),
  filterFullSwitch: document.querySelector("#filterFullSwitch"),
  cacheModeSwitch: document.querySelector("#cacheModeSwitch"),
  refreshCourses: document.querySelector("#refreshCourses"),
  campusSelect: document.querySelector("#campusSelect"),
  courseList: document.querySelector("#courseList"),
  previousPage: document.querySelector("#previousPage"),
  nextPage: document.querySelector("#nextPage"),
  pageLabel: document.querySelector("#pageLabel"),
  openCart: document.querySelector("#openCart"),
  cartCount: document.querySelector("#cartCount"),
  cartDialog: document.querySelector("#cartDialog"),
  cartList: document.querySelector("#cartList"),
  cartHint: document.querySelector("#cartHint"),
  openEnrollConfirm: document.querySelector("#openEnrollConfirm"),
  enrollDialog: document.querySelector("#enrollDialog"),
  phaseConfirmation: document.querySelector("#phaseConfirmation"),
  startEnroll: document.querySelector("#startEnroll"),
  enrollMessage: document.querySelector("#enrollMessage"),
  sessionDialog: document.querySelector("#sessionDialog"),
  sessionMessage: document.querySelector("#sessionMessage"),
  sessionLoginLink: document.querySelector("#sessionDialog a[href^='/login']"),
  brandLink: document.querySelector(".topbar .brand-lockup"),
  logout: document.querySelector("#logoutButton"),
  toastRegion: document.querySelector("#toastRegion"),
  openMyCourses: document.querySelector("#openMyCourses"),
  myCoursesDialog: document.querySelector("#myCoursesDialog"),
  myCoursesList: document.querySelector("#myCoursesList"),
  myCoursesHint: document.querySelector("#myCoursesHint"),
  selectedCreditTotal: document.querySelector("#selectedCreditTotal"),
  pendingCreditTotal: document.querySelector("#pendingCreditTotal"),
  combinedCreditTotal: document.querySelector("#combinedCreditTotal"),
  refreshMyCourses: document.querySelector("#refreshMyCourses"),
  myCoursesScheduleWrap: document.querySelector("#myCoursesScheduleWrap"),
  scheduleViewGrid: document.querySelector("#scheduleViewGrid"),
  scheduleViewList: document.querySelector("#scheduleViewList"),
  showPendingSwitch: document.querySelector("#showPendingSwitch"),
  enrollProgress: document.querySelector("#enrollProgress"),
  progressCounts: document.querySelector("#progressCounts"),
  progressBarFill: document.querySelector("#progressBarFill"),
  progressRows: document.querySelector("#progressRows"),
  progressState: document.querySelector("#progressState"),
  progressNotice: document.querySelector("#progressNotice"),
  taskControlButton: document.querySelector("#taskControlButton"),
  enrollModeLabel: document.querySelector("#enrollModeLabel"),
  enrollRuntimeStatus: document.querySelector("#enrollRuntimeStatus"),
  enrollControlHint: document.querySelector("#enrollControlHint"),
  pauseEnroll: document.querySelector("#pauseEnroll"),
  resumeEnroll: document.querySelector("#resumeEnroll"),
  stopEnroll: document.querySelector("#stopEnroll"),
  modeButtons: document.querySelectorAll("[data-enroll-mode]"),
  boostInterval: document.querySelector("#boostInterval"),
  normalInterval: document.querySelector("#normalInterval"),
  scanInterval: document.querySelector("#scanInterval"),
  boostFailureLimit: document.querySelector("#boostFailureLimit"),
  boostFailureUnlimited: document.querySelector("#boostFailureUnlimited"),
  normalFailureLimit: document.querySelector("#normalFailureLimit"),
  normalFailureUnlimited: document.querySelector("#normalFailureUnlimited"),
};

class ApiError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = "ApiError";
    this.status = Number(options.status || 0);
    this.code = options.code || "REQUEST_FAILED";
    this.retryable = Boolean(options.retryable);
    this.requiresManualLogin = Boolean(options.requiresManualLogin);
    this.payload = options.payload || {};
  }
}

class SessionExpiredError extends ApiError {
  constructor(message, options = {}) {
    super(message, options);
    this.name = "SessionExpiredError";
  }
}

function cleanPagePath(path) {
  const url = new URL(path, window.location.origin);
  url.searchParams.delete("ui");
  return `${url.pathname}${url.search}${url.hash}`;
}

function stripUiQuery() {
  if (!window.history?.replaceState || !window.location?.href) return;
  const url = new URL(window.location.href);
  if (!url.searchParams.has("ui")) return;
  url.searchParams.delete("ui");
  const search = url.searchParams.toString();
  window.history.replaceState(
    null,
    "",
    `${url.pathname}${search ? `?${search}` : ""}${url.hash}`,
  );
}

function courseRequestUrl(courseType, page, pageSize = 10, { cacheMode = appState.cacheMode } = {}) {
  const params = new URLSearchParams({
    type: String(courseType || ""),
    page: String(page),
    page_size: String(pageSize),
  });
  if (cacheMode) params.set("cache_mode", "true");
  return `/api/school/courses?${params.toString()}`;
}

function cacheTimestampLabel(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return "时间未知";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

function courseSummary(totalCount, courseCount, metadata = {}) {
  const base = `共 ${totalCount} 门课程，本页 ${courseCount} 门`;
  if (!metadata.cached) return base;
  return `缓存课程：${base}（最近更新 ${cacheTimestampLabel(metadata.cached_at)}）`;
}

function clearCacheRefreshTimer() {
  if (appState.cacheRefreshTimer) {
    window.clearInterval(appState.cacheRefreshTimer);
    appState.cacheRefreshTimer = null;
  }
}

function isOfflineCacheType(type = appState.type) {
  return OFFLINE_CACHE_TYPES.has(String(type || "").toUpperCase());
}

function setCacheMode(enabled, { load = true } = {}) {
  const wasEnabled = appState.cacheMode;
  appState.cacheMode = Boolean(enabled);
  if (appElements.cacheModeSwitch) appElements.cacheModeSwitch.checked = appState.cacheMode;
  if (!appState.session?.logged_in) {
    clearCacheRefreshTimer();
    if (appState.cacheMode && load && isOfflineCacheType() && !wasEnabled) {
      loadCourses({ cacheOnly: true });
    }
    return;
  }
  if (!appState.cacheMode) {
    clearCacheRefreshTimer();
    if (wasEnabled && appState.session?.logged_in) {
      if (isFilterActive()) {
        runSearchFetch({ force: true, preserveExisting: true, forceLive: true });
      } else {
        loadCourses({ preserveExisting: true, forceLive: true });
      }
    }
    return;
  }
  if (!appState.cacheRefreshTimer) {
    appState.cacheRefreshTimer = window.setInterval(
      refreshCoursesFromNetwork,
      CACHE_REFRESH_INTERVAL_MS,
    );
  }
  if (!wasEnabled && appState.session?.logged_in) {
    if (isFilterActive()) {
      runSearchFetch({ force: true, preserveExisting: true });
    } else {
      loadCourses({ preserveExisting: true });
    }
  }
}

function isAbortError(error) {
  return error instanceof DOMException && error.name === "AbortError";
}

function catalogScopeKey(session = appState.session) {
  if (!session?.logged_in) return "";
  return JSON.stringify([
    String(session.student_id || ""),
    String(session.batch_code || ""),
    String(session.batch_name || ""),
    String(session.campus_code || "01"),
  ]);
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function numericValue(value, fallback = 0) {
  const number = Number(String(value ?? "").replace(/,/g, "").trim());
  return Number.isFinite(number) ? number : fallback;
}

function intervalSecondsToMilliseconds(value, fallbackMilliseconds) {
  const seconds = numericValue(value, numericValue(fallbackMilliseconds, 0) / 1000);
  return Math.max(0, Math.round(seconds * 1000));
}

function intervalMillisecondsToSeconds(value, fallbackMilliseconds) {
  const milliseconds = numericValue(value, fallbackMilliseconds);
  return Math.max(0, Math.round(milliseconds / 1000));
}

function failureLimitFromInputs(input, unlimitedCheckbox, label) {
  if (unlimitedCheckbox?.checked) return null;
  const raw = String(input?.value ?? "").trim();
  const value = Number(raw);
  if (!raw || !Number.isInteger(value) || value < 1 || value > 1000000) {
    throw new Error(`${label}必须是 1–1,000,000 的整数，或勾选“无限次”。`);
  }
  return value;
}

function syncFailureLimitControl(input, unlimitedCheckbox, limit, fallback) {
  const unlimited = limit === null;
  if (unlimitedCheckbox) unlimitedCheckbox.checked = unlimited;
  if (!input) return;
  input.disabled = unlimited;
  if (!unlimited) input.value = String(limit);
  else if (!String(input.value || "").trim()) input.value = String(fallback);
}

function syncFailureLimitControls() {
  syncFailureLimitControl(
    appElements.boostFailureLimit,
    appElements.boostFailureUnlimited,
    appState.enroll.boostFailureLimit,
    5,
  );
  syncFailureLimitControl(
    appElements.normalFailureLimit,
    appElements.normalFailureUnlimited,
    appState.enroll.normalFailureLimit,
    10,
  );
}

function failureDowngradeHint(modeName, limit, targetName) {
  if (limit === null) return `${modeName}模式不自动降为${targetName}模式`;
  return `${modeName}模式业务失败 ${limit} 次降为${targetName}模式`;
}

function settingPayload() {
  return {
    boost_interval_ms: intervalSecondsToMilliseconds(appElements.boostInterval?.value, appState.enroll.boostIntervalMs),
    normal_interval_ms: intervalSecondsToMilliseconds(appElements.normalInterval?.value, appState.enroll.normalIntervalMs),
    scan_interval_ms: intervalSecondsToMilliseconds(appElements.scanInterval?.value, appState.enroll.scanIntervalMs),
    boost_failure_limit: failureLimitFromInputs(
      appElements.boostFailureLimit,
      appElements.boostFailureUnlimited,
      "爆发模式失败次数",
    ),
    normal_failure_limit: failureLimitFromInputs(
      appElements.normalFailureLimit,
      appElements.normalFailureUnlimited,
      "一般模式失败次数",
    ),
  };
}

function applyEnrollSettings(data = {}) {
  const source = data.settings || data;
  const mode = source.mode || data.mode || data.current_mode;
  if (["boost", "normal", "scan"].includes(mode)) appState.enroll.mode = mode;
  appState.enroll.running = Boolean(data.running ?? data.task_running ?? appState.enroll.running);
  appState.enroll.paused = Boolean(data.paused ?? data.task_paused ?? appState.enroll.paused);
  appState.enroll.boostIntervalMs = numericValue(source.boost_interval_ms, appState.enroll.boostIntervalMs);
  appState.enroll.normalIntervalMs = numericValue(source.normal_interval_ms, appState.enroll.normalIntervalMs);
  appState.enroll.scanIntervalMs = numericValue(source.scan_interval_ms, appState.enroll.scanIntervalMs);
  if (Object.prototype.hasOwnProperty.call(source, "boost_failure_limit")) {
    appState.enroll.boostFailureLimit = source.boost_failure_limit === null
      ? null
      : numericValue(source.boost_failure_limit, appState.enroll.boostFailureLimit);
  }
  if (Object.prototype.hasOwnProperty.call(source, "normal_failure_limit")) {
    appState.enroll.normalFailureLimit = source.normal_failure_limit === null
      ? null
      : numericValue(source.normal_failure_limit, appState.enroll.normalFailureLimit);
  }
  if (appElements.boostInterval) appElements.boostInterval.value = String(intervalMillisecondsToSeconds(appState.enroll.boostIntervalMs, 1000));
  if (appElements.normalInterval) appElements.normalInterval.value = String(intervalMillisecondsToSeconds(appState.enroll.normalIntervalMs, 10000));
  if (appElements.scanInterval) appElements.scanInterval.value = String(Math.max(1, intervalMillisecondsToSeconds(appState.enroll.scanIntervalMs, 60000)));
  syncFailureLimitControls();
  renderEnrollControls();
}

function planErrorMessage(error) {
  if (error.status === 404) return "当前后端尚未支持这项抢课控制接口。";
  if (error.status >= 500) return "后端暂时不可用，已有设置仍保留，请稍后重试。";
  return error.message || "抢课设置暂时没有生效。";
}

async function planApi(path, options = {}) {
  const data = await api(path, options);
  if (data?.is_error || data?.success === false) {
    throw new ApiError(data.message || "后端没有接受这项抢课设置", {
      code: data.error_code || "PLAN_API_REJECTED",
      payload: data,
    });
  }
  return data;
}

function renderEnrollControls() {
  const state = appState.enroll;
  const running = Boolean(state.running || appState.session?.task_running);
  const paused = Boolean(state.paused || appState.session?.task_paused);
  const mode = state.mode || "boost";
  if (appElements.enrollModeLabel) {
    appElements.enrollModeLabel.textContent = `${enrollModeNames[mode] || mode} · ${running ? (paused ? "已暂停" : "运行中") : "未运行"}`;
  }
  if (appElements.enrollRuntimeStatus) {
    appElements.enrollRuntimeStatus.textContent = running ? (paused ? "已暂停" : "抢课中") : "未运行";
    appElements.enrollRuntimeStatus.className = `status-pill ${running ? (paused ? "status-warning" : "status-success") : "status-neutral"}`;
  }
  if (appElements.enrollControlHint) {
    appElements.enrollControlHint.textContent = paused
      ? "任务已暂停，恢复后会保留当前课程进度和失败次数。"
      : `${failureDowngradeHint("爆发", appState.enroll.boostFailureLimit, "一般")}；${failureDowngradeHint("一般", appState.enroll.normalFailureLimit, "扫描")}；网络异常和 5xx 不计失败。`;
  }
  if (appElements.pauseEnroll) appElements.pauseEnroll.disabled = !running || paused;
  if (appElements.resumeEnroll) appElements.resumeEnroll.disabled = !running || !paused;
  if (appElements.stopEnroll) appElements.stopEnroll.disabled = !running || Boolean(appState.session?.task_stopping);
  for (const button of appElements.modeButtons || []) {
    const active = button.dataset.enrollMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

async function updateEnrollMode(mode) {
  if (!["boost", "normal", "scan"].includes(mode)) return;
  const previous = appState.enroll.mode;
  const previousSettings = {
    boostIntervalMs: appState.enroll.boostIntervalMs,
    normalIntervalMs: appState.enroll.normalIntervalMs,
    scanIntervalMs: appState.enroll.scanIntervalMs,
    boostFailureLimit: appState.enroll.boostFailureLimit,
    normalFailureLimit: appState.enroll.normalFailureLimit,
  };
  appState.enroll.mode = mode;
  renderEnrollControls();
  try {
    const data = await planApi("/api/enroll/mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    // A mode response must not replace the user's interval/risk settings with
    // missing values. The server now returns a full snapshot, while this
    // fallback keeps older backends from clearing the current form.
    applyEnrollSettings({
      ...data,
      mode,
      boost_interval_ms: data.settings?.boost_interval_ms ?? previousSettings.boostIntervalMs,
      normal_interval_ms: data.settings?.normal_interval_ms ?? previousSettings.normalIntervalMs,
      scan_interval_ms: data.settings?.scan_interval_ms ?? previousSettings.scanIntervalMs,
      boost_failure_limit: Object.prototype.hasOwnProperty.call(data.settings || {}, "boost_failure_limit")
        ? data.settings.boost_failure_limit
        : previousSettings.boostFailureLimit,
      normal_failure_limit: Object.prototype.hasOwnProperty.call(data.settings || {}, "normal_failure_limit")
        ? data.settings.normal_failure_limit
        : previousSettings.normalFailureLimit,
    });
    showToast(`已切换为${enrollModeNames[mode]}`, false, true);
  } catch (error) {
    appState.enroll.mode = previous;
    renderEnrollControls();
    showToast(planErrorMessage(error), true);
  }
}

async function toggleEnrollPause(paused) {
  try {
    const data = await planApi(paused ? "/api/enroll/pause" : "/api/enroll/resume", { method: "POST" });
    appState.enroll.running = true;
    appState.enroll.paused = paused;
    applyEnrollSettings({ ...data, running: true, paused });
    await loadEnrollProgress();
    showToast(paused ? "已请求暂停抢课，等待当前请求结束" : "抢课已恢复", false, true);
  } catch (error) {
    showToast(planErrorMessage(error), true);
  }
}

async function saveEnrollSettings() {
  try {
    const payload = settingPayload();
    const data = await planApi("/api/enroll/settings", {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    applyEnrollSettings(data);
    showToast("抢课设置已保存", false, true);
  } catch (error) {
    syncFailureLimitControls();
    showToast(planErrorMessage(error), true);
  }
}

function preferenceFor(item) {
  const saved = appState.cartPreferences[String(item.id)] || {};
  const fallbackGroup = [item.course_number, item.time_signature]
    .filter(Boolean)
    .join("|") || item.course_number || "未分组课程";
  const storedGroup = saved.priorityGroup ?? item.priority_group;
  return {
    autoEnabled: saved.autoEnabled ?? saved.auto_enabled ?? item.auto_enabled ?? item.autoEnabled ?? true,
    priorityGroup: String(storedGroup || fallbackGroup),
    priorityRank: numericValue(saved.priorityRank ?? saved.priority_rank ?? item.priority_rank ?? item.priorityRank, 0),
  };
}

function ensureCartPreferenceRanks() {
  const nextRanks = new Map();
  let changed = false;
  for (const item of appState.cart) {
    const preference = preferenceFor(item);
    const hasRank = appState.cartPreferences[String(item.id)]?.priorityRank != null
      || item.priority_rank != null;
    if (hasRank) continue;
    const rank = nextRanks.get(preference.priorityGroup) || 0;
    nextRanks.set(preference.priorityGroup, rank + 1);
    appState.cartPreferences[String(item.id)] = { ...preference, priorityRank: rank };
    changed = true;
  }
  if (changed) {
    try {
      window.localStorage?.setItem("szu-course-help.cart-preferences.v1", JSON.stringify(appState.cartPreferences));
    } catch {}
  }
}

function updateLocalCartPreference(item, values) {
  const current = preferenceFor(item);
  const normalized = {
    ...current,
    ...(values.auto_enabled !== undefined || values.autoEnabled !== undefined
      ? { autoEnabled: Boolean(values.auto_enabled ?? values.autoEnabled) }
      : {}),
    ...(values.priority_group !== undefined || values.priorityGroup !== undefined
      ? { priorityGroup: String(values.priority_group ?? values.priorityGroup ?? "") }
      : {}),
    ...(values.priority_rank !== undefined || values.priorityRank !== undefined
      ? { priorityRank: numericValue(values.priority_rank ?? values.priorityRank, current.priorityRank) }
      : {}),
  };
  appState.cartPreferences[String(item.id)] = normalized;
  try {
    window.localStorage?.setItem("szu-course-help.cart-preferences.v1", JSON.stringify(appState.cartPreferences));
  } catch {}
}

function classIsFull(classInfo) {
  if (String(classInfo.is_full || "") === "1") return true;
  const selected = numericValue(classInfo.number_of_selected, NaN);
  const capacity = numericValue(classInfo.class_capacity, NaN);
  return Number.isFinite(selected) && Number.isFinite(capacity) && selected >= capacity;
}

function classIsSelected(classInfo) {
  return String(classInfo.is_choose || "") === "1" || classInfo.is_choose === true;
}

function classHasConflict(classInfo) {
  return String(classInfo.is_conflict || "") === "1"
    || classInfo.is_conflict === true
    || String(classInfo.conflict || "") === "1";
}

function readFilterPreferences() {
  try {
    const saved = JSON.parse(window.localStorage?.getItem(FILTER_PREFERENCES_KEY) || "null");
    return {
      hideConflict: saved?.hideConflict === true,
      hideFull: saved?.hideFull === true,
    };
  } catch {
    return { hideConflict: false, hideFull: false };
  }
}

function saveFilterPreferences() {
  try {
    window.localStorage?.setItem(FILTER_PREFERENCES_KEY, JSON.stringify(appState.filters));
  } catch {
    // Private browsing or a full storage quota should not break filtering.
  }
}

function visibleTeachingClasses(course) {
  const classes = Array.isArray(course.tcList) ? course.tcList : [];
  return classes.filter((classInfo) => {
    if (classIsSelected(classInfo)) return true;
    if (appState.filters.hideConflict && classHasConflict(classInfo)) return false;
    if (appState.filters.hideFull && classIsFull(classInfo)) return false;
    return true;
  });
}

function courseHasVisibleClasses(course) {
  const classes = Array.isArray(course.tcList) ? course.tcList : [];
  // 没有教学班属于学校数据为空，保留“暂无教学班”展示，不属于被筛选隐藏。
  if (!classes.length) return true;
  return visibleTeachingClasses(course).length > 0;
}

function campusOptions(session = appState.session) {
  return Array.isArray(session?.campus_options)
    ? session.campus_options.filter((item) => item?.code && item?.name)
    : [];
}

function syncCampusControl() {
  const options = campusOptions();
  appElements.campusSelect.disabled = appState.switchingCampus
    || !appState.session?.logged_in
    || Boolean(appState.session?.relogin_in_progress)
    || options.length < 2;
}

function renderCampusOptions(session = appState.session) {
  const options = campusOptions(session);
  const selectedCode = String(session?.campus_code || "01");
  const signature = JSON.stringify([
    selectedCode,
    options.map((item) => [String(item.code), String(item.name)]),
  ]);
  if (appElements.campusSelect.dataset.signature === signature) {
    appElements.campusSelect.value = selectedCode;
    syncCampusControl();
    return;
  }
  const fragment = document.createDocumentFragment();
  if (!options.length) {
    const option = element(
      "option",
      "",
      session?.campus_name || "重启程序后可切换校区",
    );
    option.value = selectedCode;
    fragment.append(option);
  } else {
    for (const campus of options) {
      const option = element("option", "", campus.name);
      option.value = String(campus.code);
      fragment.append(option);
    }
  }
  appElements.campusSelect.replaceChildren(fragment);
  appElements.campusSelect.dataset.signature = signature;
  appElements.campusSelect.value = selectedCode;
  syncCampusControl();
}

async function switchCampus(nextCode) {
  const normalizedCode = String(nextCode || "").trim();
  const previousCode = String(appState.session?.campus_code || "01");
  if (!normalizedCode || normalizedCode === previousCode || appState.switchingCampus) return;

  appState.switchingCampus = true;
  syncCampusControl();
  appState.courseRequestId += 1;
  appState.courseRequestController?.abort();
  appState.courseRequestController = null;
  abortCatalogFetch();
  try {
    const session = await api("/api/session/campus", {
      method: "POST",
      body: JSON.stringify({ campus_code: normalizedCode }),
    });
    appState.page = 1;
    appState.searchKeyword = "";
    appState.searchResults = [];
    appState.searchPage = 1;
    appState.catalogBlockedCode = "";
    appElements.courseSearch.value = "";
    applySessionData(session);
    showToast(session.message || `已切换到${session.campus_name || "新校区"}`, false, true);
    if (courseCatalogBlocked()) renderCourseAvailabilityState();
    else await loadCourses();
  } catch (error) {
    appElements.campusSelect.value = previousCode;
    if (!(error instanceof SessionExpiredError)) showToast(`校区切换失败：${error.message}`, true);
  } finally {
    appState.switchingCampus = false;
    syncCampusControl();
  }
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

async function api(url, options = {}) {
  const upstreamSignal = options.signal;
  const timeoutMs = Number(options.timeoutMs || REQUEST_TIMEOUT_MS);
  const controller = new AbortController();
  const abortFromUpstream = () => controller.abort();
  if (upstreamSignal?.aborted) controller.abort();
  upstreamSignal?.addEventListener("abort", abortFromUpstream, { once: true });
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  const {
    signal: _ignoredSignal,
    timeoutMs: _ignoredTimeout,
    ...fetchOptions
  } = options;

  try {
    const response = await fetch(url, {
      cache: "no-store",
      ...fetchOptions,
      signal: controller.signal,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const data = await readJson(response);
    if (response.status === 401) {
      const message = data.message || "登录已过期，请重新登录";
      showSessionDialog(message);
      throw new SessionExpiredError(message, {
        status: response.status,
        code: data.error_code || "NOT_LOGGED_IN",
        retryable: data.retryable,
        requiresManualLogin: data.requires_manual_login,
        payload: data,
      });
    }
    if (!response.ok) {
      throw new ApiError(data.message || data.detail || "请求失败，请稍后重试", {
        status: response.status,
        code: data.error_code,
        retryable: data.retryable,
        requiresManualLogin: data.requires_manual_login,
        payload: data,
      });
    }
    return data;
  } catch (error) {
    if (controller.signal.aborted && !upstreamSignal?.aborted) {
      throw new ApiError("请求超时，请检查网络后重试", {
        code: "REQUEST_TIMEOUT",
        retryable: true,
      });
    }
    if (error instanceof ApiError || isAbortError(error)) throw error;
    if (error instanceof TypeError) {
      throw new ApiError("无法连接本地服务，请确认程序仍在运行", {
        code: "LOCAL_SERVICE_UNAVAILABLE",
        retryable: true,
      });
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    upstreamSignal?.removeEventListener("abort", abortFromUpstream);
  }
}

function showToast(message, error = false, success = false) {
  const variant = error ? " is-error" : success ? " is-success" : "";
  const toast = element("div", `toast${variant}`, message);
  appElements.toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), success ? 4600 : 3600);
}

function showSessionDialog(message) {
  appElements.sessionMessage.textContent = message;
  appElements.sessionLoginLink.href = cleanPagePath("/login");
  if (!appElements.sessionDialog.open) appElements.sessionDialog.showModal();
}

function hideSessionDialog() {
  if (appElements.sessionDialog.open) appElements.sessionDialog.close();
}

function hideRecoveryBanner() {
  appElements.sessionRecoveryBanner.hidden = true;
  appElements.sessionRecoveryBanner.setAttribute("aria-busy", "false");
}

function renderSessionRecovery(session, previousStatus = "idle") {
  const status = String(session?.relogin_status || "idle");
  const taskPaused = Boolean(session?.task_paused);
  const taskRunning = Boolean(session?.task_running);
  const finishedAt = String(session?.relogin_finished_at || "success");
  appElements.recoveryLoginLink.href = cleanPagePath("/login");

  if (status === "idle") {
    hideRecoveryBanner();
    return;
  }

  if (status === "running") {
    if (appState.recoveryHideTimer) {
      window.clearTimeout(appState.recoveryHideTimer);
      appState.recoveryHideTimer = null;
    }
    appElements.sessionRecoveryBanner.hidden = false;
    appElements.sessionRecoveryBanner.className = "session-recovery-banner is-running";
    appElements.sessionRecoveryBanner.setAttribute("aria-busy", "true");
    appElements.recoveryTitle.textContent = "正在自动重新登录";
    appElements.recoveryDetail.textContent = session.relogin_message
      || `学校会话已过期，OCR 最多尝试 ${session.relogin_max_attempts || 50} 张验证码；任务和清单会保留。`;
    appElements.recoveryLoginLink.hidden = true;
    return;
  }

  appElements.sessionRecoveryBanner.setAttribute("aria-busy", "false");
  if (status === "success") {
    hideSessionDialog();
    if (appState.recoveryDismissedAt === finishedAt) {
      hideRecoveryBanner();
      return;
    }
    appElements.sessionRecoveryBanner.hidden = false;
    appElements.sessionRecoveryBanner.className = "session-recovery-banner is-success";
    appElements.recoveryTitle.textContent = "自动重新登录成功";
    appElements.recoveryDetail.textContent = "学校会话已经恢复，页面数据与抢课任务会自动继续。";
    appElements.recoveryLoginLink.hidden = true;
    if (previousStatus === "running") {
      showToast("自动重新登录成功，已恢复学校会话", false, true);
    }
    if (!appState.recoveryHideTimer) {
      appState.recoveryHideTimer = window.setTimeout(() => {
        appState.recoveryDismissedAt = finishedAt;
        appState.recoveryHideTimer = null;
        hideRecoveryBanner();
      }, 6000);
    }
    return;
  }

  if (appState.recoveryHideTimer) {
    window.clearTimeout(appState.recoveryHideTimer);
    appState.recoveryHideTimer = null;
  }
  appElements.sessionRecoveryBanner.hidden = false;
  appElements.sessionRecoveryBanner.className = "session-recovery-banner is-error";
  const failureCount = Number(session.relogin_failure_count || 0);
  const maxRetries = Number(session.relogin_max_retries || 5);
  const retryAfter = Number(session.relogin_retry_after || 0);
  const recoveryExhausted = failureCount >= maxRetries;
  appElements.recoveryTitle.textContent = taskRunning && !taskPaused
    ? "自动重新登录暂未成功"
    : "自动重新登录失败";
  const retryHint = recoveryExhausted
    ? "自动重登录已停止，请手动登录。"
    : `后台将在${retryAfter > 0 ? `${retryAfter}秒后` : "稍后"}继续尝试。`;
  appElements.recoveryDetail.textContent = taskRunning && !taskPaused
    ? `${session.relogin_message || "OCR 暂未识别成功"}；${retryHint}`
    : `${session.relogin_message || "无法恢复学校会话"}；请手动登录后返回清单继续任务。`;
  appElements.recoveryLoginLink.hidden = taskRunning && !taskPaused && !recoveryExhausted;
}

function renderLoading() {
  const wrapper = element("div", "loading-list");
  for (let index = 0; index < 4; index += 1) {
    wrapper.append(element("div", "loading-row"));
  }
  appElements.courseList.replaceChildren(wrapper);
}

function renderState(title, message, options = {}) {
  const wrapper = element("div", options.tone === "error" ? "error-state" : "empty-state");
  wrapper.append(element("strong", "", title));
  wrapper.append(element("p", "", message));
  if (options.note) wrapper.append(element("span", "state-note", options.note));
  if (Array.isArray(options.actions) && options.actions.length) {
    const actions = element("div", "state-actions");
    for (const action of options.actions) {
      const button = element(
        "button",
        action.primary ? "button button-primary" : "button button-secondary",
        action.label,
      );
      button.type = "button";
      button.addEventListener("click", action.handler);
      actions.append(button);
    }
    wrapper.append(actions);
  }
  appElements.courseList.replaceChildren(wrapper);
}

function courseCatalogBlocked() {
  return Boolean(
    appState.closedPhase
      || !appState.session?.batch_code
      || ["COURSE_WINDOW_CLOSED", "BATCH_UNAVAILABLE"].includes(appState.catalogBlockedCode),
  );
}

function renderCourseAvailabilityState(message = "") {
  appState.courses = [];
  appState.totalCount = 0;
  appState.courseDataKey = "";
  appState.courseCacheMeta = null;
  appState.searchKeyword = "";
  appState.searchResults = [];
  appState.searchPage = 1;
  appElements.courseSearch.value = "";
  invalidateCatalogCaches();
  appElements.courseTypeCode.textContent = appState.type;
  appElements.courseTitle.textContent = categoryNames[appState.type] || appState.type;
  appElements.courseSearch.disabled = true;
  const closed = appState.closedPhase || appState.catalogBlockedCode === "COURSE_WINDOW_CLOSED";
  appElements.courseSummary.textContent = closed
    ? "课程目录当前不可用，本地清单已保留"
    : "等待学校返回有效选课批次";
  updatePagination();

  if (closed) {
    renderState(
      "当前未开放课程目录",
      message || "你已经登录，但学校当前批次显示为未开放、暂停或已结束。课程目录暂时不可读取。",
      {
        note: "已加入的本地选课清单不会丢失。开放后重新检查状态即可继续浏览。",
        actions: [{ label: "重新检查开放状态", handler: refreshPhaseAndCourses, primary: true }],
      },
    );
    return;
  }

  renderState(
    "暂未读取到选课批次",
    message || "登录已经成功，但学校当前没有返回可用的选课批次，可能尚未开放或服务正在波动。",
    {
      note: "这不代表登录失败，也不会影响本地选课清单。",
      actions: [{ label: "重新检查开放状态", handler: refreshPhaseAndCourses, primary: true }],
    },
  );
}

function setPhasePresentation() {
  const batch = (appState.session?.batch_name || "").trim();
  const catalogReportedClosed = appState.catalogBlockedCode === "COURSE_WINDOW_CLOSED";
  const taskWindowPaused = Boolean(
    appState.session?.task_paused && appState.session?.task_pause_source === "school_window",
  );
  const taskPauseAcknowledged = Boolean(appState.session?.task_pause_acknowledged);
  appState.preselection = appState.session?.phase === "preselection" && !catalogReportedClosed;
  appState.closedPhase = appState.session?.phase === "closed" || catalogReportedClosed;
  appState.grabPhase = Boolean(appState.session?.automatic_enroll_allowed);
  appElements.phaseBanner.classList.remove("is-warning", "is-danger");
  appElements.phaseBadge.className = "status-pill status-neutral";

  if (appState.preselection) {
    appElements.phaseBanner.classList.add("is-danger");
    appElements.phaseTitle.textContent = "当前为预选阶段";
    appElements.phaseDescription.textContent = "可以浏览课程并整理清单，后端已禁止启动自动抢课。";
    appElements.phaseBadge.textContent = batch || "预选";
    appElements.phaseBadge.className = "status-pill status-danger";
  } else if (appState.closedPhase) {
    appElements.phaseBanner.classList.add("is-warning");
    appElements.phaseTitle.textContent = "当前不在开放选课时间";
    appElements.phaseDescription.textContent = "学校批次显示为未开放、暂停或已结束；本地清单会保留。";
    appElements.phaseBadge.textContent = batch || "未开放";
    appElements.phaseBadge.className = "status-pill status-warning";
  } else if (taskWindowPaused) {
    appElements.phaseBanner.classList.add("is-warning");
    appElements.phaseTitle.textContent = `批次为${batch || "可抢阶段"}，但当前时段未开放`;
    appElements.phaseDescription.textContent = appState.session?.task_pause_reason
      || "学校拒绝了本次提交，任务已暂停；开放后可在清单中继续。";
    appElements.phaseBadge.textContent = taskPauseAcknowledged ? "任务已暂停" : "正在暂停";
    appElements.phaseBadge.className = "status-pill status-warning";
  } else if (appState.grabPhase) {
    appElements.phaseTitle.textContent = `当前批次：${batch}`;
    appElements.phaseDescription.textContent = "启动前仍需在清单中再次确认阶段。抢到的课程会实时加入你的课程。";
    appElements.phaseBadge.textContent = batch;
    appElements.phaseBadge.className = "status-pill status-success";
  } else {
    appElements.phaseBanner.classList.add("is-warning");
    appElements.phaseTitle.textContent = batch ? `当前批次：${batch}` : "暂未读取到选课批次";
    appElements.phaseDescription.textContent = "该批次不在自动抢课白名单内，只能浏览和整理课程。";
    appElements.phaseBadge.textContent = batch || "未知批次";
    appElements.phaseBadge.className = "status-pill status-warning";
  }
  syncEnrollControls();
}

function applySessionData(session) {
  const previousReloginStatus = appState.lastReloginStatus;
  const previousCatalogScope = catalogScopeKey(appState.session);
  const nextCatalogScope = catalogScopeKey(session);
  const reloginCompleted = Boolean(appState.session)
    && String(session.relogin_status || "idle") === "success"
    && previousReloginStatus !== "success";
  const catalogContextChanged = Boolean(
    (previousCatalogScope && previousCatalogScope !== nextCatalogScope)
    || reloginCompleted
  );
  if (catalogContextChanged) {
    invalidateCatalogCaches();
    appState.courses = [];
    appState.totalCount = 0;
    appState.courseDataKey = "";
    appState.courseCacheMeta = null;
  }
  appState.session = session;
  if (!session?.logged_in && appState.cacheMode) clearCacheRefreshTimer();
  renderCampusOptions(session);
  appState.lastReloginStatus = String(session.relogin_status || "idle");
  const reloginAvailable = !session.logged_in
    && !session.relogin_in_progress
    && Number(session.relogin_failure_count || 0)
      < Number(session.relogin_max_retries || 5);
  appElements.studentLabel.textContent = session.relogin_in_progress
    ? "正在恢复登录"
    : session.logged_in
      ? `学号 ${session.student_id}`
      : "未登录";
  appElements.studentLabel.disabled = !reloginAvailable;
  appElements.studentLabel.classList.toggle("is-actionable", reloginAvailable);
  appElements.studentLabel.title = reloginAvailable
    ? "点击立即尝试自动重新登录"
    : session.logged_in
      ? "当前已登录"
      : "自动重登录暂不可用，请稍候或手动登录";
  renderSessionRecovery(session, previousReloginStatus);
  if (session.logged_in && !session.relogin_in_progress) hideSessionDialog();
  updateTaskIndicator();
  setPhasePresentation();
  if (session.task_running) startProgressPolling();
  return catalogContextChanged;
}

async function requestAutomaticRelogin(options = {}) {
  const automatic = Boolean(options?.automatic);
  if (!appState.session || appState.session.logged_in || appState.session.relogin_in_progress) return;
  if (appState.reloginRequestPending) return;
  if (!automatic && appElements.studentLabel.disabled) return;
  appState.reloginRequestPending = true;
  appElements.studentLabel.disabled = true;
  appElements.studentLabel.classList.remove("is-actionable");
  appElements.studentLabel.textContent = "正在恢复登录";
  try {
    const session = await api("/api/session/recover", {
      method: "POST",
      timeoutMs: SESSION_RECOVERY_TIMEOUT_MS,
    });
    applySessionData(session);
    showToast(session.message || "已开始自动重新登录，请稍候", false, true);
  } catch (error) {
    if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
    await loadSession(false, false);
  } finally {
    appState.reloginRequestPending = false;
    if (appState.session) applySessionData(appState.session);
  }
}

async function maybeStartAutomaticRelogin() {
  const session = appState.session;
  if (!session || session.logged_in || session.relogin_in_progress) return;
  if (session.relogin_status === "failed") return;
  if (Number(session.relogin_failure_count || 0) >= Number(session.relogin_max_retries || 5)) return;
  if (appState.reloginRequestPending) return;

  if (!session.automatic_relogin_available || appState.session !== session) return;
  void requestAutomaticRelogin({ automatic: true });
}

function cartEditStateKey() {
  const session = appState.session;
  return [
    Boolean(session?.task_running),
    Boolean(session?.task_paused),
    Boolean(session?.task_pause_acknowledged),
    Boolean(session?.task_stopping),
  ].join(":");
}

function canMutateQueue() {
  const session = appState.session;
  if (!session?.task_running) return true;
  return Boolean(
    session.task_paused
    && session.task_pause_acknowledged
    && !session.task_stopping
  );
}

function applyProgressTaskState(data) {
  const previousCartEditState = cartEditStateKey();
  const previousQueueRevision = Number(appState.session?.task_queue_revision || 0);
  appState.progress = data;
  if (appState.session && data) {
    appState.session.task_running = Boolean(data.running);
    appState.session.task_paused = Boolean(data.paused);
    appState.session.task_pause_acknowledged = Boolean(data.pause_acknowledged);
    appState.session.task_pause_reason = data.pause_reason || "";
    appState.session.task_pause_source = data.pause_source || "";
    appState.session.task_stopping = Boolean(data.stopping);
    appState.session.task_stopping_reason = data.stopping_reason || "";
    appState.session.task_queue_revision = Number(
      data.queue_revision ?? previousQueueRevision,
    );
  }
  return {
    controlsChanged: previousCartEditState !== cartEditStateKey(),
    queueChanged: Number(appState.session?.task_queue_revision || 0) !== previousQueueRevision,
  };
}

function updateTaskIndicator() {
  const running = Boolean(appState.progress?.running ?? appState.session?.task_running);
  const paused = Boolean(appState.progress?.paused ?? appState.session?.task_paused);
  const pauseAcknowledged = Boolean(
    appState.progress?.pause_acknowledged ?? appState.session?.task_pause_acknowledged,
  );
  const stopping = Boolean(appState.progress?.stopping ?? appState.session?.task_stopping);
  const relogin = Boolean(appState.session?.relogin_in_progress);
  appElements.taskIndicator.hidden = !running;
  if (!running) return;
  appElements.taskIndicator.classList.toggle("is-paused", paused);
  appElements.taskIndicator.classList.toggle("is-relogin", relogin);
  appElements.taskIndicator.classList.toggle("is-stopping", stopping);
  if (stopping) {
    appElements.taskIndicator.textContent = "任务正在结束";
    return;
  }
  if (relogin) {
    appElements.taskIndicator.textContent = "正在重新登录";
    return;
  }
  if (paused) {
    appElements.taskIndicator.textContent = pauseAcknowledged ? "任务已暂停" : "正在暂停";
    return;
  }
  const counts = appState.progress?.counts;
  appElements.taskIndicator.textContent = counts
    ? `抢课中 ${counts.success}/${counts.total}`
    : "抢课中";
}

async function loadSession(showDialog = true, refreshOnCatalogChange = true) {
  if (appState.loadingSession) return appState.session;
  appState.loadingSession = true;
  const previousTaskState = Boolean(appState.session?.task_running);
  try {
    const session = await api("/api/session");
    const catalogContextChanged = applySessionData(session);
    void maybeStartAutomaticRelogin();
    if (
      !appState.session.logged_in
      && !appState.session.relogin_in_progress
      && appState.session.relogin_status !== "failed"
      && showDialog
    ) {
      showSessionDialog("当前没有有效登录状态，请返回登录页完成登录。");
    }
    if (previousTaskState !== Boolean(appState.session.task_running)) {
      await loadCart();
    }
    if (catalogContextChanged) {
      if (!appState.session.logged_in) {
        appState.searchKeyword = "";
        appElements.courseSearch.value = "";
        if (isOfflineCacheType()) {
          await loadCourses({ cacheOnly: true });
        } else {
          appElements.courseSearch.disabled = true;
          renderState("登录状态已失效", "请返回登录页完成登录后再读取课程目录。");
          updatePagination();
        }
      } else if (courseCatalogBlocked()) {
        renderCourseAvailabilityState();
      } else if (refreshOnCatalogChange) {
        if (!appState.session.task_running) {
          await refreshCurrentView();
        } else {
          appElements.courseSummary.textContent = "课程状态需要重新加载";
          renderState(
            "登录状态或选课批次已更新",
            "抢课任务仍在运行；为避免额外占用学校接口，请在需要时手动刷新课程。",
            {
              actions: [
                { label: "重新加载课程", handler: refreshCurrentView, primary: true },
              ],
            },
          );
          updatePagination();
        }
      }
    }
    return appState.session;
  } catch (error) {
    if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
    return appState.session;
  } finally {
    appState.loadingSession = false;
  }
}

function classTag(classInfo) {
  if (classIsSelected(classInfo)) return ["已选", "tag-chosen"];
  if (classHasConflict(classInfo)) return ["时间冲突", "tag-conflict"];
  if (classIsFull(classInfo)) return ["已满", "tag-full"];
  return ["可加入", "tag-open"];
}

function appendClassRow(container, course, classInfo) {
  const row = element("div", "class-row");
  const primary = element("div", "class-primary");
  primary.append(element("strong", "", classInfo.teacher_name || "教师待定"));
  primary.append(element("span", "", classInfo.course_index || classInfo.teaching_class_id));

  const location = element("div", "class-location");
  location.append(element("strong", "", classInfo.teaching_place || "时间地点待定"));
  location.append(element("span", "", classInfo.sport_name || (String(classInfo.is_mooc) === "1" ? "线上课程" : "教学安排")));

  const selected = classInfo.number_of_selected || classInfo.course_total_number || "-";
  const capacityValue = classInfo.class_capacity || "-";
  const capacity = element("div", "class-capacity");
  capacity.append(element("strong", "", `${selected} / ${capacityValue}`));
  capacity.append(element("span", "", "已选 / 容量"));

  const actions = element("div", "class-actions");
  const [tagText, tagClass] = classTag(classInfo);
  const statusTag = classHasConflict(classInfo) && !classIsSelected(classInfo)
    ? element("button", `class-tag ${tagClass} class-tag-button`, tagText)
    : element("span", `class-tag ${tagClass}`, tagText);
  if (statusTag.tagName === "BUTTON") {
    statusTag.type = "button";
    statusTag.title = "查看冲突课表";
    statusTag.addEventListener("click", () => openConflictTimetable(course, classInfo));
  }
  actions.append(statusTag);

  const alreadyInCart = appState.cart.some(
    (item) => String(item.id) === String(classInfo.teaching_class_id || ""),
  );
  const blocked = classIsSelected(classInfo) || classHasConflict(classInfo) || alreadyInCart;
  const addButton = element(
    "button",
    "button button-secondary",
    alreadyInCart
      ? "已在清单"
      : blocked
        ? (classIsSelected(classInfo) ? "已选" : "不可加入")
        : (classIsFull(classInfo) ? "加入候补" : "加入清单"),
  );
  addButton.type = "button";
  addButton.disabled = blocked || !canMutateQueue();
  if (!blocked && addButton.disabled) {
    addButton.title = appState.session?.task_paused
      ? "正在完成当前请求，请等待安全暂停"
      : "请先暂停抢课任务";
  }
  addButton.addEventListener("click", async () => {
    addButton.disabled = true;
    try {
      const result = await api("/api/courses/add", {
        method: "POST",
        body: JSON.stringify({
          id: String(classInfo.teaching_class_id || ""),
          type: appState.type,
          name: `${course.course_name || "未命名课程"} (${classInfo.teacher_name || "教师待定"})`,
          campus_code: String(appState.session?.campus_code || "01"),
          campus_name: String(appState.session?.campus_name || course.campus_name || ""),
          teaching_place: String(classInfo.teaching_place || ""),
          course_name: String(course.course_name || ""),
          teacher_name: String(classInfo.teacher_name || ""),
          credit: String(course.credit || ""),
          course_number: String(classInfo.course_number || course.course_number || ""),
          time_signature: String(classInfo.time_signature || ""),
          auto_enabled: true,
          is_choose: String(classInfo.is_choose || ""),
          is_conflict: String(classInfo.is_conflict || ""),
          is_full: String(classInfo.is_full || ""),
        }),
      });
      if (result.is_error) throw new Error(result.message);
      showToast(result.message || "已加入选课清单");
      await loadCart();
    } catch (error) {
      if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
    } finally {
      const nowInCart = appState.cart.some(
        (item) => String(item.id) === String(classInfo.teaching_class_id || ""),
      );
      if (nowInCart) addButton.textContent = "已在清单";
      addButton.disabled = blocked || nowInCart || !canMutateQueue();
    }
  });
  actions.append(addButton);
  row.append(primary, location, capacity, actions);
  container.append(row);
}

function isFilterActive() {
  return appState.searchKeyword.length > 0;
}

function courseMatchesKeyword(course, keyword) {
  const teachingText = visibleTeachingClasses(course)
    .map((item) => `${item.teacher_name || ""} ${item.teaching_place || ""}`)
    .join(" ");
  return `${course.course_name || ""} ${course.course_number || ""} ${course.department_name || ""} ${teachingText}`
    .toLowerCase()
    .includes(keyword);
}

appState.filters = readFilterPreferences();

function renderCourseList(courses) {
  const fragment = document.createDocumentFragment();
  courses.forEach((course, index) => {
    const selected = (course.tcList || []).some((classInfo) => classIsSelected(classInfo));
    const details = element("details", `course-group${selected ? " is-selected" : ""}`);
    details.style.animationDelay = `${Math.min(index, 8) * 40}ms`;
    const summary = element("summary");
    const main = element("div", "course-summary-main");
    main.append(element("strong", "", course.course_name || "未命名课程"));
    main.append(
      element(
        "span",
        "",
        [
          course.course_number,
          course.course_type_name,
          course.course_nature_name,
          course.department_name,
          course.credit ? `${course.credit} 学分` : "",
        ]
          .filter(Boolean)
          .join(" · ") || "课程信息待定",
      ),
    );
    const side = element("div", "course-summary-side");
    const visibleClasses = visibleTeachingClasses(course);
    side.append(element("span", "", `${visibleClasses.length} / ${(course.tcList || []).length} 个教学班`));
    side.append(element("i", "course-chevron"));
    summary.append(main, side);

    const classes = element("div", "class-list");
    if (!(course.tcList || []).length) {
      classes.append(element("div", "empty-state", "暂无教学班"));
    } else {
      for (const classInfo of visibleClasses) appendClassRow(classes, course, classInfo);
    }
    details.append(summary, classes);
    fragment.append(details);
  });
  appElements.courseList.replaceChildren(fragment);
}

function applyCourseFilter() {
  const cache = appState.catalogCaches[appState.type];
  if (!cache || !cache.complete || cache.scopeKey !== catalogScopeKey()) return;
  const keyword = appState.searchKeyword.toLowerCase();
  const results = keyword
    ? cache.courses
      .filter((course) => courseMatchesKeyword(course, keyword))
      .filter((course) => courseHasVisibleClasses(course))
    : [];
  appState.searchResults = results;
  const totalPages = Math.max(1, Math.ceil(results.length / FILTER_PAGE_SIZE));
  if (appState.searchPage > totalPages) appState.searchPage = totalPages;
  const pageItems = results.slice(
    (appState.searchPage - 1) * FILTER_PAGE_SIZE,
    appState.searchPage * FILTER_PAGE_SIZE,
  );
  const prefix = cache.cached
    ? `缓存课程（最近更新 ${cacheTimestampLabel(cache.cachedAt)}）：`
    : "";
  appElements.courseSummary.textContent = results.length
    ? `${prefix}匹配 ${results.length} 门课程（全部 ${cache.totalCount} 门），本页 ${pageItems.length} 门`
    : `${prefix}没有匹配课程，全部目录共 ${cache.totalCount} 门`;
}

function renderFilteredCourses() {
  const cache = appState.catalogCaches[appState.type];
  if (!cache || !cache.complete || cache.scopeKey !== catalogScopeKey()) {
    if (appState.loadingCatalog && appState.catalogLoadingType === appState.type) {
      renderState(
        "正在加载全部课程",
        appElements.courseSummary.textContent || "正在读取学校课程数据，加载完成后即可搜索整个目录。",
      );
    } else {
      renderState("课程目录尚未加载", "暂时无法在全部课程中搜索，请重新加载后重试。", {
        tone: "error",
        actions: [
          { label: "重新加载课程", handler: () => runSearchFetch({ force: true }), primary: true },
        ],
      });
    }
    updatePagination();
    return;
  }

  const results = Array.isArray(appState.searchResults) ? appState.searchResults : [];
  if (!results.length) {
    const filtersOn = appState.filters.hideConflict || appState.filters.hideFull;
    renderState(
      "没有匹配的课程",
      filtersOn
        ? "换个关键词，或关闭“不显示时间冲突”“不显示人数已满”后重试。"
        : "换一个关键词，或切换课程目录后重试。",
    );
    updatePagination();
    return;
  }

  const totalPages = Math.max(1, Math.ceil(results.length / FILTER_PAGE_SIZE));
  if (appState.searchPage > totalPages) appState.searchPage = totalPages;
  const start = (appState.searchPage - 1) * FILTER_PAGE_SIZE;
  renderCourseList(results.slice(start, start + FILTER_PAGE_SIZE));
  updatePagination();
}

function renderCourses() {
  if (isFilterActive()) {
    renderFilteredCourses();
    return;
  }
  if (!appState.courses.length) {
    renderState("本页没有课程", "学校系统当前没有返回该目录的课程。");
    return;
  }
  const visibleCourses = appState.courses.filter((course) => courseHasVisibleClasses(course));
  if (!visibleCourses.length) {
    renderState(
      "当前筛选条件下没有可显示的课程",
      "本页课程的教学班都被“不显示时间冲突”或“不显示人数已满”隐藏了，关闭对应筛选即可恢复显示。",
    );
    return;
  }
  renderCourseList(visibleCourses);
}

function updatePagination() {
  const blocked = courseCatalogBlocked() && !appState.cacheMode;
  if (isFilterActive()) {
    const results = Array.isArray(appState.searchResults) ? appState.searchResults : [];
    const totalPages = Math.max(1, Math.ceil(results.length / FILTER_PAGE_SIZE));
    appElements.pageLabel.textContent = `第 ${appState.searchPage} / ${totalPages} 页`;
    const busy = appState.loadingCourses || appState.loadingCatalog;
    appElements.previousPage.disabled = blocked || busy || appState.searchPage <= 1;
    appElements.nextPage.disabled = blocked || busy || appState.searchPage >= totalPages;
    return;
  }
  const totalPages = Math.max(1, Math.ceil(appState.totalCount / FILTER_PAGE_SIZE));
  appElements.pageLabel.textContent = `第 ${appState.page} / ${totalPages} 页`;
  appElements.previousPage.disabled = blocked || appState.page <= 1 || appState.loadingCourses;
  appElements.nextPage.disabled = blocked || appState.page >= totalPages || appState.loadingCourses;
}

function abortCatalogFetch() {
  const loadingType = appState.catalogLoadingType;
  appState.catalogRequestId += 1;
  appState.catalogRequestController?.abort();
  appState.catalogRequestController = null;
  appState.loadingCatalog = false;
  appState.catalogLoadingType = "";
  if (loadingType && !appState.catalogCaches[loadingType]?.complete) {
    delete appState.catalogCaches[loadingType];
  }
  appElements.refreshCourses.disabled = appState.loadingCourses || appState.refreshingPhase;
}

function invalidateCatalogCache(type = appState.type) {
  const normalizedType = String(type || "");
  if (!normalizedType) return;
  if (appState.catalogLoadingType === normalizedType) abortCatalogFetch();
  delete appState.catalogCaches[normalizedType];
  if (normalizedType === appState.type) {
    appState.searchResults = [];
    appState.searchPage = 1;
  }
}

function invalidateCatalogCaches() {
  abortCatalogFetch();
  appState.catalogCaches = {};
  appState.searchResults = [];
  appState.searchPage = 1;
}

function waitForCatalogPageDelay(signal) {
  if (CATALOG_PAGE_DELAY_MS <= 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Catalog request aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Catalog request aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, CATALOG_PAGE_DELAY_MS);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function fetchFullCatalog({ force = false, preserveExisting = false, forceLive = false } = {}) {
  const type = appState.type;
  const offlineCache = !appState.session?.logged_in && isOfflineCacheType();
  const scopeKey = catalogScopeKey();
  const existing = appState.catalogCaches[type];
  if (existing?.complete && existing.scopeKey === scopeKey && !force) return true;
  if (appState.loadingCatalog && appState.catalogLoadingType === type) return true;

  abortCatalogFetch();
  const controller = new AbortController();
  const requestId = appState.catalogRequestId + 1;
  appState.catalogRequestId = requestId;
  appState.catalogRequestController = controller;
  appState.loadingCatalog = true;
  appState.catalogLoadingType = type;
  appElements.refreshCourses.disabled = true;
  const cache = {
    courses: [],
    totalCount: 0,
    complete: false,
    scopeKey,
    cached: true,
    cachedAt: 0,
  };
  if (!preserveExisting) appState.catalogCaches[type] = cache;

  const updateProgress = () => {
    if (!preserveExisting) {
      appElements.courseSummary.textContent = cache.totalCount
        ? `正在加载全部课程 ${cache.courses.length} / ${cache.totalCount} 门`
        : "正在加载全部课程";
    }
  };
  updateProgress();
  if (!preserveExisting) renderCourses();
  updatePagination();

  try {
    let completed = false;
    let expectedTotalCount = null;
    for (let page = 1; page <= MAX_CATALOG_PAGES; page += 1) {
      const data = await api(
        courseRequestUrl(type, page, FILTER_PAGE_SIZE, {
          cacheMode: (appState.cacheMode || offlineCache) && !forceLive,
        }),
        { signal: controller.signal, timeoutMs: SESSION_RECOVERY_TIMEOUT_MS },
      );
      if (
        requestId !== appState.catalogRequestId
        || scopeKey !== catalogScopeKey()
      ) return false;
      const items = Array.isArray(data.courses) ? data.courses : [];
      if (data.full_catalog) {
        const reportedTotal = Number(data.total_count);
        if (!Number.isInteger(reportedTotal) || reportedTotal < items.length) {
          throw new ApiError("本地课程缓存数据无效，请重新加载", {
            code: "SCHOOL_RESPONSE_INVALID",
            retryable: true,
          });
        }
        cache.courses = items;
        cache.totalCount = reportedTotal;
        cache.cached = Boolean(data.cached);
        cache.cachedAt = Number(data.cached_at) || 0;
        completed = true;
        break;
      }
      const reportedTotal = Number(data.total_count);
      if (!Number.isInteger(reportedTotal) || reportedTotal < 0) {
        throw new ApiError("学校返回的课程总数无效，请稍后重新加载", {
          code: "SCHOOL_RESPONSE_INVALID",
          retryable: true,
        });
      }
      if (expectedTotalCount === null) {
        expectedTotalCount = reportedTotal;
      } else if (reportedTotal !== expectedTotalCount) {
        throw new ApiError("课程总数在分页读取期间发生变化，请重新加载", {
          code: "SCHOOL_RESPONSE_INVALID",
          retryable: true,
        });
      }
      cache.totalCount = expectedTotalCount;
      cache.courses.push(...items);
      cache.cached = cache.cached && Boolean(data.cached);
      cache.cachedAt = Math.max(cache.cachedAt, Number(data.cached_at) || 0);
      updateProgress();
      const totalPages = Math.max(1, Math.ceil(cache.totalCount / FILTER_PAGE_SIZE));
      if (totalPages > MAX_CATALOG_PAGES) {
        throw new ApiError(
          `该课程目录共有 ${totalPages} 页，超过安全加载上限 ${MAX_CATALOG_PAGES} 页`,
          { code: "CATALOG_PAGE_LIMIT", retryable: false },
        );
      }
      if (page >= totalPages) {
        if (cache.courses.length !== cache.totalCount) {
          throw new ApiError("学校课程目录分页数据不完整，请稍后重新加载", {
            code: "SCHOOL_RESPONSE_INVALID",
            retryable: true,
          });
        }
        completed = true;
        break;
      }
      if (!items.length) {
        throw new ApiError("学校课程目录中途返回了空页，请稍后重新加载", {
          code: "SCHOOL_RESPONSE_INVALID",
          retryable: true,
        });
      }
      await waitForCatalogPageDelay(controller.signal);
    }
    if (requestId !== appState.catalogRequestId) return false;
    if (!completed) {
      throw new ApiError("课程目录未能在安全页数范围内加载完成", {
        code: "CATALOG_PAGE_LIMIT",
        retryable: false,
      });
    }
    cache.complete = true;
    if (preserveExisting) appState.catalogCaches[type] = cache;
    return true;
  } catch (error) {
    if (isAbortError(error) || requestId !== appState.catalogRequestId) return false;
    if (!preserveExisting) delete appState.catalogCaches[type];
    if (error instanceof SessionExpiredError) return false;
    const availabilityError = ["COURSE_WINDOW_CLOSED", "BATCH_UNAVAILABLE"].includes(error.code);
    if (availabilityError) {
      if (preserveExisting && existing?.complete) return false;
      appState.catalogBlockedCode = error.code;
      setPhasePresentation();
      renderCourseAvailabilityState(error.message);
    } else {
      showToast(`${courseErrorTitle(error)}：${error.message}`, true);
      renderCourses();
      updatePagination();
    }
    return false;
  } finally {
    if (requestId === appState.catalogRequestId) {
      appState.loadingCatalog = false;
      appState.catalogLoadingType = "";
      appState.catalogRequestController = null;
      appElements.courseSearch.disabled = courseCatalogBlocked()
        && !appState.cacheMode
        && !offlineCache;
      appElements.refreshCourses.disabled = appState.loadingCourses || appState.refreshingPhase;
    }
  }
}

async function runSearchFetch({ force = false, preserveExisting = false, forceLive = false } = {}) {
  const offlineCache = !appState.session?.logged_in && isOfflineCacheType();
  if (!appState.session?.logged_in && !offlineCache) return;
  if (courseCatalogBlocked() && !appState.cacheMode && !forceLive) return;
  const ok = await fetchFullCatalog({ force, preserveExisting, forceLive });
  if (!ok || !isFilterActive()) return;
  applyCourseFilter();
  renderCourses();
  updatePagination();
  return ok;
}

async function handleSearchInput() {
  const keyword = appElements.courseSearch.value.trim();
  appState.searchPage = 1;
  if (!keyword) {
    if (isFilterActive()) {
      appState.searchKeyword = "";
      abortCatalogFetch();
      if (appState.courseDataKey) {
        appElements.courseSummary.textContent = courseSummary(
          appState.totalCount,
          appState.courses.length,
          appState.courseCacheMeta || {},
        );
        renderCourses();
      }
      updatePagination();
    }
    return;
  }
  appState.searchKeyword = keyword;
  await runSearchFetch();
}

function courseErrorTitle(error) {
  if (["SCHOOL_TIMEOUT", "REQUEST_TIMEOUT"].includes(error.code)) return "学校响应超时";
  if (["SCHOOL_NETWORK_ERROR", "LOCAL_SERVICE_UNAVAILABLE"].includes(error.code)) {
    return "暂时无法连接服务";
  }
  if (error.code === "SCHOOL_RESPONSE_INVALID") return "学校数据暂时异常";
  if (error.code === "SCHOOL_COURSE_REJECTED") return "学校暂时拒绝了请求";
  if (error.code === "UNSUPPORTED_COURSE_TYPE") return "该目录暂不支持";
  return "课程目录读取失败";
}

async function loadCourses(options = {}) {
  const offlineCache = Boolean(options.cacheOnly || (!appState.session?.logged_in && isOfflineCacheType()));
  if (!appState.session?.logged_in && !offlineCache) {
    renderState("尚未登录", "返回登录页完成学号、密码、卡密和验证码校验。");
    return;
  }
  const forceLive = Boolean(options.forceLive);
  if (courseCatalogBlocked() && !appState.cacheMode && !offlineCache && !forceLive) {
    renderCourseAvailabilityState();
    return;
  }
  // 筛选模式下课程视图由全目录搜索负责，服务端分页加载直接跳过。
  if (isFilterActive()) return;

  appState.courseRequestController?.abort();
  const controller = new AbortController();
  const requestId = appState.courseRequestId + 1;
  const requestedType = appState.type;
  const requestedPage = appState.page;
  const requestedKey = `${requestedType}:${requestedPage}`;
  const preserveExisting = Boolean(options.preserveExisting);
  const hasCurrentResult = appState.courseDataKey === requestedKey;
  appState.courseRequestController = controller;
  appState.courseRequestId = requestId;
  appState.loadingCourses = true;
  appElements.refreshCourses.disabled = true;
  appElements.courseSearch.disabled = false;
  appElements.courseTypeCode.textContent = appState.type;
  appElements.courseTitle.textContent = categoryNames[appState.type] || appState.type;
  appElements.courseSummary.textContent = hasCurrentResult && preserveExisting
    ? "正在刷新，当前仍显示上次成功结果"
    : "正在读取学校课程数据";
  if (!hasCurrentResult || !preserveExisting) {
    appState.courses = [];
    appState.totalCount = 0;
    appState.courseDataKey = "";
    renderLoading();
  }
  updatePagination();

  try {
    const data = await api(
      courseRequestUrl(requestedType, requestedPage, 10, {
        cacheMode: (appState.cacheMode || offlineCache) && !forceLive,
      }),
      { signal: controller.signal, timeoutMs: SESSION_RECOVERY_TIMEOUT_MS },
    );
    if (requestId !== appState.courseRequestId) return;
    const allCourses = Array.isArray(data.courses) ? data.courses : [];
    const courses = data.full_catalog
      ? allCourses.slice((requestedPage - 1) * 10, requestedPage * 10)
      : allCourses;
    if (preserveExisting && !courses.length && hasCurrentResult) {
      appElements.courseSummary.textContent = "实时刷新返回空列表，仍显示上次成功结果";
      return;
    }
    appState.courses = courses;
    appState.totalCount = Number(data.total_count || allCourses.length || 0);
    appState.courseDataKey = requestedKey;
    appState.courseCacheMeta = data;
    appState.catalogBlockedCode = "";
    appElements.courseSummary.textContent = courseSummary(
      appState.totalCount,
      appState.courses.length,
      data,
    );
    renderCourses();
  } catch (error) {
    if (isAbortError(error) || requestId !== appState.courseRequestId) return;
    if (error instanceof SessionExpiredError) {
      if (offlineCache) {
        appElements.courseSummary.textContent = "没有找到本地课程缓存";
        renderState(
          "暂无离线课程数据",
          "请先登录并成功加载本班推荐或方案内课程，之后即使学校系统不可用也可以本地查阅。",
        );
      }
      return;
    }

    const availabilityError = ["COURSE_WINDOW_CLOSED", "BATCH_UNAVAILABLE"].includes(error.code);
    if (availabilityError) {
      if (preserveExisting && hasCurrentResult) {
        appElements.courseSummary.textContent = "实时刷新暂不可用，仍显示上次成功结果";
        return;
      }
      appState.catalogBlockedCode = error.code;
      setPhasePresentation();
      renderCourseAvailabilityState(error.message);
      return;
    }

    if (hasCurrentResult && preserveExisting) {
      appElements.courseSummary.textContent = "刷新失败，仍显示上次成功结果";
      showToast(`${courseErrorTitle(error)}：${error.message}`, true);
      return;
    }

    appState.courses = [];
    appState.totalCount = 0;
    appState.courseDataKey = "";
    appElements.courseSummary.textContent = "课程目录暂时不可用";
    renderState(courseErrorTitle(error), error.message, {
      tone: "error",
      note: "登录状态和已加入的本地选课清单不会因本次读取失败而丢失。",
      actions: error.retryable
        ? [
          { label: "重新加载课程", handler: () => loadCourses({ preserveExisting: true }), primary: true },
          { label: "重新检查开放状态", handler: refreshPhaseAndCourses },
        ]
        : [],
    });
  } finally {
    if (requestId === appState.courseRequestId) {
      appState.loadingCourses = false;
      appState.courseRequestController = null;
      appElements.refreshCourses.disabled = appState.refreshingPhase || appState.loadingCatalog;
      appElements.courseSearch.disabled = courseCatalogBlocked()
        && !appState.cacheMode
        && !offlineCache;
      updatePagination();
    }
  }
}

async function refreshPhaseAndCourses() {
  if (appState.refreshingPhase || !appState.session?.logged_in) return;
  appState.refreshingPhase = true;
  appState.courseRequestController?.abort();
  const previousLabel = appElements.refreshPhase.textContent;
  appElements.refreshPhase.textContent = "检查中...";
  appElements.refreshPhase.disabled = true;
  appElements.refreshCourses.disabled = true;

  try {
    const session = await api("/api/session/refresh", {
      method: "POST",
      timeoutMs: SESSION_RECOVERY_TIMEOUT_MS,
    });
    appState.catalogBlockedCode = "";
    applySessionData(session);
    showToast(session.message || "开放状态已更新", false, true);
    if (courseCatalogBlocked()) renderCourseAvailabilityState();
    else {
      invalidateCatalogCache(appState.type);
      if (isFilterActive()) await runSearchFetch({ force: true });
      else await loadCourses({ preserveExisting: true });
    }
  } catch (error) {
    if (error instanceof SessionExpiredError) return;
    if (error.payload?.session) applySessionData(error.payload.session);
    if (error.code === "BATCH_UNAVAILABLE") {
      appState.catalogBlockedCode = error.code;
      renderCourseAvailabilityState(error.message);
      return;
    }

    if (appState.courseDataKey) {
      showToast(`开放状态检查失败：${error.message}`, true);
    } else {
      appElements.courseSummary.textContent = "开放状态检查失败";
      renderState("暂时无法检查开放状态", error.message, {
        tone: "error",
        note: "当前登录状态和本地选课清单不受影响。",
        actions: error.retryable
          ? [{ label: "重新检查", handler: refreshPhaseAndCourses, primary: true }]
          : [],
      });
    }
  } finally {
    appState.refreshingPhase = false;
    appElements.refreshPhase.textContent = previousLabel;
    appElements.refreshPhase.disabled = false;
    appElements.refreshCourses.disabled = appState.loadingCourses;
    updatePagination();
  }
}

async function refreshCurrentView() {
  if (courseCatalogBlocked() && !appState.cacheMode) await refreshPhaseAndCourses();
  else {
    if (!appState.cacheMode) invalidateCatalogCache(appState.type);
    if (isFilterActive()) {
      await runSearchFetch({ force: true, preserveExisting: true, forceLive: true });
    } else {
      await loadCourses({ preserveExisting: true, forceLive: true });
    }
  }
}

async function refreshCoursesFromNetwork() {
  if (
    !appState.cacheMode
    || !appState.session?.logged_in
    || appState.loadingCourses
    || appState.loadingCatalog
    || appState.refreshingPhase
  ) return;
  if (isFilterActive()) {
    const ok = await runSearchFetch({ force: true, preserveExisting: true, forceLive: true });
    if (ok !== false) {
      applyCourseFilter();
      renderCourses();
      updatePagination();
    }
    return;
  }
  await loadCourses({ preserveExisting: true, forceLive: true });
}

function syncEnrollControls() {
  const running = Boolean(appState.session?.task_running);
  const paused = Boolean(appState.session?.task_paused);
  const pauseAcknowledged = Boolean(appState.session?.task_pause_acknowledged);
  const stopping = Boolean(appState.session?.task_stopping);
  const hasPending = appState.cart.some((item) => (
    (item.status || "PENDING") === "PENDING" && preferenceFor(item).autoEnabled !== false
  ));
  appElements.openEnrollConfirm.disabled = !appState.grabPhase || running || !hasPending;
  if (appElements.stopEnroll) appElements.stopEnroll.disabled = !running || stopping;
  if (running && stopping) {
    appElements.cartHint.textContent = appState.session?.task_stopping_reason
      || "待处理课程已清空，后台任务正在结束。";
  } else if (running && paused && !pauseAcknowledged) {
    appElements.cartHint.textContent = "正在完成当前学校请求；安全暂停后即可增删课程或调整优先级。";
  } else if (running && paused) {
    appElements.cartHint.textContent = appState.session?.task_pause_reason
      || "抢课任务已安全暂停，可以增删课程或调整优先级；继续后保留已有尝试次数。";
  } else if (running) {
    appElements.cartHint.textContent = "后台抢课任务正在运行，清单已锁定，抢到的课程会自动进入我的课程。";
  } else if (appState.preselection) {
    appElements.cartHint.textContent = "预选阶段由系统抽签，无需抢课；可先整理好清单，等复选或补选再启动。";
  } else if (appState.closedPhase) {
    appElements.cartHint.textContent = "当前不在开放选课时间，清单可以保留，开放后刷新批次再启动。";
  } else if (!appState.grabPhase) {
    appElements.cartHint.textContent = "当前批次不允许自动抢课，仅可浏览和整理清单。";
  } else if (!hasPending) {
    const hasDisabledPending = appState.cart.some((item) => (
      (item.status || "PENDING") === "PENDING" && preferenceFor(item).autoEnabled === false
    ));
    appElements.cartHint.textContent = hasDisabledPending
      ? "待抢课程的“自动抢课”开关均已关闭，请先在清单中开启后再启动。"
      : "清单中没有待启动课程，先从课程目录加入。";
  } else {
    appElements.cartHint.textContent = "满员课程可以加入清单排队候补；冲突或已选课程不能加入。";
  }
  renderEnrollControls();
}

function cartItemsSorted() {
  return [...appState.cart].sort((left, right) => {
    const a = preferenceFor(left);
    const b = preferenceFor(right);
    return a.priorityGroup.localeCompare(b.priorityGroup, "zh-CN")
      || a.priorityRank - b.priorityRank
      || String(left.name || left.id).localeCompare(String(right.name || right.id), "zh-CN");
  });
}

function cartGroups() {
  const groups = new Map();
  for (const item of cartItemsSorted()) {
    const group = preferenceFor(item).priorityGroup;
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(item);
  }
  return groups;
}

async function patchCartPreference(item, values) {
  const previous = preferenceFor(item);
  updateLocalCartPreference(item, values);
  renderCart();
  try {
    await planApi(`/api/courses/${encodeURIComponent(item.id)}`, {
      method: "PATCH",
      body: JSON.stringify(values),
    });
    await loadCart();
  } catch (error) {
    updateLocalCartPreference(item, previous);
    renderCart();
    showToast(planErrorMessage(error), true);
  }
}

function makeCartPreferenceControl(item) {
  const preference = preferenceFor(item);
  const controls = element("div", "cart-preference-controls");
  const label = element("label", "switch-row compact-switch");
  const toggle = element("input");
  toggle.type = "checkbox";
  toggle.checked = Boolean(preference.autoEnabled);
  toggle.disabled = !canMutateQueue();
  toggle.setAttribute("aria-label", "自动抢课开关");
  label.append(toggle, element("span", "", "自动抢课"));
  toggle.addEventListener("change", () => patchCartPreference(item, { auto_enabled: toggle.checked }));

  const group = element("input", "cart-group-input");
  group.type = "text";
  group.maxLength = 256;
  group.value = preference.priorityGroup;
  group.disabled = !canMutateQueue();
  group.title = "同一优选组内按优先级尝试，优先级较高者先尝试";
  group.setAttribute("aria-label", "优选分组");
  group.addEventListener("change", () => patchCartPreference(item, { priority_group: group.value.trim() || "未分组课程" }));

  const rank = element("span", "cart-rank", `优先级 ${preference.priorityRank + 1}`);
  const up = element("button", "button button-quiet", "↑");
  const down = element("button", "button button-quiet", "↓");
  up.type = "button";
  down.type = "button";
  up.disabled = !canMutateQueue();
  down.disabled = !canMutateQueue();
  up.title = "提高同组优先级";
  down.title = "降低同组优先级";
  up.addEventListener("click", () => moveCartItem(item, -1));
  down.addEventListener("click", () => moveCartItem(item, 1));
  controls.append(label, group, rank, up, down);
  return controls;
}

async function moveCartItem(item, direction) {
  const ordered = cartItemsSorted();
  const currentIndex = ordered.findIndex((candidate) => String(candidate.id) === String(item.id));
  const targetIndex = currentIndex + direction;
  if (currentIndex < 0 || targetIndex < 0 || targetIndex >= ordered.length) return;
  const other = ordered[targetIndex];
  const currentPreference = preferenceFor(item);
  const otherPreference = preferenceFor(other);
  if (currentPreference.priorityGroup !== otherPreference.priorityGroup) {
    showToast("上下移动只作用于同一优选分组", true);
    return;
  }
  updateLocalCartPreference(item, { priorityRank: otherPreference.priorityRank });
  updateLocalCartPreference(other, { priorityRank: currentPreference.priorityRank });
  renderCart();
  try {
    await planApi("/api/courses/priority/order", {
      method: "PATCH",
      body: JSON.stringify({
        updates: [
          { id: item.id, priority_rank: otherPreference.priorityRank },
          { id: other.id, priority_rank: currentPreference.priorityRank },
        ],
      }),
    });
  } catch (error) {
    updateLocalCartPreference(item, { priorityRank: currentPreference.priorityRank });
    updateLocalCartPreference(other, { priorityRank: otherPreference.priorityRank });
    renderCart();
    showToast(planErrorMessage(error), true);
  }
}

function renderCart() {
  ensureCartPreferenceRanks();
  appElements.cartCount.textContent = String(appState.cart.length);
  if (!appState.cart.length) {
    const empty = element("div", "empty-state");
    empty.append(element("strong", "", "选课清单为空"));
    empty.append(element("p", "", "从课程目录展开教学班后加入清单。"));
    appElements.cartList.replaceChildren(empty);
    syncEnrollControls();
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const [groupName, items] of cartGroups()) {
    const group = element("section", "cart-priority-group");
    const heading = element("div", "cart-priority-group-title");
    heading.append(element("strong", "", groupName));
    heading.append(element("span", "", `${items.length} 门，按优先级尝试`));
    group.append(heading);
    for (const item of items) {
    const row = element("div", "cart-item");
    const copy = element("div");
    copy.append(element("strong", "", item.name || item.id));
    copy.append(
      element(
        "span",
        "",
        [item.type, item.campus_name, statusNames[item.status] || item.status || "待启动"]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    copy.append(makeCartPreferenceControl(item));
    const actions = element("div", "cart-item-actions");
    const statusClass = item.status === "SUCCESS" ? "status-success" : item.status === "FAILED" ? "status-danger" : item.status === "ENROLLING" ? "status-warning" : "status-neutral";
    actions.append(element("span", `status-pill ${statusClass}`, statusNames[item.status] || "待启动"));
    if (item.status === "FAILED" && appState.session?.logged_in && canMutateQueue()) {
      const retry = element("button", "button button-secondary", "重新排队");
      retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try {
          const result = await api(`/api/courses/retry?id=${encodeURIComponent(item.id)}`, {
            method: "POST",
          });
          if (result.is_error) throw new Error(result.message);
          showToast(result.message || "课程已重新排队", false, true);
          await loadCart();
        } catch (error) {
          if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
          retry.disabled = false;
        }
      });
      actions.append(retry);
    }
    const remove = element("button", "button button-quiet", "移除");
    remove.type = "button";
    const taskRunning = Boolean(appState.session?.task_running);
    const taskStopping = Boolean(appState.session?.task_stopping);
    const canEditPausedTask = taskRunning
      && Boolean(appState.session?.task_paused)
      && Boolean(appState.session?.task_pause_acknowledged)
      && !taskStopping;
    const terminalCourse = ["SUCCESS", "FAILED"].includes(item.status);
    remove.disabled = !appState.session?.logged_in
      || (taskRunning && (taskStopping || (!terminalCourse && !canEditPausedTask)));
    if (remove.disabled) {
      remove.title = !appState.session?.logged_in
        ? "未登录时仅可查阅本地选课清单"
        : taskStopping
        ? "抢课任务正在结束"
        : appState.session?.task_paused
          ? "正在完成当前请求，请等待安全暂停"
          : "请先暂停抢课任务";
    }
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        const result = await api(`/api/courses/delete?id=${encodeURIComponent(item.id)}`, { method: "POST" });
        if (result.is_error) throw new Error(result.message);
        showToast(result.message || "已移除");
        if (result.progress) {
          const { controlsChanged } = applyProgressTaskState(result.progress);
          renderProgress(result.progress);
          updateTaskIndicator();
          if (controlsChanged) renderCart();
        }
        await loadCart();
      } catch (error) {
        if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
        remove.disabled = false;
      }
    });
    actions.append(remove);
    row.append(copy, actions);
    group.append(row);
    }
    fragment.append(group);
  }
  appElements.cartList.replaceChildren(fragment);
  syncEnrollControls();
}

async function loadCart() {
  try {
    const data = await api("/api/courses/dblist?status=");
    appState.cart = Array.isArray(data) ? data : [];
    // The SQLite response is authoritative. Reconcile old browser cache
    // entries so a server-side unchecked auto_enabled value is not restored
    // as checked during the next render.
    for (const item of appState.cart) {
      const id = String(item.id);
      const current = appState.cartPreferences[id] || {};
      if (item.auto_enabled !== undefined) current.autoEnabled = Boolean(item.auto_enabled);
      if (item.priority_group !== undefined) current.priorityGroup = String(item.priority_group || "");
      if (item.priority_rank !== undefined) current.priorityRank = numericValue(item.priority_rank, 0);
      appState.cartPreferences[id] = current;
    }
    renderCart();
    renderMyCoursesSchedule();
  } catch (error) {
    if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
  }
}

/* ---------------- My courses (school enrolled) ---------------- */

function renderMyCourses() {
  const list = appState.myCourses;
  const pendingItems = getPendingMyCourseItems();
  const entries = [
    ...list.map((course) => ({ course, pending: false })),
    ...pendingItems.map((course) => ({ course, pending: true })),
  ];
  if (!entries.length) {
    const empty = element("div", "empty-state");
    empty.append(element("strong", "", "还没有已选课程"));
    empty.append(element("p", "", "抢到课程后会显示在这里；选课清单中的待抢课程会以虚化状态显示。"));
    appElements.myCoursesList.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  entries.forEach(({ course, pending }, index) => {
    const row = element("div", `my-course-item${pending ? " is-pending" : ""}`);
    row.append(element("span", "my-course-index", String(index + 1)));
    const body = element("div", "my-course-body");
    const title = element("strong", "", course.course_name || course.name || "未命名课程");
    if (pending) title.append(element("span", "my-course-pending-badge", "待选"));
    body.append(title);
    const meta = element("div", "my-course-meta");
    if (course.teacher_name) meta.append(element("span", "", `教师 ${course.teacher_name}`));
    if (course.teaching_place) meta.append(element("span", "", course.teaching_place));
    if (course.credit) meta.append(element("span", "", `${course.credit} 学分`));
    if (course.course_type_name) meta.append(element("span", "", course.course_type_name));
    if (course.campus_name) meta.append(element("span", "", course.campus_name));
    body.append(meta);
    row.append(body);
    fragment.append(row);
  });
  appElements.myCoursesList.replaceChildren(fragment);
}

function getPendingMyCourseItems({ visibleOnly = true } = {}) {
  const enrolledIds = new Set(
    appState.myCourses.map((course) => String(course.teaching_class_id || "")),
  );
  if (visibleOnly && !appState.showCartOnSchedule) return [];
  return appState.cart.filter(
    (item) => (item.status || "PENDING") !== "SUCCESS"
      && !enrolledIds.has(String(item.id)),
  );
}

function numericCredit(value) {
  const number = Number.parseFloat(String(value ?? "").replace(/[^0-9.+-]/g, ""));
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function formatCredit(value) {
  return Number.isInteger(value)
    ? String(value)
    : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function myCoursesCreditSummary() {
  const selected = appState.myCourses.reduce(
    (total, course) => total + numericCredit(course.credit),
    0,
  );
  const pending = getPendingMyCourseItems({ visibleOnly: false }).reduce(
    (total, course) => total + numericCredit(course.credit),
    0,
  );
  return `已选课程 ${formatCredit(selected)} 学分 + 待抢课程 ${formatCredit(pending)} 学分 = 合计 ${formatCredit(selected + pending)} 学分`;
}

function renderMyCoursesCreditSummary() {
  const selected = appState.myCourses.reduce(
    (total, course) => total + numericCredit(course.credit),
    0,
  );
  const pending = getPendingMyCourseItems({ visibleOnly: false }).reduce(
    (total, course) => total + numericCredit(course.credit),
    0,
  );
  if (appElements.selectedCreditTotal) appElements.selectedCreditTotal.textContent = formatCredit(selected);
  if (appElements.pendingCreditTotal) appElements.pendingCreditTotal.textContent = formatCredit(pending);
  if (appElements.combinedCreditTotal) appElements.combinedCreditTotal.textContent = formatCredit(selected + pending);
}

/* ---------------- My courses schedule (weekly grid) ---------------- */

const SCHEDULE_COLORS = 12;
const BREAK_PERIODS = [3, 6, 9, 11];

/**
 * Build a course-color mapping so visually distinct courses stand out.
 * Keyed by course_name|teacher_name for enrolled courses.
 */
function buildScheduleColorMap(courses) {
  const colorMap = new Map();
  let nextColor = 0;
  for (const course of courses) {
    const key = (course.course_name || "未命名课程") + "|" + (course.teacher_name || "");
    if (colorMap.has(key)) continue;
    colorMap.set(key, nextColor % SCHEDULE_COLORS);
    nextColor += 1;
  }
  return colorMap;
}

/**
 * Parse a course-like object's teaching_place into schedule slots
 * and split them into placed (on the standard 14-period grid) and
 * unplaced (go to the right-side non-standard list).
 */
function collectScheduleEntries(courseLike, color, pending, placedSlots, unplaced, state = {}) {
  const slots = parseScheduleSlots(courseLike.teaching_place || "");
  let hasPlaced = false;
  for (const slot of slots) {
    const ok =
      slot.dayOfWeek >= 0 && slot.dayOfWeek <= 6 &&
      slot.startPeriod >= 1 && slot.endPeriod <= MAX_PERIOD &&
      slot.startPeriod <= slot.endPeriod;
    if (ok) {
      placedSlots.push({ course: courseLike, slot, color, pending, ...state });
      hasPlaced = true;
    }
  }
  if (!hasPlaced) {
    unplaced.push({
      course: courseLike,
      color,
      pending,
      ...state,
      reason: slots.length ? "时间超出标准节次" : "无时间信息",
    });
  }
}

function formatCourseTooltip(course, slot, pending) {
  const parts = [];
  parts.push(course.course_name || "未命名课程");
  if (pending) parts.push("（待选）");
  if (course.teacher_name) parts.push("教师：" + course.teacher_name);
  if (slot.weeks) parts.push("周数：" + slot.weeks);
  if (slot.dayLabel) parts.push("星期：" + slot.dayLabel);
  parts.push("节次：第" + slot.startPeriod + "-" + slot.endPeriod + "节");
  if (slot.place) parts.push("地点：" + slot.place);
  if (course.teaching_place && course.teaching_place !== slot.raw) {
    parts.push("完整时间地点：" + course.teaching_place);
  }
  return parts.join("\n");
}

function buildCourseBlock(placed) {
  const stateClasses = [
    placed.pending ? "is-pending" : "",
    placed.isFocused ? "is-focused" : "",
    placed.isConflictHighlight ? "is-conflict-highlight" : "",
    placed.isContextMuted ? "is-context-muted" : "",
  ].filter(Boolean).join(" ");
  const block = element("div", `schedule-course${stateClasses ? ` ${stateClasses}` : ""}`);
  block.setAttribute("data-color", String(placed.color));
  block.title = formatCourseTooltip(placed.course, placed.slot, placed.pending);
  const nameEl = element("strong", "", placed.course.course_name || "未命名课程");
  block.append(nameEl);
  if (placed.pending) {
    block.append(element("span", "schedule-pending-badge", "待选"));
  }
  if (placed.slot.weeks) {
    block.append(element("span", "schedule-weeks", placed.slot.weeks));
  }
  if (placed.slot.place) {
    block.append(element("span", "schedule-place", placed.slot.place));
  }
  if (placed.course.teacher_name) {
    block.append(element("span", "schedule-teacher", placed.course.teacher_name));
  }
  return block;
}

function scheduleEntriesOverlap(left, right) {
  return Number(left.dayOfWeek) === Number(right.dayOfWeek)
    && Number(left.startPeriod) <= Number(right.endPeriod)
    && Number(right.startPeriod) <= Number(left.endPeriod);
}

function isConflictFocusCourse(courseLike, conflict) {
  const focusId = String(conflict?.focusId || "");
  return Boolean(focusId && String(courseLike.teaching_class_id || courseLike.id || "") === focusId);
}

function updateMyCoursesHint() {
  const conflict = appState.scheduleConflict;
  renderMyCoursesCreditSummary();
  const creditSummary = myCoursesCreditSummary();
  if (conflict) {
    appElements.myCoursesHint.textContent = conflict.focusSlots.length
      ? `${creditSummary}；冲突查看：${conflict.title}；当前教学班使用蓝色高亮，冲突课程使用红色高亮。`
      : `${creditSummary}；冲突查看：${conflict.title}；当前教学班的时间地点格式无法解析，暂时无法定位冲突课程。`;
    return;
  }
  if (appState.myCoursesLoaded) {
    appElements.myCoursesHint.textContent = `${creditSummary}；学校系统当前返回 ${appState.myCourses.length} 门已选课程；虚化内容为选课清单中的待抢课程；悬停在卡片上以显示完整课程信息。`;
  }
}

function renderMyCoursesSchedule() {
  const wrap = appElements.myCoursesScheduleWrap;
  const courses = appState.myCourses;
  const conflict = appState.scheduleConflict;

  /* Determine pending (cart) items to show */
  const pendingItems = getPendingMyCourseItems();

  if (!courses.length && !pendingItems.length && !conflict) {
    const empty = element("div", "empty-state");
    empty.append(element("strong", "", "还没有已选课程"));
    empty.append(element("p", "", "抢到课程后会显示在这里，也可能是学校系统暂未返回数据。"));
    wrap.replaceChildren(empty);
    return;
  }

  /* Build color map (enrolled + pending share the same palette space) */
  const colorMap = buildScheduleColorMap(courses);
  let pendingColorBase = colorMap.size;

  const placedSlots = [];
  const unplaced = [];

  for (const course of courses) {
    const key = (course.course_name || "未命名课程") + "|" + (course.teacher_name || "");
    collectScheduleEntries(
      course,
      colorMap.get(key) || 0,
      false,
      placedSlots,
      unplaced,
      { isFocused: isConflictFocusCourse(course, conflict) },
    );
  }

  for (const item of pendingItems) {
    const displayName = item.course_name || item.name || "未命名课程";
    const teacherName = item.teacher_name || "";
    const key = displayName + "|" + teacherName;
    if (!colorMap.has(key)) {
      colorMap.set(key, pendingColorBase % SCHEDULE_COLORS);
      pendingColorBase += 1;
    }
    collectScheduleEntries(
      {
        id: item.id,
        teaching_class_id: item.id,
        course_name: displayName,
        teacher_name: teacherName,
        teaching_place: item.teaching_place,
      },
      colorMap.get(key),
      true,
      placedSlots,
      unplaced,
      { isFocused: isConflictFocusCourse(item, conflict) },
    );
  }

  if (conflict && !placedSlots.some((placed) => placed.isFocused)
      && !unplaced.some((item) => item.isFocused)) {
    if (conflict.focusSlots.length) {
      for (const slot of conflict.focusSlots) {
        placedSlots.push({
          course: conflict.course,
          slot,
          color: 0,
          pending: false,
          isFocused: true,
        });
      }
    } else {
      unplaced.push({
        course: conflict.course,
        color: 0,
        pending: false,
        isFocused: true,
        reason: "当前教学班的时间地点格式无法解析",
      });
    }
  }

  if (conflict) {
    for (const placed of placedSlots) {
      placed.isConflictHighlight = !placed.isFocused
        && conflict.focusSlots.some((focusSlot) => scheduleEntriesOverlap(placed.slot, focusSlot));
      placed.isContextMuted = !placed.isFocused && !placed.isConflictHighlight;
    }
    for (const item of unplaced) {
      item.isContextMuted = !item.isFocused;
    }
  }
  updateMyCoursesHint();

  /* ---- Build layout ---- */
  const container = element("div", "schedule-layout");

  /* Left: weekly grid (14 per-period rows) */
  const gridWrap = element("div", "schedule-grid-wrap");

  const today = new Date().getDay();
  const todayIndex = today === 0 ? 6 : today - 1;

  const grid = element("div", "schedule-grid");
  grid.style.gridTemplateColumns = "58px repeat(7, minmax(78px, 1fr))";
  grid.style.gridTemplateRows = "auto repeat(" + MAX_PERIOD + ", minmax(28px, auto))";

  /* Corner */
  const corner = element("div", "schedule-corner", "节");
  corner.style.gridRow = "1";
  corner.style.gridColumn = "1";
  grid.append(corner);

  /* Day headers */
  for (let d = 0; d < 7; d++) {
    const header = element("div", "schedule-col-header");
    if (d === todayIndex) header.classList.add("is-today");
    header.append(element("span", "", DAYS_OF_WEEK[d].label));
    header.style.gridRow = "1";
    header.style.gridColumn = String(d + 2);
    grid.append(header);
  }

  /* Row labels + background cells (one per period) */
  for (let p = 1; p <= MAX_PERIOD; p++) {
    const isBreak = BREAK_PERIODS.includes(p);
    const row = p + 1;

    const label = element("div", "schedule-row-label" + (isBreak ? " is-break" : ""));
    label.append(element("strong", "", String(p)));
    label.append(element("small", "", PERIODS[p - 1].timeLabel));
    label.style.gridRow = String(row);
    label.style.gridColumn = "1";
    grid.append(label);

    for (let d = 0; d < 7; d++) {
      const cell = element("div", "schedule-cell" + (isBreak ? " is-break" : ""));
      if (d === todayIndex) cell.classList.add("is-today");
      cell.style.gridRow = String(row);
      cell.style.gridColumn = String(d + 2);
      grid.append(cell);
    }
  }

  /* Course blocks: group by day|startPeriod|endPeriod, each group spans rows */
  const stackMap = new Map();
  for (const placed of placedSlots) {
    const key = placed.slot.dayOfWeek + "|" + placed.slot.startPeriod + "|" + placed.slot.endPeriod;
    if (!stackMap.has(key)) stackMap.set(key, []);
    stackMap.get(key).push(placed);
  }

  for (const entries of stackMap.values()) {
    const first = entries[0].slot;
    const stack = element("div", "schedule-stack");
    stack.style.gridColumn = String(first.dayOfWeek + 2);
    stack.style.gridRow = (first.startPeriod + 1) + " / " + (first.endPeriod + 2);
    for (const placed of entries) {
      stack.append(buildCourseBlock(placed));
    }
    grid.append(stack);
  }

  gridWrap.append(grid);

  /* Right: non-standard time courses */
  const nonStandard = element("div", "schedule-nonstandard");
  nonStandard.append(element("p", "schedule-nonstandard-title", "非标准时间课程"));

  if (unplaced.length) {
    for (const item of unplaced) {
      const stateClasses = [
        item.pending ? "is-pending" : "",
        item.isFocused ? "is-focused" : "",
        item.isConflictHighlight ? "is-conflict-highlight" : "",
        item.isContextMuted ? "is-context-muted" : "",
      ].filter(Boolean).join(" ");
      const nsItem = element(
        "div",
        `schedule-nonstandard-item${stateClasses ? ` ${stateClasses}` : ""}`,
      );
      nsItem.setAttribute("data-color", String(item.color));
      const nsTitleParts = [item.course.course_name || "未命名课程"];
      if (item.pending) nsTitleParts.push("（待选）");
      if (item.course.teacher_name) nsTitleParts.push("教师：" + item.course.teacher_name);
      if (item.course.teaching_place) nsTitleParts.push("时间地点：" + item.course.teaching_place);
      nsItem.title = nsTitleParts.join("\n");
      nsItem.append(element("strong", "", item.course.course_name || "未命名课程"));
      if (item.pending) {
        nsItem.append(element("span", "schedule-pending-badge", "待选"));
      }
      const meta = element("div", "schedule-nonstandard-meta");
      if (item.course.teacher_name) meta.append(element("span", "", item.course.teacher_name));
      if (item.course.teaching_place) meta.append(element("span", "", item.course.teaching_place));
      meta.append(element("span", "", item.reason));
      nsItem.append(meta);
      nonStandard.append(nsItem);
    }
  } else {
    nonStandard.append(element("p", "", "所有课程均在标准时段内。"));
  }

  /* Legend */
  if (pendingItems.length) {
    const legend = element("p", "schedule-legend");
    legend.append(element("i", "legend-dot legend-pending"));
    legend.append(document.createTextNode("虚化块为选课清单中的待选课程（未实际选上）"));
    nonStandard.append(legend);
  }

  container.append(gridWrap, nonStandard);
  wrap.replaceChildren(container);
}

function switchMyCoursesView(view) {
  appState.myCoursesView = view;
  const isGrid = view === "grid";
  appElements.scheduleViewGrid.classList.toggle("is-active", isGrid);
  appElements.scheduleViewList.classList.toggle("is-active", !isGrid);
  appElements.myCoursesScheduleWrap.hidden = !isGrid;
  appElements.myCoursesList.hidden = isGrid;
  appElements.myCoursesDialog.classList.toggle("is-wide-schedule", isGrid);
  if (isGrid) renderMyCoursesSchedule();
  else renderMyCourses();
}

function conflictScheduleSlots(classInfo) {
  return (typeof parseScheduleSlots === "function" ? parseScheduleSlots(classInfo.teaching_place || "") : [])
    .filter((slot) => (
      slot.dayOfWeek >= 0
      && slot.dayOfWeek <= 6
      && slot.startPeriod >= 1
      && slot.endPeriod <= MAX_PERIOD
      && slot.startPeriod <= slot.endPeriod
    ));
}

async function openConflictTimetable(course, classInfo) {
  const focusId = String(classInfo.teaching_class_id || "");
  const focusCourse = {
    ...course,
    id: focusId,
    teaching_class_id: focusId,
    teacher_name: classInfo.teacher_name || course.teacher_name || "",
    teaching_place: classInfo.teaching_place || course.teaching_place || "",
  };
  appState.scheduleConflict = {
    focusId,
    focusSlots: conflictScheduleSlots(classInfo),
    course: focusCourse,
    title: `${course.course_name || "未命名课程"} · ${classInfo.teacher_name || "教师待定"}`,
  };
  appElements.myCoursesDialog.showModal();
  switchMyCoursesView("grid");
  await loadCart();
  await loadMyCourses();
}


async function loadMyCourses(silent = false) {
  if (appState.loadingMyCourses) return;
  if (!appState.session?.logged_in) {
    if (!silent) showToast("请先登录后再查看已选课程", true);
    return;
  }
  appState.loadingMyCourses = true;
  const preserveExisting = appState.myCoursesLoaded;
  const previousLabel = appElements.refreshMyCourses.textContent;
  appElements.refreshMyCourses.disabled = true;
  appElements.refreshMyCourses.textContent = "刷新中...";
  if (!silent && !preserveExisting) {
    const loading = element("div", "empty-state");
    loading.append(element("strong", "", "正在读取已选课程"));
    loading.append(element("p", "", "正在向学校系统查询，请稍候。"));
    const target = appState.myCoursesView === "grid"
      ? appElements.myCoursesScheduleWrap
      : appElements.myCoursesList;
    target.replaceChildren(loading);
  } else if (!silent) {
    appElements.myCoursesHint.textContent = "正在刷新，当前仍显示上次成功结果。";
  }
  try {
    const data = await api("/api/school/enrolled", {
      timeoutMs: SESSION_RECOVERY_TIMEOUT_MS,
    });
    appState.myCourses = Array.isArray(data.courses) ? data.courses : [];
    appState.myCoursesLoaded = true;
    renderMyCourses();
    renderMyCoursesSchedule();
  } catch (error) {
    if (!(error instanceof SessionExpiredError)) {
      if (preserveExisting) {
        appElements.myCoursesHint.textContent = "刷新失败，仍显示上次成功结果。";
        if (!silent) showToast(`已选课程刷新失败：${error.message}`, true);
      } else if (!silent) {
        const errorState = element("div", "error-state");
        errorState.append(element("strong", "", "读取失败"));
        errorState.append(element("p", "", error.message));
        const actions = element("div", "state-actions");
        const retry = element("button", "button button-secondary", "重新加载");
        retry.type = "button";
        retry.addEventListener("click", () => loadMyCourses());
        actions.append(retry);
        errorState.append(actions);
        const target = appState.myCoursesView === "grid"
          ? appElements.myCoursesScheduleWrap
          : appElements.myCoursesList;
        target.replaceChildren(errorState);
      }
    }
  } finally {
    appState.loadingMyCourses = false;
    appElements.refreshMyCourses.disabled = false;
    appElements.refreshMyCourses.textContent = previousLabel;
  }
}

/* ---------------- Enrollment progress polling ---------------- */

function renderProgress(data) {
  const courses = (data && data.courses) || [];
  const hasProgress = courses.length > 0;
  appElements.enrollProgress.hidden = !hasProgress;
  if (!hasProgress) {
    appElements.taskControlButton.hidden = true;
    return;
  }

  const counts = data.counts || { total: 0, success: 0, failed: 0, active: 0 };
  const paused = Boolean(data.paused);
  const pauseAcknowledged = Boolean(data.pause_acknowledged);
  const stopping = Boolean(data.stopping);
  const recovering = Boolean(appState.session?.relogin_in_progress);
  appElements.progressCounts.textContent = `${counts.success} 抢到 · ${counts.failed} 失败 · ${counts.active} 待处理`;
  const completed = counts.success + counts.failed;
  const pct = counts.total ? Math.round((completed / counts.total) * 100) : 0;
  appElements.progressBarFill.style.width = `${pct}%`;

  appElements.progressState.className = "status-pill status-neutral";
  if (!data.running) {
    appElements.progressState.textContent = "任务已结束";
  } else if (stopping) {
    appElements.progressState.textContent = "正在结束";
    appElements.progressState.className = "status-pill status-warning";
  } else if (recovering) {
    appElements.progressState.textContent = "正在重新登录";
    appElements.progressState.className = "status-pill status-warning";
  } else if (paused) {
    appElements.progressState.textContent = pauseAcknowledged ? "已暂停" : "正在暂停";
    appElements.progressState.className = "status-pill status-warning";
  } else {
    appElements.progressState.textContent = "抢课中";
    appElements.progressState.className = "status-pill status-success";
  }

  appElements.taskControlButton.hidden = !data.running || stopping;
  appElements.taskControlButton.textContent = paused
    ? pauseAcknowledged ? "继续任务" : "正在暂停"
    : "暂停任务";
  appElements.taskControlButton.className = paused
    ? "button button-primary"
    : "button button-secondary";
  appElements.taskControlButton.disabled = appState.taskControlPending
    || stopping
    || (paused && (!pauseAcknowledged || recovering));
  if (stopping) {
    appElements.progressNotice.textContent = data.stopping_reason
      || "待处理课程已清空，后台任务正在结束。";
  } else if (recovering) {
    appElements.progressNotice.textContent = "学校会话已过期，正在自动重新登录；课程和当前进度均已保留。";
  } else if (paused && !pauseAcknowledged) {
    appElements.progressNotice.textContent = "正在等待当前学校请求结束；安全暂停后即可移除课程。";
  } else if (paused) {
    appElements.progressNotice.textContent = data.pause_reason
      || "任务已暂停；点击继续后从现有清单和尝试次数接着运行。";
  } else if (data.running) {
    appElements.progressNotice.textContent = "任务运行中；点击暂停后，会在当前学校请求结束后停止发送新请求。";
  } else {
    appElements.progressNotice.textContent = counts.active
      ? "仍有待处理课程，可重新启动任务。"
      : "本轮任务已经结束。";
  }

  const fragment = document.createDocumentFragment();
  for (const course of courses) {
    const row = element("div", "progress-row");
    const info = element("div");
    info.append(element("span", "p-name", course.name || course.id));
    info.append(
      element(
        "span",
        "p-msg",
        [course.campus_name, course.message].filter(Boolean).join(" · "),
      ),
    );
    const side = element("div", "cart-item-actions");
    const statusClass = course.status === "SUCCESS" ? "status-success" : course.status === "FAILED" ? "status-danger" : "status-warning";
    const statusLabel = stopping && course.status === "ENROLLING"
      ? "正在结束"
      : paused && course.status === "ENROLLING"
        ? pauseAcknowledged ? "已暂停" : "正在暂停"
      : statusNames[course.status] || course.status;
    side.append(element("span", `status-pill ${statusClass}`, statusLabel));
    side.append(element("span", "p-attempts", `${course.attempts || 0} 次 · 业务失败 ${course.failures || 0} 次`));
    row.append(info, side);
    fragment.append(row);
  }
  appElements.progressRows.replaceChildren(fragment);
}

async function toggleEnrollmentPause() {
  if (appState.taskControlPending || !appState.progress?.running) return;
  const paused = Boolean(appState.progress.paused);
  appState.taskControlPending = true;
  renderProgress(appState.progress);
  try {
    const result = await api(paused ? "/api/enroll/resume" : "/api/enroll/pause", {
      method: "POST",
      timeoutMs: SESSION_RECOVERY_TIMEOUT_MS,
    });
    if (result.progress) {
      const { controlsChanged } = applyProgressTaskState(result.progress);
      if (controlsChanged) renderCart();
    }
    showToast(result.message || (paused ? "抢课任务已继续" : "抢课任务已暂停"), false, true);
    renderProgress(appState.progress);
    updateTaskIndicator();
    setPhasePresentation();
    syncEnrollControls();
  } catch (error) {
    if (error.requiresManualLogin) showSessionDialog(error.message);
    else if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
    if (["PHASE_NOT_ALLOWED", "BATCH_UNAVAILABLE"].includes(error.code)) {
      await loadSession(false);
    }
  } finally {
    appState.taskControlPending = false;
    if (appState.progress) renderProgress(appState.progress);
  }
}

async function stopEnrollment() {
  const taskRunning = Boolean(appState.progress?.running || appState.session?.task_running);
  if (appState.taskControlPending || !taskRunning) return;
  if (appState.progress?.stopping || appState.session?.task_stopping) return;
  appState.taskControlPending = true;
  if (appState.progress) renderProgress(appState.progress);
  renderEnrollControls();
  try {
    const result = await api("/api/enroll/stop", {
      method: "POST",
      timeoutMs: SESSION_RECOVERY_TIMEOUT_MS,
    });
    if (appState.session) {
      appState.session.task_stopping = true;
      appState.session.task_stopping_reason = "用户请求停止抢课任务";
    }
    if (appState.progress) {
      applyProgressTaskState({
        ...appState.progress,
        stopping: true,
        stopping_reason: "用户请求停止抢课任务",
      });
    }
    showToast(result.message || "已请求停止抢课，等待当前请求结束", false, true);
    renderProgress(appState.progress);
    renderEnrollControls();
    await loadEnrollProgress();
    await loadSession(false, false);
  } catch (error) {
    if (error.requiresManualLogin) showSessionDialog(error.message);
    else if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
    await loadEnrollProgress();
  } finally {
    appState.taskControlPending = false;
    if (appState.progress) renderProgress(appState.progress);
    renderEnrollControls();
    syncEnrollControls();
  }
}

async function loadEnrollProgress() {
  if (appState.loadingProgress) return;
  appState.loadingProgress = true;
  let data;
  try {
    data = await api("/api/enroll/status");
  } catch (error) {
    if (error instanceof SessionExpiredError) stopProgressPolling();
    return;
  } finally {
    appState.loadingProgress = false;
  }
  const { controlsChanged, queueChanged } = applyProgressTaskState(data);
  applyEnrollSettings(data);
  renderProgress(data);
  updateTaskIndicator();
  setPhasePresentation();
  syncEnrollControls();
  if (queueChanged) await loadCart();
  else if (controlsChanged) renderCart();

  for (const course of data.courses || []) {
    if (course.status === "SUCCESS" && !appState.knownSuccessIds.has(course.id)) {
      appState.knownSuccessIds.add(course.id);
      showToast(`${course.name} 已加入我的课程`, false, true);
    }
  }

  if (data.running) {
    appState.wasTaskRunning = true;
  } else if (appState.wasTaskRunning) {
    appState.wasTaskRunning = false;
    stopProgressPolling();
    const counts = data.counts || { success: 0, failed: 0 };
    showToast(
      `抢课任务结束：成功 ${counts.success} 门，失败 ${counts.failed} 门，保留 ${counts.active || 0} 门`,
      counts.failed > 0 && counts.success === 0,
    );
    await loadCart();
    await loadMyCourses(true);
    await loadSession(false, false);
    invalidateCatalogCaches();
    if (!courseCatalogBlocked()) await refreshCurrentView();
  }
}

function startProgressPolling() {
  if (appState.progressTimer) return;
  loadEnrollProgress();
  appState.progressTimer = window.setInterval(loadEnrollProgress, 1500);
}

function stopProgressPolling() {
  if (appState.progressTimer) {
    window.clearInterval(appState.progressTimer);
    appState.progressTimer = null;
  }
}

function startSessionPolling() {
  if (appState.sessionTimer) return;
  appState.sessionTimer = window.setInterval(() => {
    if (!appState.refreshingPhase) loadSession(false);
  }, SESSION_POLL_INTERVAL_MS);
}

function stopSessionPolling() {
  if (appState.sessionTimer) {
    window.clearInterval(appState.sessionTimer);
    appState.sessionTimer = null;
  }
}

async function startEnrollment() {
  if (!appElements.phaseConfirmation.checked || !appState.grabPhase) return;
  appElements.startEnroll.disabled = true;
  appElements.enrollMessage.textContent = "正在启动后台任务...";
  try {
    const result = await api("/api/enroll/courses", {
      method: "POST",
      body: JSON.stringify({ confirmed_phase: true }),
    });
    if (result.is_error) throw new Error(result.message);
    appState.knownSuccessIds = new Set();
    appElements.enrollDialog.close();
    showToast(result.message || "后台任务已启动");
    await loadSession(false);
    await loadCart();
    startProgressPolling();
  } catch (error) {
    appElements.enrollMessage.textContent = error.message;
  } finally {
    appElements.startEnroll.disabled = !appElements.phaseConfirmation.checked;
  }
}

appElements.categoryList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-type]");
  if (!button || button.dataset.type === appState.type) return;
  for (const item of appElements.categoryList.querySelectorAll("[data-type]")) {
    item.classList.toggle("is-active", item === button);
  }
  appState.type = button.dataset.type;
  appState.page = 1;
  appState.searchKeyword = "";
  appState.searchResults = [];
  appState.searchPage = 1;
  appElements.courseSearch.value = "";
  abortCatalogFetch();
  loadCourses();
});

let searchDebounceTimer = null;
appElements.filterConflictSwitch.checked = appState.filters.hideConflict;
appElements.filterFullSwitch.checked = appState.filters.hideFull;
appElements.courseSearch.addEventListener("input", () => {
  if (searchDebounceTimer) window.clearTimeout(searchDebounceTimer);
  searchDebounceTimer = window.setTimeout(() => {
    searchDebounceTimer = null;
    handleSearchInput();
  }, SEARCH_DEBOUNCE_MS);
});
appElements.filterConflictSwitch.addEventListener("change", () => {
  appState.filters.hideConflict = appElements.filterConflictSwitch.checked;
  saveFilterPreferences();
  if (isFilterActive()) applyCourseFilter();
  renderCourses();
  updatePagination();
});
appElements.filterFullSwitch.addEventListener("change", () => {
  appState.filters.hideFull = appElements.filterFullSwitch.checked;
  saveFilterPreferences();
  if (isFilterActive()) applyCourseFilter();
  renderCourses();
  updatePagination();
});
appElements.cacheModeSwitch?.addEventListener("change", () => {
  setCacheMode(appElements.cacheModeSwitch.checked);
});
appElements.pauseEnroll?.addEventListener("click", () => toggleEnrollPause(true));
appElements.resumeEnroll?.addEventListener("click", () => toggleEnrollPause(false));
for (const button of appElements.modeButtons || []) {
  button.addEventListener("click", () => updateEnrollMode(button.dataset.enrollMode));
}
for (const input of [appElements.boostInterval, appElements.normalInterval, appElements.scanInterval]) {
  input?.addEventListener("change", saveEnrollSettings);
}
for (const [input, checkbox] of [
  [appElements.boostFailureLimit, appElements.boostFailureUnlimited],
  [appElements.normalFailureLimit, appElements.normalFailureUnlimited],
]) {
  input?.addEventListener("change", saveEnrollSettings);
  checkbox?.addEventListener("change", () => {
    if (input) input.disabled = checkbox.checked;
    saveEnrollSettings();
  });
}
appElements.campusSelect.addEventListener("change", () => {
  switchCampus(appElements.campusSelect.value);
});
appElements.refreshCourses.addEventListener("click", refreshCurrentView);
appElements.refreshPhase.addEventListener("click", refreshPhaseAndCourses);
appElements.previousPage.addEventListener("click", () => {
  if (isFilterActive()) {
    if (appState.searchPage > 1) {
      appState.searchPage -= 1;
      applyCourseFilter();
      renderCourses();
      updatePagination();
    }
    return;
  }
  if (appState.page > 1) {
    appState.page -= 1;
    loadCourses();
  }
});
appElements.nextPage.addEventListener("click", () => {
  if (isFilterActive()) {
    const results = Array.isArray(appState.searchResults) ? appState.searchResults : [];
    const totalPages = Math.max(1, Math.ceil(results.length / FILTER_PAGE_SIZE));
    if (appState.searchPage < totalPages) {
      appState.searchPage += 1;
      applyCourseFilter();
      renderCourses();
      updatePagination();
    }
    return;
  }
  appState.page += 1;
  loadCourses();
});
appElements.openCart.addEventListener("click", async () => {
  await loadCart();
  if (appState.session?.task_running) await loadEnrollProgress();
  appElements.cartDialog.showModal();
});
appElements.openMyCourses.addEventListener("click", async () => {
  appState.scheduleConflict = null;
  appElements.myCoursesDialog.showModal();
  switchMyCoursesView(appState.myCoursesView);
  await loadCart();
  await loadMyCourses();
});
appElements.refreshMyCourses.addEventListener("click", () => loadMyCourses());
appElements.scheduleViewGrid.addEventListener("click", () => switchMyCoursesView("grid"));
appElements.scheduleViewList.addEventListener("click", () => switchMyCoursesView("list"));
appElements.showPendingSwitch.addEventListener("change", () => {
  appState.showCartOnSchedule = appElements.showPendingSwitch.checked;
  renderMyCourses();
  renderMyCoursesSchedule();
});
appElements.openEnrollConfirm.addEventListener("click", () => {
  if (!appState.grabPhase) return;
  appElements.phaseConfirmation.checked = false;
  appElements.startEnroll.disabled = true;
  appElements.enrollMessage.textContent = "";
  appElements.enrollDialog.showModal();
});
appElements.phaseConfirmation.addEventListener("change", () => {
  appElements.startEnroll.disabled = !appElements.phaseConfirmation.checked;
});
appElements.startEnroll.addEventListener("click", startEnrollment);
appElements.taskControlButton.addEventListener("click", toggleEnrollmentPause);
appElements.stopEnroll?.addEventListener("click", stopEnrollment);
appElements.studentLabel?.addEventListener("click", requestAutomaticRelogin);
appElements.logout.addEventListener("click", async () => {
  try {
    const result = await api("/api/logout", { method: "POST" });
    if (result.is_error) throw new Error(result.message);
    stopProgressPolling();
    stopSessionPolling();
    appState.cacheMode = false;
    clearCacheRefreshTimer();
    if (appElements.cacheModeSwitch) appElements.cacheModeSwitch.checked = false;
    window.location.assign(cleanPagePath("/login"));
  } catch (error) {
    if (!(error instanceof SessionExpiredError)) showToast(error.message, true);
  }
});

for (const closeButton of document.querySelectorAll("[data-close-dialog]")) {
  closeButton.addEventListener("click", () => {
    document.querySelector(`#${closeButton.dataset.closeDialog}`)?.close();
  });
}

window.addEventListener?.("pagehide", clearCacheRefreshTimer);

async function initializeApp() {
  stripUiQuery();
  try {
    const saved = JSON.parse(window.localStorage?.getItem("szu-course-help.cart-preferences.v1") || "null");
    if (saved && typeof saved === "object") appState.cartPreferences = saved;
  } catch {}
  await loadSession(false);
  if (appState.session?.logged_in) {
    try { applyEnrollSettings(await planApi("/api/enroll/settings")); } catch (error) {
      if (error.status !== 404) showToast(`抢课设置读取失败：${planErrorMessage(error)}`, true);
    }
  }
  startSessionPolling();
  appElements.brandLink.href = cleanPagePath("/");
  appElements.sessionLoginLink.href = cleanPagePath("/login");
  await loadCart();
  if (appState.session?.logged_in) {
    await loadCourses();
  } else {
    await loadCourses({ cacheOnly: true });
  }
}

initializeApp();
