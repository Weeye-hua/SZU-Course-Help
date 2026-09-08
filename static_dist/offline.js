"use strict";

const COURSE_TYPES = {
  TJKC: "本班推荐",
  FANKC: "方案内课程",
};
const PAGE_SIZE = 10;
const state = {
  graduate: false,
  type: "TJKC",
  page: 1,
  keyword: "",
  courses: [],
  metadata: {},
};

const elements = {
  types: document.querySelector("#offlineTypes"),
  search: document.querySelector("#offlineSearch"),
  title: document.querySelector("#offlineTitle"),
  summary: document.querySelector("#offlineSummary"),
  list: document.querySelector("#offlineCourseList"),
  previous: document.querySelector("#offlinePrevious"),
  next: document.querySelector("#offlineNext"),
  pageLabel: document.querySelector("#offlinePageLabel"),
  cartSummary: document.querySelector("#offlineCartSummary"),
  cartList: document.querySelector("#offlineCartList"),
};

function makeElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function classIsSelected(item) {
  return String(item?.is_choose || "") === "1" || item?.is_choose === true;
}

function classIsFull(item) {
  if (String(item?.is_full || "") === "1") return true;
  const selected = Number(item?.number_of_selected);
  const capacity = Number(item?.class_capacity);
  return Number.isFinite(selected) && Number.isFinite(capacity) && selected >= capacity;
}

function classIsConflict(item) {
  return String(item?.is_conflict || item?.conflict || "") === "1";
}

function cacheDate(value) {
  const timestamp = Number(value);
  return Number.isFinite(timestamp) && timestamp > 0
    ? new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false })
    : "时间未知";
}

async function requestJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.message || "本地缓存暂不可用");
  }
  return payload;
}

function classStatus(item) {
  if (classIsSelected(item)) return ["已选", "tag-chosen"];
  if (classIsConflict(item)) return ["时间冲突", "tag-conflict"];
  if (classIsFull(item)) return ["已满", "tag-full"];
  return ["可加入", "tag-open"];
}

function appendClass(container, course, item) {
  const row = makeElement("div", "class-row");
  const primary = makeElement("div", "class-primary");
  primary.append(
    makeElement("strong", "", item.teacher_name || "教师待定"),
    makeElement("span", "", item.course_index || item.teaching_class_id || ""),
  );
  const location = makeElement("div", "class-location");
  location.append(
    makeElement("strong", "", item.teaching_place || "时间地点待定"),
    makeElement("span", "", item.sport_name || "教学安排"),
  );
  const capacity = makeElement("div", "class-capacity");
  capacity.append(
    makeElement("strong", "", `${item.number_of_selected || item.course_total_number || "-"} / ${item.class_capacity || "-"}`),
    makeElement("span", "", "已选 / 容量"),
  );
  const [label, labelClass] = classStatus(item);
  const actions = makeElement("div", "class-actions");
  actions.append(makeElement("span", `class-tag ${labelClass}`, label));
  row.append(primary, location, capacity, actions);
  container.append(row);
}

function renderCourses() {
  const keyword = state.keyword.toLowerCase();
  const filtered = state.courses.filter((course) => {
    if (!keyword) return true;
    const teachers = (course.tcList || []).map((item) => item.teacher_name || "").join(" ");
    return `${course.course_name || ""} ${course.course_number || ""} ${course.department_name || ""} ${teachers}`
      .toLowerCase()
      .includes(keyword);
  });
  const totalPages = Math.max(1, Math.ceil((state.graduate ? Number(state.metadata.total_count || 0) : filtered.length) / PAGE_SIZE));
  state.page = Math.min(state.page, totalPages);
  const pageCourses = state.graduate ? filtered : filtered.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);
  elements.list.replaceChildren();
  if (!pageCourses.length) {
    elements.list.append(makeElement("div", "empty-state", "暂无符合条件的本地课程"));
  } else {
    const fragment = document.createDocumentFragment();
    pageCourses.forEach((course) => {
      const selected = (course.tcList || []).some(classIsSelected);
      const group = makeElement("details", `course-group${selected ? " is-selected" : ""}`);
      const summary = makeElement("summary");
      const main = makeElement("div", "course-summary-main");
      main.append(
        makeElement("strong", "", course.course_name || "未命名课程"),
        makeElement("span", "", [
          course.course_number,
          course.course_type_name,
          course.course_nature_name,
          course.department_name,
          course.credit ? `${course.credit} 学分` : "",
        ].filter(Boolean).join(" · ") || "课程信息待定"),
      );
      const side = makeElement("div", "course-summary-side");
      side.append(makeElement("span", "", `${(course.tcList || []).length} 个教学班`));
      summary.append(main, side);
      const classes = makeElement("div", "class-list");
      if (!course.tcList?.length) classes.append(makeElement("div", "empty-state", "暂无教学班"));
      else course.tcList.forEach((item) => appendClass(classes, course, item));
      group.append(summary, classes);
      fragment.append(group);
    });
    elements.list.append(fragment);
  }
  const cachedText = state.metadata.cached
    ? `；缓存于 ${cacheDate(state.metadata.cached_at)}`
    : "";
  elements.summary.textContent = `共 ${state.graduate ? state.metadata.total_count || 0 : filtered.length} 门课程，本页 ${pageCourses.length} 门${cachedText}`;
  elements.pageLabel.textContent = `第 ${state.page} / ${totalPages} 页`;
  elements.previous.disabled = state.page <= 1;
  elements.next.disabled = state.page >= totalPages;
}

