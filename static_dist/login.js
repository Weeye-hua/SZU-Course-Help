"use strict";

const REQUEST_TIMEOUT_MS = 30000;
const CAPTCHA_IMAGE_TIMEOUT_MS = 8000;

class ApiError extends Error {
  constructor(message, { status = 0, code = "UNKNOWN_ERROR", retryable = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

const loginState = {
  program: "undergraduate",
  captcha: null,
  captchaStatus: "idle",
  captchaFailureMessage: "",
  points: [],
  loadingCaptcha: false,
  solvingCaptcha: false,
  submitting: false,
  backend: "auto",
  prefilledStudentId: "",
  cardKey: "",
  webvpnAuthTimer: 0,
  webvpnAuthRequested: false,
};

const loginElements = {
  form: document.querySelector("#loginForm"),
  studentId: document.querySelector("#studentId"),
  password: document.querySelector("#password"),
  passwordToggle: document.querySelector("#passwordToggle"),
  phaseNotice: document.querySelector("#phaseNotice"),
  stage: document.querySelector("#captchaStage"),
  image: document.querySelector("#captchaImage"),
  markers: document.querySelector("#captchaMarkers"),
  progress: document.querySelector("#captchaProgress"),
  statusTitle: document.querySelector("#captchaStatusTitle"),
  statusDetail: document.querySelector("#captchaStatusDetail"),
  undo: document.querySelector("#undoPoint"),
  refresh: document.querySelector("#refreshCaptcha"),
  solveCaptcha: document.querySelector("#solveCaptcha"),
  message: document.querySelector("#loginMessage"),
  submit: document.querySelector("#loginButton"),
  backendStatus: document.querySelector("#backendStatus"),
  webvpnAuthButton: document.querySelector("#webvpnAuthButton"),
  captchaWebvpnAuth: document.querySelector("#captchaWebvpnAuth"),
  captchaWebvpnAuthButton: document.querySelector("#captchaWebvpnAuthButton"),
  captchaActions: document.querySelector("#captchaActions"),
};

const captchaStatusCopy = {
  idle: ["准备获取验证码", "正在准备本地登录环境。"],
  loading: ["正在获取验证码", "正在连接学校选课系统，请稍候。"],
  ready: ["验证码已就绪", "点击“自动识别”自动填入，或按顶部提示依次点击四个汉字。"],
  unavailable: [
    "当前时段暂无验证码",
    "学校当前没有返回登录验证码，请等待选课开放或维护结束后再试。",
  ],
  error: ["验证码加载失败", "本次加载已经停止，请查看下方提示后手动重试。"],
};

function setLoginMessage(message, success = false) {
  loginElements.message.textContent = message || "";
  loginElements.message.classList.toggle("is-success", success);
}

function selectedBackend() {
  if (loginState.program === "graduate") return "primary";
  return document.querySelector("input[name='backend']:checked")?.value || "auto";
}

function setBackendPresentation(payload = {}) {
  const preference = payload.preference || loginState.backend || "auto";
  loginState.backend = preference;
  const preferenceLabel = payload.preference_label || (preference === "auto" ? "自动（优先主站）" : preference === "webvpn" ? "WebVPN 备用" : "主站");
  const activeLabel = payload.active_backend_label ? `；当前后端：${payload.active_backend_label}` : "";
  loginElements.backendStatus.textContent = `访问策略：${preferenceLabel}${activeLabel}`;
  const webvpnReady = Boolean(payload.webvpn_authenticated || payload.authenticated);
  const authNeeded = Boolean(payload.requires_webvpn_auth) || loginState.webvpnAuthRequested;
  loginElements.webvpnAuthButton.hidden = (!authNeeded && preference !== "webvpn") || webvpnReady;
  if (webvpnReady) loginElements.webvpnAuthButton.textContent = "WebVPN 已认证";
}

async function selectBackend(value) {
  loginState.backend = value;
  loginState.webvpnAuthRequested = value === "webvpn";
  if (value === "webvpn") {
    // Clear the previous primary-server captcha immediately.  Otherwise the
    // old image remains visible while the backend-selection request is in
    // flight and can be mistaken for a WebVPN captcha.
    loginState.captcha = null;
    clearCaptchaPoints();
    clearCaptchaImage();
    setCaptchaStatus(
      "webvpn-auth-required",
      "需要 WebVPN 统一认证",
      "当前选择 WebVPN，但验证码暂时无法获取，请先完成统一认证。",
    );
    setLoginMessage("请先完成 WebVPN 统一认证", false);
  } else {
    loginElements.stage.hidden = false;
    loginElements.captchaWebvpnAuth.hidden = true;
    loginElements.captchaActions.hidden = false;
  }
  try {
    const payload = await requestJson("/api/backend/select", {
      method: "POST",
      body: JSON.stringify({ backend: value }),
    });
    setBackendPresentation(payload);
    if (payload.requires_webvpn_auth) {
      setCaptchaStatus(
        "webvpn-auth-required",
        "需要 WebVPN 统一认证",
        "当前选择 WebVPN，但验证码暂时无法获取，请先完成统一认证。",
      );
      setLoginMessage("请先完成 WebVPN 统一认证", false);
      return;
    }
    await loadCaptcha();
  } catch (error) {
    setLoginMessage(error instanceof Error ? error.message : "后端切换失败");
  }
}

function updateLoginControls() {
  const captchaReady = loginState.captchaStatus === "ready" && Boolean(loginState.captcha);
  const busy = loginState.loadingCaptcha || loginState.submitting || loginState.solvingCaptcha;
  loginElements.refresh.disabled = busy;
  loginElements.undo.disabled = busy || !captchaReady || loginState.points.length === 0;
  loginElements.solveCaptcha.disabled = busy || !captchaReady;
  loginElements.submit.disabled = loginState.submitting || loginState.solvingCaptcha || !captchaReady;
  loginElements.solveCaptcha.textContent = loginState.solvingCaptcha ? "识别中…" : "自动识别";
  const textInput = document.querySelector("#textCaptcha");
  if (textInput) textInput.disabled = busy || !captchaReady;
}

function isValidStudentId(value) {
  return /^\d{6,12}$/.test(String(value || "").trim());
}

function prefilledCardKeyForStudent(studentId) {
  if (
    studentId !== loginState.prefilledStudentId
    || typeof loginState.cardKey !== "string"
    || !loginState.cardKey.startsWith("SZU3.")
  ) {
    throw new ApiError("学号与终端生成的卡密不一致，请重新启动程序并输入该学号", {
      code: "CARD_KEY_STUDENT_MISMATCH",
    });
  }
  return loginState.cardKey;
}


function setCaptchaStatus(status, title = "", detail = "") {
  const fallback = captchaStatusCopy[status] || captchaStatusCopy.error;
  const webvpnFallback = status === "webvpn-auth-required"
    || (loginState.backend === "webvpn" && status !== "ready" && status !== "loading");
  loginState.captchaStatus = status;
  loginElements.stage.dataset.state = status;
  loginElements.stage.hidden = webvpnFallback;
  loginElements.stage.setAttribute("aria-busy", String(status === "loading"));
  loginElements.stage.setAttribute("aria-disabled", String(status !== "ready"));
  loginElements.captchaActions.hidden = webvpnFallback;
  loginElements.statusTitle.textContent = title || fallback[0];
  loginElements.statusDetail.textContent = detail || fallback[1];
  if (loginState.program === "graduate" && status === "ready") {
    loginElements.statusDetail.textContent = "请输入图片中的四位字符。";
  }
  loginElements.captchaWebvpnAuth.hidden = !webvpnFallback && status !== "webvpn-auth-required";
  if (webvpnFallback) {
    const detailElement = loginElements.captchaWebvpnAuth.querySelector("span");
    if (detailElement) {
      detailElement.textContent = "当前选择 WebVPN，但验证码暂时无法获取。请先完成统一认证后重试。";
    }
  }
  loginElements.refresh.textContent = status === "ready" ? "刷新验证码" : "重新获取验证码";
  updateLoginControls();
}


async function solveCaptcha() {
  if (loginState.solvingCaptcha || loginState.submitting) return;
  if (!loginState.captcha || loginState.captchaStatus !== "ready") return;

  loginState.solvingCaptcha = true;
  updateLoginControls();
  clearCaptchaPoints();
  setLoginMessage("正在自动识别验证码...", true);

  try {
    const result = await requestJson("/api/captcha/solve", {
      method: "POST",
      body: JSON.stringify({
        imageUrl: loginState.captcha.imageUrl,
        vtoken: loginState.captcha.vtoken,
        cookie: loginState.captcha.cookie,
      }),
    });
    if (result.captcha) {
      loginState.captcha = result.captcha;
      await loadCaptchaImage(result.captcha.imageUrl);
      renderCaptchaPoints();
    }
    if (loginState.program === "graduate") {
      document.querySelector("#textCaptcha").value = result.text || "";
      setLoginMessage(result.text ? "验证码已填入" : result.message || "未能识别，请手动输入", Boolean(result.text));
      return;
    }
    const points = result.points;
    if (Array.isArray(points) && points.length === 4) {
      loginState.points = points.map((p) => {
        const x = Math.max(0, Math.min(250, Math.round(p[0])));
        const y = Math.max(0, Math.min(80, Math.round(p[1])));
        return [x, y];
      });
      renderCaptchaPoints();
      setLoginMessage("已自动识别四个汉字，可直接登录", true);
    } else {
      setLoginMessage(result.message || "OCR 未能识别，请手动点击四个汉字");
    }
  } catch (error) {
    setLoginMessage(error instanceof Error ? error.message : "OCR 识别失败，请手动点击");
  } finally {
    loginState.solvingCaptcha = false;
    updateLoginControls();
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

async function readJson(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

async function requestJson(url, options = {}) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      cache: "no-store",
      ...options,
      signal: controller.signal,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const data = await readJson(response);
    if (!response.ok) {
      throw new ApiError(data.message || data.detail || "请求失败，请稍后重试", {
        status: response.status,
        code: data.error_code || "HTTP_ERROR",
        retryable: Boolean(data.retryable),
      });
    }
    return data;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError("请求超过 30 秒，本次操作已停止。请检查网络后手动重试。", {
        code: "CLIENT_TIMEOUT",
        retryable: true,
      });
    }
    if (error instanceof ApiError) throw error;
    throw new ApiError("无法连接本地程序，请确认启动终端仍在运行。", {
      code: "CLIENT_NETWORK_ERROR",
      retryable: true,
    });
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function renderCaptchaPoints() {
  loginElements.markers.replaceChildren();
  for (const [index, point] of loginState.points.entries()) {
    const marker = document.createElement("span");
    marker.className = "captcha-marker";
    marker.textContent = String(index + 1);
    marker.style.left = `${(point[0] / 250) * 100}%`;
    marker.style.top = `${(point[1] / 80) * 100}%`;
    loginElements.markers.append(marker);
  }
  loginElements.progress.textContent = `${loginState.points.length} / 4`;
  updateLoginControls();
}

function clearCaptchaPoints() {
  loginState.points = [];
  renderCaptchaPoints();
}

function clearCaptchaImage() {
  loginElements.image.onload = null;
  loginElements.image.onerror = null;
  loginElements.image.removeAttribute("src");
}

function validateCaptchaPayload(captcha) {
  const valid =
    captcha &&
    typeof captcha === "object" &&
    typeof captcha.vtoken === "string" &&
    captcha.vtoken.length > 0 &&
    captcha.vtoken.length <= 512 &&
    typeof captcha.cookie === "string" &&
    captcha.cookie.length > 0 &&
    captcha.cookie.length <= 8192 &&
    typeof captcha.imageUrl === "string" &&
    captcha.imageUrl.startsWith("data:image/") &&
    captcha.imageUrl.length <= 3 * 1024 * 1024;
  if (!valid) {
    throw new ApiError("学校验证码数据不完整，本次加载已停止。请手动重试。", {
      code: "CAPTCHA_INVALID_RESPONSE",
      retryable: true,
    });
  }
  return captcha;
}

function loadCaptchaImage(source) {
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      cleanup();
      reject(
        new ApiError("验证码图片解析超时，本次加载已停止。请手动重试。", {
          code: "CAPTCHA_IMAGE_TIMEOUT",
          retryable: true,
        }),
      );
    }, CAPTCHA_IMAGE_TIMEOUT_MS);

    function cleanup() {
      window.clearTimeout(timeoutId);
      loginElements.image.onload = null;
      loginElements.image.onerror = null;
    }

    loginElements.image.onload = () => {
      cleanup();
      resolve();
    };
    loginElements.image.onerror = () => {
      cleanup();
      reject(
        new ApiError("验证码图片无法显示，本次加载已停止。请手动重试。", {
          code: "CAPTCHA_IMAGE_ERROR",
          retryable: true,
        }),
      );
    };
    loginElements.image.src = source;
  });
}

function describeCaptchaFailure(error) {
  const code = error instanceof ApiError ? error.code : "UNKNOWN_ERROR";
  const message = error instanceof Error ? error.message : "验证码加载失败，请手动重试。";
  if (code === "CAPTCHA_UNAVAILABLE") {
    return {
      status: "unavailable",
      title: "当前时段暂无验证码",
      detail: "学校未开放验证码接口。请等待预选、复选、补选开放或维护结束后再试。",
      message,
    };
  }
  if (code === "WEBVPN_AUTH_REQUIRED") {
    return {
      status: "webvpn-auth-required",
      title: "需要 WebVPN 统一认证",
      detail: "验证码接口需要 WebVPN 登录，请先完成统一认证后再获取验证码。",
      message,
    };
  }
  if (["CAPTCHA_TIMEOUT", "CLIENT_TIMEOUT", "CAPTCHA_IMAGE_TIMEOUT"].includes(code)) {
    return {
      status: "error",
      title: "验证码请求超时",
      detail: "本次加载已经停止，不会在后台自动循环。请检查网络后手动重试。",
      message,
    };
  }
  if (code === "CAPTCHA_NETWORK_ERROR") {
    return {
      status: "error",
      title: "学校系统暂不可达",
      detail: "本次加载已经停止。请检查网络，或等待学校服务恢复后手动重试。",
      message,
    };
  }
  if (code === "CLIENT_NETWORK_ERROR") {
    return {
      status: "error",
      title: "本地服务连接失败",
      detail: "请确认启动程序的终端窗口仍然开启，然后刷新本页。",
      message,
    };
  }
  if (["CAPTCHA_INVALID_RESPONSE", "CAPTCHA_IMAGE_ERROR"].includes(code)) {
    return {
      status: "error",
      title: "验证码响应异常",
      detail: "学校没有返回可用的验证码。本次加载已经停止，请稍后手动重试。",
      message,
    };
  }
  return {
    status: "error",
    title: "验证码加载失败",
    detail: "本次加载已经停止，请查看下方原因后手动重试。",
    message,
  };
}

async function loadCaptcha() {
  if (loginState.loadingCaptcha) return false;
  loginState.loadingCaptcha = true;
  loginState.captcha = null;
  loginState.captchaFailureMessage = "";
  const textInput = document.querySelector("#textCaptcha");
  if (textInput) textInput.value = "";
  clearCaptchaPoints();
  clearCaptchaImage();
  setCaptchaStatus("loading");
  setLoginMessage("");

  try {
    const captcha = validateCaptchaPayload(await requestJson(`/api/captcha?backend=${encodeURIComponent(loginState.backend)}&t=${Date.now()}`));
    await loadCaptchaImage(captcha.imageUrl);
    loginState.captcha = captcha;
    setCaptchaStatus("ready");
    return true;
  } catch (error) {
    loginState.captcha = null;
    clearCaptchaImage();
    if (loginState.backend === "webvpn" || error?.code === "WEBVPN_AUTH_REQUIRED") {
      loginElements.webvpnAuthButton.hidden = false;
    }
    const failure = describeCaptchaFailure(error);
    loginState.captchaFailureMessage = failure.message;
    setCaptchaStatus(failure.status, failure.title, failure.detail);
    setLoginMessage(failure.message);
    return false;
  } finally {
    loginState.loadingCaptcha = false;
    updateLoginControls();
  }
}

function stopWebvpnAuthPolling() {
  if (loginState.webvpnAuthTimer) {
    window.clearTimeout(loginState.webvpnAuthTimer);
    loginState.webvpnAuthTimer = 0;
  }
}

async function pollWebvpnAuth() {
  stopWebvpnAuthPolling();
  try {
    const payload = await requestJson("/api/webvpn/auth/status");
    setBackendPresentation(payload);
    if (payload.state === "authenticated" || payload.authenticated) {
      loginState.webvpnAuthRequested = false;
      loginElements.webvpnAuthButton.textContent = "WebVPN 已认证";
      setLoginMessage("WebVPN 统一认证完成，正在刷新验证码。", true);
      await loadCaptcha();
      return;
    }
    if (payload.state === "error") {
      loginElements.webvpnAuthButton.textContent = "重新完成 WebVPN 统一认证";
      setLoginMessage(payload.message || "WebVPN 认证未完成");
      return;
    }
    loginElements.webvpnAuthButton.textContent = "认证进行中…";
    setLoginMessage(payload.message || "请在新打开的受控浏览器中完成认证");
    loginState.webvpnAuthTimer = window.setTimeout(pollWebvpnAuth, 1000);
  } catch (error) {
    loginState.webvpnAuthTimer = window.setTimeout(pollWebvpnAuth, 2000);
    setLoginMessage(error instanceof Error ? error.message : "无法读取 WebVPN 认证状态");
  }
}

async function startWebvpnAuth() {
  if (loginState.webvpnAuthTimer) return;
  loginState.webvpnAuthRequested = true;
  loginElements.webvpnAuthButton.disabled = true;
  loginElements.webvpnAuthButton.textContent = "正在打开受控浏览器…";
  setLoginMessage("正在启动独立认证浏览器，请稍候。", true);
  try {
    const payload = await requestJson("/api/webvpn/auth/start", {
      method: "POST",
      body: JSON.stringify({}),
    });
    setBackendPresentation(payload);
    if (payload.state === "authenticated" || payload.authenticated) {
      await loadCaptcha();
      return;
    }
    await pollWebvpnAuth();
  } catch (error) {
    loginElements.webvpnAuthButton.textContent = "完成 WebVPN 统一认证";
    setLoginMessage(error instanceof Error ? error.message : "受控浏览器启动失败");
  } finally {
    loginElements.webvpnAuthButton.disabled = false;
  }
}

function addCaptchaPoint(event) {
  if (loginState.program === "graduate") return;
  if (
    loginState.captchaStatus !== "ready" ||
    !loginState.captcha ||
    loginState.points.length >= 4
  ) {
    return;
  }

  const rect = loginElements.image.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const x = Math.max(0, Math.min(250, Math.round(((event.clientX - rect.left) / rect.width) * 250)));
  const y = Math.max(0, Math.min(80, Math.round(((event.clientY - rect.top) / rect.height) * 80)));
  loginState.points.push([x, y]);
  renderCaptchaPoints();
}

async function submitLogin(event) {
  event.preventDefault();
  if (loginState.submitting) return;

  const studentId = loginElements.studentId.value.trim();
  const password = loginElements.password.value;
  if (!/^\d{6,12}$/.test(studentId)) {
    setLoginMessage("请输入 6 至 12 位数字学号");
    loginElements.studentId.focus();
    return;
  }
  if (!password) {
    setLoginMessage("请输入选课系统密码");
    loginElements.password.focus();
    return;
  }
  if (!loginState.captcha || loginState.captchaStatus !== "ready") {
    setLoginMessage(loginState.captchaFailureMessage || "请先成功获取验证码");
    return;
  }
  const graduate = loginState.program === "graduate";
  const textCode = document.querySelector("#textCaptcha")?.value.trim() || "";
  if (graduate && !/^[A-Za-z0-9]{4}$/.test(textCode)) {
    setLoginMessage("请输入图片中的四位字母或数字验证码");
    document.querySelector("#textCaptcha").focus();
    return;
  }
  if (!graduate && loginState.points.length !== 4) {
    setLoginMessage("请按顺序完成四个验证码点击点");
    return;
  }

  loginState.submitting = true;
  updateLoginControls();
  loginElements.submit.textContent = "正在验证登录";
  setLoginMessage("正在连接学校选课系统...", true);

  try {
    const cardKey = prefilledCardKeyForStudent(studentId);
    const result = await requestJson("/api/login", {
      method: "POST",
      body: JSON.stringify({
        student_id: studentId,
        password,
        card_key: cardKey,
        vtoken: loginState.captcha.vtoken,
        verifyCode: graduate ? textCode : loginState.points,
        cookie: loginState.captcha.cookie,
        backend: loginState.backend,
      }),
    });
    setLoginMessage(result.message || "登录成功", true);
    window.location.assign(cleanPagePath("/"));
  } catch (error) {
    const failureMessage = error instanceof Error ? error.message : "登录失败";
    const refreshed = await loadCaptcha();
    if (refreshed) {
      setLoginMessage(`${failureMessage}；验证码已更新，${graduate ? "请重新输入四位字符" : "请重新点击四个汉字"}。`);
    } else {
      const captchaMessage = loginState.captchaFailureMessage || "新验证码获取失败，请手动重试。";
      setLoginMessage(`${failureMessage}；${captchaMessage}`);
    }
  } finally {
    loginState.submitting = false;
    loginElements.submit.textContent = "登录并进入工作台";
    updateLoginControls();
  }
}

async function initializeLogin() {
  stripUiQuery();
  setCaptchaStatus("idle");
  try {
    const [bootstrap, session] = await Promise.all([
      requestJson("/api/bootstrap"),
      requestJson("/api/session"),
    ]);
    loginState.program = bootstrap.program || "undergraduate";
    const graduate = loginState.program === "graduate";
    document.querySelector("#programLabel").textContent = `${graduate ? "研究生" : "本科生"} · 本地安全登录`;
    document.querySelector("#textCaptchaField").hidden = !graduate;
    document.querySelector(".backend-fieldset").hidden = graduate;
    loginElements.stage.classList.toggle("is-text-captcha", graduate);
    loginElements.undo.hidden = graduate;
    loginElements.progress.hidden = graduate;
    loginElements.markers.hidden = graduate;
    if (graduate) {
      document.querySelector("#loginIntro").textContent = "研究生选课系统";
      document.querySelector("#captchaLabel").textContent = "图片验证码";
      loginElements.image.alt = "四位字符验证码";
    }
    setBackendPresentation(bootstrap);
    const radio = document.querySelector(`input[name='backend'][value='${bootstrap.preference || "auto"}']`);
    if (radio) radio.checked = true;
    if (session.logged_in) {
      window.location.replace(cleanPagePath("/"));
      return;
    }
    loginElements.studentId.value = bootstrap.student_id || "";
    loginState.prefilledStudentId = bootstrap.student_id || "";
    loginState.cardKey = bootstrap.card_key || "";
    if (bootstrap.phase_notice) {
      loginElements.phaseNotice.textContent = bootstrap.phase_notice;
    }
  } catch (error) {
    setLoginMessage(error instanceof Error ? error.message : "本地登录信息加载失败");
  }
  await loadCaptcha();
}

loginElements.stage.addEventListener("click", addCaptchaPoint);
loginElements.undo.addEventListener("click", () => {
  loginState.points.pop();
  renderCaptchaPoints();
});
loginElements.refresh.addEventListener("click", loadCaptcha);
loginElements.solveCaptcha.addEventListener("click", solveCaptcha);
loginElements.form.addEventListener("submit", submitLogin);
loginElements.passwordToggle.addEventListener("click", () => {
  const hidden = loginElements.password.type === "password";
  loginElements.password.type = hidden ? "text" : "password";
  loginElements.passwordToggle.textContent = hidden ? "隐藏" : "显示";
  loginElements.passwordToggle.setAttribute("aria-label", hidden ? "隐藏密码" : "显示密码");
});

for (const radio of document.querySelectorAll("input[name='backend']")) {
  radio.addEventListener("change", () => selectBackend(radio.value));
}

loginElements.webvpnAuthButton.addEventListener("click", startWebvpnAuth);
loginElements.captchaWebvpnAuthButton.addEventListener("click", startWebvpnAuth);

initializeLogin();