async function loadCourses(type = state.type, page = 1) {
  state.type = type;
  state.page = page;
  state.keyword = elements.search.value.trim();
  elements.title.textContent = COURSE_TYPES[type];
  elements.summary.textContent = "正在读取本地缓存";
  elements.list.replaceChildren(makeElement("div", "empty-state", "正在读取本地缓存"));
  try {
    const data = await requestJson(
      `/api/school/courses?type=${encodeURIComponent(type)}&page=${page}&page_size=10&cache_mode=true`,
    );
    state.courses = Array.isArray(data.courses) ? data.courses : [];
    state.metadata = data;
    renderCourses();
  } catch (error) {
    state.courses = [];
    state.metadata = {};
    elements.summary.textContent = error.message;
    elements.list.replaceChildren(
      makeElement("div", "empty-state", `暂无${COURSE_TYPES[type]}本地缓存`),
    );
    elements.previous.disabled = true;
    elements.next.disabled = true;
    elements.pageLabel.textContent = "第 1 页";
  }
}

async function loadCart() {
  try {
    const cart = await requestJson("/api/courses/dblist?status=");
    elements.cartList.replaceChildren();
    if (!Array.isArray(cart) || !cart.length) {
      elements.cartSummary.textContent = "本地清单为空";
      elements.cartList.append(makeElement("div", "empty-state", "选课清单为空"));
      return;
    }
    elements.cartSummary.textContent = `共 ${cart.length} 门课程（只读）`;
    const fragment = document.createDocumentFragment();
    cart.forEach((item) => {
      const row = makeElement("div", "cart-item");
      const copy = makeElement("div");
      copy.append(
        makeElement("strong", "", item.name || item.id || "未命名课程"),
        makeElement("span", "", [item.type, item.campus_name, item.status].filter(Boolean).join(" · ")),
      );
      row.append(copy);
      fragment.append(row);
    });
    elements.cartList.append(fragment);
  } catch (error) {
    elements.cartSummary.textContent = error.message;
    elements.cartList.replaceChildren(makeElement("div", "empty-state", "本地清单暂不可用"));
  }
}

elements.types.addEventListener("click", (event) => {
  const button = event.target.closest("[data-type]");
  if (!button) return;
  document.querySelectorAll("#offlineTypes [data-type]").forEach((item) => {
    item.classList.toggle("is-active", item === button);
  });
  loadCourses(button.dataset.type);
});
elements.search.addEventListener("input", () => {
  state.keyword = elements.search.value.trim();
  if (!state.graduate) state.page = 1;
  renderCourses();
});
elements.previous.addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    if (state.graduate) { loadCourses(state.type, state.page); return; }
    renderCourses();
  }
});
elements.next.addEventListener("click", () => {
  state.page += 1;
  if (state.graduate) { loadCourses(state.type, state.page); return; }
  renderCourses();
});

async function initializeOffline() {
  try {
    const bootstrap = await requestJson("/api/bootstrap");
    state.graduate = bootstrap.program === "graduate";
    if (state.graduate) {
      Object.assign(COURSE_TYPES, { GPN: "方案内课程", GCROSS: "跨专业课程", GALL: "开课查询" });
      state.type = "GPN";
      elements.types.replaceChildren();
      for (const type of ["GPN", "GCROSS", "GALL"]) {
        const button = makeElement("button", `button button-secondary${type === "GPN" ? " is-active" : ""}`, COURSE_TYPES[type]);
        button.type = "button";
        button.dataset.type = type;
        elements.types.append(button);
      }
      elements.search.placeholder = "搜索当前缓存页";
    }
    await loadCourses();
    await loadCart();
  } catch (error) { elements.summary.textContent = error.message; }
}
initializeOffline();
