"use strict";

(() => {
  const STORAGE_KEY = "inspect-dependency-source.dashboard.v1";
  const REFRESH_INTERVAL_MS = 2000;
  const EVENT_API_BATCH_SIZE = 500;
  const MAX_EVENT_CATCH_UP_BATCHES = 8;
  const MAX_EVENT_CATCH_UP_BURST_MS = 500;
  const EVENT_CATCH_UP_PAUSE_MS = 50;
  const MAX_EVENTS_PER_OPERATION = 120;
  const MAX_PENDING_EVENT_OPERATIONS = 16;
  const translations = {
    en: {
      skip: "Skip to content",
      preferences: "Display preferences",
      language: "Language",
      theme: "Theme",
      themeSystem: "System",
      themeLight: "Light",
      themeDark: "Dark",
      eyebrow: "Local source intelligence",
      heroKicker: "Global catalog",
      heroTitle: "Know exactly which source your agents inspect.",
      heroCopy: "Verified refs, reusable source trees, and operation health—all local to this machine.",
      connecting: "Connecting…",
      live: "Live · refreshed {time}",
      retrying: "Connection interrupted · retrying",
      healthKicker: "At a glance",
      healthTitle: "Catalog health",
      reconciled: "Storage reconciled {time}",
      inventoryKicker: "Source inventory",
      inventoryTitle: "Repositories",
      search: "Search repositories",
      searchPlaceholder: "Search name, alias, package…",
      filter: "Filter repositories",
      filterAll: "All sources",
      filterVerified: "Verified exact",
      filterWarning: "Needs attention",
      filterLocal: "Local sources",
      inventoryNav: "Repository inventory",
      noRepositories: "No repositories found",
      noRepositoriesHelp: "Adjust the filter or add a source with the CLI.",
      detailKicker: "Repository details",
      selectRepository: "Select a repository",
      selectRepositoryHelp: "Inspect provenance, cached artifacts, tags, manifests, and source paths.",
      operationsKicker: "Activity",
      operationsTitle: "Operations",
      noOperations: "No operations yet",
      noOperationsHelp: "Fetch and verification activity will appear here.",
      footer: "Private by default · localhost only · read-only",
      connectionError: "Dashboard unavailable",
      connectionErrorHelp: "The next refresh will retry automatically.",
      repositoriesMetric: "Repositories",
      artifactsMetric: "Cached artifacts",
      verifiedMetric: "Verified exact",
      manifestsMetric: "Rich manifests",
      activeMetric: "Active operations",
      failedMetric: "Failed operations",
      warningsMetric: "Integrity warnings",
      staleTagsMetric: "Stale tag indexes",
      storageMetric: "Cache size",
      freeStorage: "{size} free",
      repositoryCount: "{count} repositories",
      operationCount: "{count} operations",
      sourceIdentity: "Source identity",
      cacheState: "Cache state",
      artifacts: "Cached artifacts",
      packageBindings: "Package provenance",
      localSources: "Local source snapshots",
      cachedTags: "Cached tags",
      manifest: "Retrieval manifest",
      provider: "Provider",
      remote: "Sanitized remote",
      aliases: "Aliases",
      sourcePath: "Preferred source",
      exactRef: "Selected ref",
      commit: "Commit",
      verification: "Verification",
      tagRefresh: "Tags refreshed",
      localSnapshot: "Local snapshot",
      branch: "Branch",
      workingTree: "Working tree",
      clean: "Clean",
      dirty: "Dirty",
      operationError: "Operation error",
      errorCode: "Error code",
      noValue: "Not available",
      noArtifacts: "No cached artifacts",
      noBindings: "No package bindings",
      noLocalSources: "No registered local source snapshots",
      noTags: "No cached tags",
      noManifest: "No enriched manifest yet.",
      package: "Package",
      requested: "Requested",
      resolved: "Resolved",
      provenance: "Provenance",
      path: "Path",
      updated: "Updated",
      events: "event timeline",
      noEvents: "No event details recorded.",
      justNow: "just now",
      secondsAgo: "{count}s ago",
      minutesAgo: "{count}m ago",
      hoursAgo: "{count}h ago",
      daysAgo: "{count}d ago",
      unknown: "Unknown",
      local: "Local",
      github: "GitHub",
      git: "Git",
      verified: "Verified",
      exact_commit: "Exact commit",
      exact_tag: "Exact tag",
      heuristic_tag: "Heuristic tag",
      unresolved: "Unresolved",
      running: "Running",
      completed: "Completed",
      failed: "Failed",
      pending: "Pending",
      interrupted: "Interrupted",
      warning: "Needs attention",
      stale: "Stale",
      ready: "Ready"
    },
    "zh-CN": {
      skip: "跳到主要内容",
      preferences: "显示偏好",
      language: "语言",
      theme: "主题",
      themeSystem: "跟随系统",
      themeLight: "浅色",
      themeDark: "深色",
      eyebrow: "本地源码情报",
      heroKicker: "全局目录",
      heroTitle: "准确掌握 Agent 正在分析哪一份源码。",
      heroCopy: "精确版本、可复用源码树与操作健康状态，全部保留在本机。",
      connecting: "正在连接…",
      live: "实时 · {time} 刷新",
      retrying: "连接中断 · 正在重试",
      healthKicker: "状态概览",
      healthTitle: "目录健康状态",
      reconciled: "存储于 {time} 完成核对",
      inventoryKicker: "源码清单",
      inventoryTitle: "代码仓库",
      search: "搜索代码仓库",
      searchPlaceholder: "搜索名称、别名、包…",
      filter: "筛选代码仓库",
      filterAll: "全部源码",
      filterVerified: "精确验证",
      filterWarning: "需要关注",
      filterLocal: "本地源码",
      inventoryNav: "代码仓库清单",
      noRepositories: "没有找到代码仓库",
      noRepositoriesHelp: "请调整筛选条件，或通过 CLI 添加源码。",
      detailKicker: "仓库详情",
      selectRepository: "选择一个代码仓库",
      selectRepositoryHelp: "查看来源依据、缓存构件、标签、清单和源码路径。",
      operationsKicker: "近期活动",
      operationsTitle: "操作记录",
      noOperations: "暂无操作记录",
      noOperationsHelp: "拉取和验证活动会显示在这里。",
      footer: "默认私有 · 仅限本机 · 只读",
      connectionError: "仪表盘暂不可用",
      connectionErrorHelp: "下一次刷新将自动重试。",
      repositoriesMetric: "代码仓库",
      artifactsMetric: "缓存构件",
      verifiedMetric: "精确验证",
      manifestsMetric: "增强清单",
      activeMetric: "进行中操作",
      failedMetric: "失败操作",
      warningsMetric: "完整性警告",
      staleTagsMetric: "过期标签索引",
      storageMetric: "缓存空间",
      freeStorage: "剩余 {size}",
      repositoryCount: "共 {count} 个仓库",
      operationCount: "共 {count} 项操作",
      sourceIdentity: "源码身份",
      cacheState: "缓存状态",
      artifacts: "缓存构件",
      packageBindings: "软件包溯源",
      localSources: "本地源码快照",
      cachedTags: "缓存标签",
      manifest: "检索清单",
      provider: "来源类型",
      remote: "脱敏远程地址",
      aliases: "别名",
      sourcePath: "首选源码",
      exactRef: "选定版本",
      commit: "提交",
      verification: "验证方式",
      tagRefresh: "标签刷新时间",
      localSnapshot: "本地快照",
      branch: "分支",
      workingTree: "工作区",
      clean: "干净",
      dirty: "有修改",
      operationError: "操作错误",
      errorCode: "错误代码",
      noValue: "暂无",
      noArtifacts: "暂无缓存构件",
      noBindings: "暂无软件包绑定",
      noLocalSources: "暂无已注册的本地源码快照",
      noTags: "暂无缓存标签",
      noManifest: "尚未创建增强检索清单。",
      package: "软件包",
      requested: "请求版本",
      resolved: "解析结果",
      provenance: "解析依据",
      path: "路径",
      updated: "更新时间",
      events: "事件时间线",
      noEvents: "暂无详细事件。",
      justNow: "刚刚",
      secondsAgo: "{count} 秒前",
      minutesAgo: "{count} 分钟前",
      hoursAgo: "{count} 小时前",
      daysAgo: "{count} 天前",
      unknown: "未知",
      local: "本地",
      github: "GitHub",
      git: "Git",
      verified: "已验证",
      exact_commit: "精确提交",
      exact_tag: "精确标签",
      heuristic_tag: "推测标签",
      unresolved: "未解析",
      running: "进行中",
      completed: "已完成",
      failed: "失败",
      pending: "等待中",
      interrupted: "已中断",
      warning: "需要关注",
      stale: "已过期",
      ready: "就绪"
    }
  };

  const saved = readSavedState();
  const state = {
    language: saved.language === "zh-CN" ? "zh-CN" : "en",
    theme: ["system", "light", "dark"].includes(saved.theme) ? saved.theme : "system",
    query: typeof saved.query === "string" ? saved.query : "",
    filter: ["all", "verified", "warning", "local"].includes(saved.filter) ? saved.filter : "all",
    selectedRepository: typeof saved.selectedRepository === "string" ? saved.selectedRepository : null,
    expandedOperations: new Set(Array.isArray(saved.expandedOperations) ? saved.expandedOperations.map(String) : []),
    repositories: [],
    operations: [],
    summary: {},
    tags: [],
    repositoryDetail: null,
    eventsByOperation: new Map(),
    eventSequence: 0,
    eventCatchUpInFlight: false,
    eventCatchUpTimer: null,
    refreshInFlight: false,
    refreshQueued: false,
    detailRequest: 0,
    lastRefresh: null
  };

  const nodes = {
    language: document.getElementById("language-select"),
    theme: document.getElementById("theme-select"),
    search: document.getElementById("repository-search"),
    filter: document.getElementById("repository-filter"),
    metrics: document.getElementById("metric-grid"),
    notice: document.getElementById("summary-notice"),
    reconciled: document.getElementById("reconciled-at"),
    repositoryCount: document.getElementById("repository-count"),
    repositoryList: document.getElementById("repository-list"),
    repositoryEmpty: document.getElementById("repository-empty"),
    detail: document.getElementById("repository-detail"),
    operationCount: document.getElementById("operation-count"),
    operationList: document.getElementById("operation-list"),
    operationEmpty: document.getElementById("operation-empty"),
    sync: document.getElementById("sync-state"),
    toast: document.getElementById("error-toast"),
    errorMessage: document.getElementById("error-message")
  };

  function readSavedState() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (_error) {
      return {};
    }
  }

  function saveState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        language: state.language,
        theme: state.theme,
        query: state.query,
        filter: state.filter,
        selectedRepository: state.selectedRepository,
        expandedOperations: [...state.expandedOperations]
      }));
    } catch (_error) {
      // A disabled storage backend must not prevent read-only observation.
    }
  }

  function t(key, values = {}) {
    const source = translations[state.language][key] || translations.en[key] || key;
    return source.replace(/\{(\w+)\}/g, (_match, name) => String(values[name] ?? ""));
  }

  function applyTranslations() {
    document.documentElement.lang = state.language;
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      node.textContent = t(node.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
      node.setAttribute("aria-label", t(node.dataset.i18nAria));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
      node.setAttribute("placeholder", t(node.dataset.i18nPlaceholder));
    });
    document.title = state.language === "zh-CN" ? "依赖源码观测" : "Inspect Dependency Source";
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function valueOf(object, keys, fallback = null) {
    for (const key of keys) {
      if (object && object[key] !== undefined && object[key] !== null && object[key] !== "") return object[key];
    }
    return fallback;
  }

  function repositoryId(repository) {
    return String(valueOf(repository, ["id", "repository_id", "repo_id"], ""));
  }

  function repositoryName(repository) {
    return String(valueOf(repository, ["display_name", "canonical_name", "name", "github_full_name"], repositoryId(repository)));
  }

  function normalizedStatus(value) {
    const status = String(value || "unknown").toLowerCase().replaceAll("_", "-");
    const allowed = new Set(["verified", "ready", "warning", "stale", "failed", "error", "running", "in-progress", "completed", "pending", "interrupted", "heuristic"]);
    return allowed.has(status) ? status : "unknown";
  }

  function displayStatus(value) {
    const key = String(value || "unknown").toLowerCase().replaceAll("-", "_");
    return t(key);
  }

  function formatDate(value) {
    if (!value) return t("noValue");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(state.language, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function relativeTime(value) {
    if (!value) return t("unknown");
    const timestamp = new Date(value).getTime();
    if (!Number.isFinite(timestamp)) return String(value);
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 8) return t("justNow");
    if (seconds < 60) return t("secondsAgo", { count: seconds });
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return t("minutesAgo", { count: minutes });
    const hours = Math.round(minutes / 60);
    if (hours < 24) return t("hoursAgo", { count: hours });
    return t("daysAgo", { count: Math.round(hours / 24) });
  }

  function formatBytes(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const bytes = Number(value);
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let amount = bytes;
    let unit = -1;
    do {
      amount /= 1024;
      unit += 1;
    } while (amount >= 1024 && unit < units.length - 1);
    return `${amount >= 10 ? amount.toFixed(1) : amount.toFixed(2)} ${units[unit]}`;
  }

  function aliasesOf(repository) {
    const aliases = valueOf(repository, ["aliases"], []);
    return Array.isArray(aliases) ? aliases.map(String) : aliases ? [String(aliases)] : [];
  }

  function isVerified(repository) {
    const provenance = String(valueOf(repository, ["resolution_provenance", "provenance", "verification_state"], "")).toLowerCase();
    const verification = String(valueOf(repository, ["verification_state", "preferred_verification_state"], "")).toLowerCase();
    const hasExactProvenance = ["exact_commit", "exact_tag"].includes(provenance);
    return (hasExactProvenance && verification === "verified") || repository.verified === true;
  }

  function hasWarning(repository) {
    const status = String(valueOf(repository, ["status", "health", "verification_state"], "")).toLowerCase();
    return ["warning", "failed", "error", "stale", "unresolved"].includes(status) || Number(repository.integrity_warning_count || 0) > 0;
  }

  function isLocal(repository) {
    if (repository.has_local_source === true) return true;
    if (Number(repository.local_source_count || 0) > 0) return true;
    return Array.isArray(repository.local_sources) && repository.local_sources.length > 0;
  }

  function packageSearchTerms(repository) {
    const projected = Array.isArray(repository.package_search_terms) ? repository.package_search_terms.map(String) : [];
    const packages = Array.isArray(repository.packages) ? repository.packages.map((item) => {
      if (typeof item === "string") return item;
      return `${item.package_id || ""} ${item.version || ""}`.trim();
    }) : [];
    return [...projected, ...packages];
  }

  function repositoryStatus(repository) {
    if (hasWarning(repository)) return String(valueOf(repository, ["status", "health", "verification_state"], "warning"));
    if (isVerified(repository)) return "verified";
    return String(valueOf(repository, ["status", "health"], "ready"));
  }

  function renderMetrics() {
    const summary = state.summary || {};
    const definitions = [
      ["repositoriesMetric", valueOf(summary, ["repository_count"], 0), ""],
      ["artifactsMetric", valueOf(summary, ["artifact_count", "cached_artifact_count"], 0), ""],
      ["verifiedMetric", valueOf(summary, ["verified_exact_count"], 0), "good"],
      ["manifestsMetric", valueOf(summary, ["manifest_ready_count", "enriched_manifest_count"], 0), ""],
      ["activeMetric", valueOf(summary, ["active_operation_count"], 0), Number(summary.active_operation_count) ? "good" : ""],
      ["failedMetric", valueOf(summary, ["failed_operation_count"], 0), Number(summary.failed_operation_count) ? "danger" : ""],
      ["warningsMetric", valueOf(summary, ["integrity_warning_count"], 0), Number(summary.integrity_warning_count) ? "warning" : ""],
      ["staleTagsMetric", valueOf(summary, ["stale_tag_count"], 0), Number(summary.stale_tag_count) ? "warning" : ""],
      ["storageMetric", formatBytes(valueOf(summary, ["disk_used_bytes", "cache_bytes"], 0)), "storage"]
    ];
    const cards = definitions.map(([label, value, tone]) => {
      const card = element("article", `metric-card ${tone}`.trim());
      card.append(element("span", "metric-label", t(label)), element("strong", "metric-value", value));
      if (label === "storageMetric") {
        card.append(element("span", "metric-note", t("freeStorage", { size: formatBytes(summary.disk_free_bytes) })));
      }
      return card;
    });
    nodes.metrics.replaceChildren(...cards);
    nodes.notice.hidden = !summary.notice;
    nodes.notice.textContent = summary.notice || "";
    nodes.reconciled.textContent = summary.reconciled_at ? t("reconciled", { time: relativeTime(summary.reconciled_at) }) : "";
  }

  function filteredRepositories() {
    const query = state.query.trim().toLocaleLowerCase(state.language);
    return state.repositories.filter((repository) => {
      if (state.filter === "verified" && !isVerified(repository)) return false;
      if (state.filter === "warning" && !hasWarning(repository)) return false;
      if (state.filter === "local" && !isLocal(repository)) return false;
      if (!query) return true;
      const searchable = [
        repositoryName(repository),
        ...aliasesOf(repository),
        valueOf(repository, ["github_full_name", "remote_url", "sanitized_remote"], ""),
        ...packageSearchTerms(repository)
      ].join(" ").toLocaleLowerCase(state.language);
      return searchable.includes(query);
    });
  }

  function renderRepositories() {
    const repositories = filteredRepositories();
    nodes.repositoryCount.textContent = String(repositories.length);
    nodes.repositoryCount.setAttribute("aria-label", t("repositoryCount", { count: repositories.length }));
    nodes.repositoryEmpty.hidden = repositories.length > 0;
    const items = repositories.map((repository) => {
      const id = repositoryId(repository);
      const name = repositoryName(repository);
      const item = element("li");
      const button = element("button", "repository-button");
      button.type = "button";
      button.dataset.repositoryId = id;
      button.setAttribute("aria-current", String(id === state.selectedRepository));
      const initials = name.split(/[\s/._-]+/).filter(Boolean).slice(-2).map((part) => part[0]).join("").toUpperCase().slice(0, 2) || "<>";
      const avatar = element("span", "repo-avatar", initials);
      avatar.setAttribute("aria-hidden", "true");
      const copy = element("span", "repo-copy");
      copy.append(
        element("span", "repo-name", name),
        element("span", "repo-meta", [
          displayStatus(repositoryStatus(repository)),
          valueOf(repository, ["selected_ref", "ref", "version"], "")
        ].filter(Boolean).join(" · "))
      );
      const status = element("span", `status-dot ${normalizedStatus(repositoryStatus(repository))}`);
      status.setAttribute("aria-label", displayStatus(repositoryStatus(repository)));
      button.append(avatar, copy, status);
      button.addEventListener("click", () => selectRepository(id));
      item.append(button);
      return item;
    });
    nodes.repositoryList.replaceChildren(...items);
  }

  function factRow(label, value, monospace = false) {
    const wrapper = element("div", "fact-row");
    wrapper.append(element("dt", "", label), element("dd", monospace ? "monospace" : "", value ?? t("noValue")));
    return wrapper;
  }

  function detailCard(title, full = false) {
    const card = element("section", `detail-card${full ? " full" : ""}`);
    card.append(element("h3", "", title));
    return card;
  }

  function renderRepositoryDetail() {
    const repository = state.repositoryDetail;
    if (!repository) {
      const placeholder = element("div", "detail-placeholder");
      const orbit = element("div", "placeholder-orbit");
      orbit.setAttribute("aria-hidden", "true");
      orbit.append(element("span"));
      const kicker = element("p", "section-kicker", t("detailKicker"));
      const heading = element("h2", "", t("selectRepository"));
      heading.id = "detail-heading";
      placeholder.append(orbit, kicker, heading, element("p", "", t("selectRepositoryHelp")));
      nodes.detail.replaceChildren(placeholder);
      return;
    }

    const name = repositoryName(repository);
    const header = element("header", "detail-header");
    const title = element("div", "detail-title");
    title.append(element("p", "section-kicker", t("detailKicker")));
    const heading = element("h2", "", name);
    heading.id = "detail-heading";
    title.append(heading, element("p", "", valueOf(repository, ["summary", "description", "sanitized_remote", "remote_url"], t("noValue"))));
    const statusValue = repositoryStatus(repository);
    const status = element("span", `chip ${normalizedStatus(statusValue)}`, displayStatus(statusValue));
    header.append(title, status);

    const grid = element("div", "detail-grid");
    const identity = detailCard(t("sourceIdentity"));
    const identityFacts = element("dl", "fact-list");
    identityFacts.append(
      factRow(t("provider"), displayStatus(String(valueOf(repository, ["provider", "source_type", "kind"], "unknown")).toLowerCase())),
      factRow(t("remote"), valueOf(repository, ["sanitized_remote", "remote_url", "github_full_name"], t("noValue")), true),
      factRow(t("aliases"), aliasesOf(repository).join(", ") || t("noValue"))
    );
    identity.append(identityFacts);

    const localSources = Array.isArray(repository.local_sources) ? repository.local_sources : [];
    const latestLocalSource = localSources[0] || null;
    const cache = detailCard(t("cacheState"));
    const cacheFacts = element("dl", "fact-list");
    cacheFacts.append(
      factRow(t("sourcePath"), valueOf(repository, ["preferred_source_path", "source_path", "local_path"], t("noValue")), true),
      factRow(t("exactRef"), valueOf(repository, ["selected_ref", "exact_ref", "ref"], t("noValue")), true),
      factRow(t("commit"), valueOf(repository, ["commit", "commit_sha", "resolved_commit"], t("noValue")), true),
      factRow(t("provenance"), displayStatus(valueOf(repository, ["resolution_provenance", "provenance"], "unknown"))),
      factRow(t("verification"), displayStatus(valueOf(repository, ["verification_state", "preferred_verification_state"], "unknown"))),
      factRow(t("tagRefresh"), formatDate(valueOf(repository, ["tags_refreshed_at"], null))),
      factRow(t("localSnapshot"), formatDate(valueOf(latestLocalSource, ["verified_at", "added_at"], null)))
    );
    cache.append(cacheFacts);
    grid.append(identity, cache);

    const artifacts = Array.isArray(repository.artifacts) ? repository.artifacts : [];
    const artifactCard = detailCard(t("artifacts"), true);
    if (!artifacts.length) {
      artifactCard.append(element("p", "manifest-copy", t("noArtifacts")));
    } else {
      const list = element("ul", "artifact-list");
      artifacts.forEach((artifact) => {
        const item = element("li", "artifact");
        const copy = element("div");
        copy.append(
          element("strong", "", valueOf(artifact, ["ref", "selected_ref", "name", "artifact_id"], t("unknown"))),
          element("small", "monospace", valueOf(artifact, ["source_path", "path", "commit_sha", "commit"], t("noValue")))
        );
        const artifactStatus = valueOf(artifact, ["verification_state", "status", "provenance"], "ready");
        copy.setAttribute("title", valueOf(artifact, ["artifact_id", "id"], ""));
        item.append(copy, element("span", `chip ${normalizedStatus(artifactStatus)}`, displayStatus(artifactStatus)));
        list.append(item);
      });
      artifactCard.append(list);
    }
    grid.append(artifactCard);

    const bindings = Array.isArray(repository.package_bindings) ? repository.package_bindings : [];
    const bindingCard = detailCard(t("packageBindings"), true);
    if (!bindings.length) {
      bindingCard.append(element("p", "manifest-copy", t("noBindings")));
    } else {
      const list = element("ul", "binding-list");
      bindings.forEach((binding) => {
        const item = element("li", "artifact");
        const copy = element("div");
        copy.append(
          element("strong", "", `${valueOf(binding, ["package_id", "name"], t("package"))} · ${valueOf(binding, ["requested_version", "version"], t("noValue"))}`),
          element("small", "", `${t("resolved")}: ${valueOf(binding, ["resolved_ref", "ref", "commit"], t("noValue"))}`)
        );
        const provenance = valueOf(binding, ["provenance", "resolution_provenance", "resolution_kind"], "unresolved");
        item.append(copy, element("span", `chip ${normalizedStatus(provenance)}`, displayStatus(provenance)));
        list.append(item);
      });
      bindingCard.append(list);
    }
    grid.append(bindingCard);

    const localCard = detailCard(t("localSources"), true);
    if (!localSources.length) {
      localCard.append(element("p", "manifest-copy", t("noLocalSources")));
    } else {
      const list = element("ul", "artifact-list local-source-list");
      localSources.forEach((source) => {
        const item = element("li", "artifact local-source");
        const copy = element("div");
        const branch = valueOf(source, ["branch"], t("noValue"));
        const commit = valueOf(source, ["commit_sha", "commit"], t("noValue"));
        copy.append(
          element("strong", "monospace", valueOf(source, ["canonical_path", "path"], t("noValue"))),
          element("small", "", `${t("branch")}: ${branch} · ${t("commit")}: ${commit}`),
          element("small", "", `${t("updated")}: ${formatDate(valueOf(source, ["verified_at", "added_at"], null))}`)
        );
        const dirty = source.dirty;
        const workingTree = dirty === true ? t("dirty") : dirty === false ? t("clean") : t("unknown");
        const tone = dirty === true ? "warning" : dirty === false ? "verified" : "ready";
        const chip = element("span", `chip ${tone}`, workingTree);
        chip.setAttribute("aria-label", `${t("workingTree")}: ${workingTree}`);
        item.append(copy, chip);
        list.append(item);
      });
      localCard.append(list);
    }
    grid.append(localCard);

    const tagCard = detailCard(t("cachedTags"), true);
    const tagCloud = element("div", "tag-cloud");
    if (!state.tags.length) {
      tagCard.append(element("p", "manifest-copy", t("noTags")));
    } else {
      state.tags.slice(0, 80).forEach((tag) => {
        const tagName = typeof tag === "string" ? tag : valueOf(tag, ["name", "tag", "ref"], t("unknown"));
        tagCloud.append(element("span", "tag", tagName));
      });
      tagCard.append(tagCloud);
    }
    grid.append(tagCard);

    const manifestCard = detailCard(t("manifest"), true);
    const manifest = valueOf(repository, ["manifest", "enriched_manifest"], null);
    const manifestText = typeof manifest === "string" ? manifest : manifest ? JSON.stringify(manifest, null, 2) : t("noManifest");
    manifestCard.append(element("pre", "manifest-copy", manifestText));
    grid.append(manifestCard);

    nodes.detail.replaceChildren(header, grid);
  }

  function operationId(operation, index) {
    return String(valueOf(operation, ["id", "operation_id"], `operation-${index}`));
  }

  function eventsFor(operation, id) {
    const embedded = Array.isArray(operation.events) ? operation.events : [];
    const incremental = state.eventsByOperation.get(id) || [];
    const combined = [...embedded, ...incremental];
    const seen = new Set();
    return combined.filter((event, index) => {
      const key = String(valueOf(event, ["sequence", "id"], `${id}-${index}-${event.timestamp || ""}-${event.message || ""}`));
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0)).slice(-MAX_EVENTS_PER_OPERATION);
  }

  function renderOperations() {
    nodes.operationCount.textContent = String(state.operations.length);
    nodes.operationCount.setAttribute("aria-label", t("operationCount", { count: state.operations.length }));
    nodes.operationList.dataset.eventSequence = String(state.eventSequence);
    nodes.operationList.dataset.retainedEventCount = String(
      [...state.eventsByOperation.values()].reduce((total, events) => total + events.length, 0)
    );
    nodes.operationEmpty.hidden = state.operations.length > 0;
    const operationNodes = state.operations.map((operation, index) => {
      const id = operationId(operation, index);
      const details = element("details", "operation-item");
      details.dataset.operationId = id;
      details.open = state.expandedOperations.has(id);
      details.addEventListener("toggle", () => {
        if (details.open) state.expandedOperations.add(id);
        else state.expandedOperations.delete(id);
        saveState();
      });
      const summary = element("summary", "operation-summary");
      const statusValue = valueOf(operation, ["status", "state"], "unknown");
      const dot = element("span", `status-dot ${normalizedStatus(statusValue)}`);
      dot.setAttribute("aria-hidden", "true");
      const primary = element("span", "operation-primary");
      const kind = valueOf(operation, ["kind", "type", "command"], t("unknown"));
      const target = valueOf(operation, ["repository_name", "repository", "target", "ref"], "");
      primary.append(
        element("strong", "", [kind, target].filter(Boolean).join(" · ")),
        element("span", "", valueOf(operation, ["message", "phase", "reason"], displayStatus(statusValue)))
      );
      const chip = element("span", `chip ${normalizedStatus(statusValue)}`, displayStatus(statusValue));
      const time = element("time", "operation-time", relativeTime(valueOf(operation, ["updated_at", "finished_at", "started_at", "created_at"], null)));
      summary.setAttribute("aria-label", `${kind} ${target} · ${displayStatus(statusValue)} · ${t("events")}`);
      summary.append(dot, primary, chip, time);
      details.append(summary);

      const errorCode = valueOf(operation, ["error_code"], null);
      const errorMessage = valueOf(operation, ["error_message"], null);
      if (errorCode || errorMessage) {
        const errorPanel = element("div", "operation-error");
        errorPanel.append(
          element("strong", "", t("operationError")),
          element("span", "monospace", `${t("errorCode")}: ${errorCode || t("unknown")}`),
          element("p", "", errorMessage || t("noValue"))
        );
        details.append(errorPanel);
      }

      const events = eventsFor(operation, id);
      if (events.length) {
        const timeline = element("ol", "timeline");
        events.forEach((event) => {
          const item = element("li", "timeline-event");
          item.append(element("p", "timeline-message", valueOf(event, ["message", "phase", "event_type"], t("unknown"))));
          const meta = element("div", "timeline-meta");
          const sequence = valueOf(event, ["sequence"], null);
          if (sequence !== null) meta.append(element("span", "monospace", `#${sequence}`));
          meta.append(element("time", "", formatDate(valueOf(event, ["timestamp", "created_at"], null))));
          const progress = valueOf(event, ["progress", "percent"], null);
          if (progress !== null) meta.append(element("span", "", `${progress}%`));
          item.append(meta);
          timeline.append(item);
        });
        details.append(timeline);
      } else {
        details.append(element("p", "timeline timeline-message", t("noEvents")));
      }
      return details;
    });
    nodes.operationList.replaceChildren(...operationNodes);
  }

  async function fetchJson(path) {
    const response = await fetch(path, { method: "GET", cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload?.error?.message || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  function ingestEvents(events) {
    events.forEach((event) => {
      const sequence = Number(event.sequence || 0);
      if (sequence > state.eventSequence) state.eventSequence = sequence;
      const id = String(valueOf(event, ["operation_id", "operation"], ""));
      if (!id) return;
      const existing = state.eventsByOperation.get(id) || [];
      if (!existing.some((item) => Number(item.sequence || -1) === sequence)) existing.push(event);
      state.eventsByOperation.set(id, existing);
    });
    pruneEventState();
  }

  function pruneEventState() {
    const visibleOperationIds = new Set(state.operations.map(operationId));
    const visibleEntries = [];
    const pendingEntries = [];
    state.eventsByOperation.forEach((events, id) => {
      const retained = [...events]
        .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0))
        .slice(-MAX_EVENTS_PER_OPERATION);
      if (visibleOperationIds.has(id)) {
        visibleEntries.push([id, retained]);
      } else {
        const latestSequence = retained.reduce(
          (maximum, event) => Math.max(maximum, Number(event.sequence || 0)),
          0
        );
        pendingEntries.push([id, retained, latestSequence]);
      }
    });
    pendingEntries.sort((left, right) => right[2] - left[2]);
    state.eventsByOperation = new Map([
      ...visibleEntries,
      ...pendingEntries.slice(0, MAX_PENDING_EVENT_OPERATIONS).map(([id, events]) => [id, events])
    ]);

    const visibleIds = new Set(state.operations.map(operationId));
    let expandedChanged = false;
    state.expandedOperations.forEach((id) => {
      if (!visibleIds.has(id)) {
        state.expandedOperations.delete(id);
        expandedChanged = true;
      }
    });
    if (expandedChanged) saveState();
  }

  function applyEventPayload(payload) {
    const events = Array.isArray(payload?.events) ? payload.events : [];
    const previousSequence = state.eventSequence;
    ingestEvents(events);
    state.eventSequence = Math.max(state.eventSequence, Number(payload?.last_sequence || 0));
    return {
      fullBatch: events.length >= EVENT_API_BATCH_SIZE,
      advanced: state.eventSequence > previousSequence
    };
  }

  function scheduleEventCatchUp(delay = 0) {
    if (state.eventCatchUpInFlight || state.eventCatchUpTimer !== null) return;
    state.eventCatchUpTimer = window.setTimeout(() => {
      state.eventCatchUpTimer = null;
      void catchUpEvents();
    }, delay);
  }

  async function catchUpEvents() {
    if (state.eventCatchUpInFlight) return;
    state.eventCatchUpInFlight = true;
    const startedAt = performance.now();
    let fullBatchRemains = false;
    try {
      for (let batch = 0; batch < MAX_EVENT_CATCH_UP_BATCHES; batch += 1) {
        const payload = await fetchJson(`/api/v1/events?after_sequence=${state.eventSequence}`);
        const result = applyEventPayload(payload);
        fullBatchRemains = result.fullBatch && result.advanced;
        if (!fullBatchRemains || performance.now() - startedAt >= MAX_EVENT_CATCH_UP_BURST_MS) break;
      }
      renderOperations();
    } catch (error) {
      fullBatchRemains = false;
      showError(error);
    } finally {
      state.eventCatchUpInFlight = false;
      if (fullBatchRemains) scheduleEventCatchUp(EVENT_CATCH_UP_PAUSE_MS);
    }
  }

  async function loadRepository(id) {
    if (!id) {
      state.repositoryDetail = null;
      state.tags = [];
      renderRepositoryDetail();
      return true;
    }
    const requestNumber = ++state.detailRequest;
    const encoded = encodeURIComponent(id);
    try {
      const [detailPayload, tagsPayload] = await Promise.all([
        fetchJson(`/api/v1/repositories/${encoded}`),
        fetchJson(`/api/v1/repositories/${encoded}/tags`)
      ]);
      if (requestNumber !== state.detailRequest || state.selectedRepository !== id) return true;
      state.repositoryDetail = detailPayload.repository || null;
      state.tags = Array.isArray(tagsPayload.tags) ? tagsPayload.tags : [];
      renderRepositoryDetail();
      return true;
    } catch (error) {
      if (requestNumber !== state.detailRequest || state.selectedRepository !== id) return true;
      state.repositoryDetail = null;
      state.tags = [];
      renderRepositoryDetail();
      showError(error);
      return false;
    }
  }

  function selectRepository(id) {
    if (!id || state.selectedRepository === id) return;
    state.selectedRepository = id;
    state.repositoryDetail = null;
    state.tags = [];
    saveState();
    renderRepositories();
    renderRepositoryDetail();
    void loadRepository(id);
  }

  async function refresh() {
    if (state.refreshInFlight) {
      state.refreshQueued = true;
      return;
    }
    state.refreshInFlight = true;
    try {
      const eventsAlreadyLoading = state.eventCatchUpInFlight || state.eventCatchUpTimer !== null;
      const [summaryPayload, repositoriesPayload, operationsPayload, eventsPayload] = await Promise.all([
        fetchJson("/api/v1/summary"),
        fetchJson("/api/v1/repositories"),
        fetchJson("/api/v1/operations"),
        eventsAlreadyLoading
          ? Promise.resolve(null)
          : fetchJson(`/api/v1/events?after_sequence=${state.eventSequence}`)
      ]);
      state.summary = summaryPayload.summary || {};
      state.repositories = Array.isArray(repositoriesPayload.repositories) ? repositoriesPayload.repositories : [];
      state.operations = Array.isArray(operationsPayload.operations) ? operationsPayload.operations : [];
      pruneEventState();
      const eventResult = eventsPayload ? applyEventPayload(eventsPayload) : null;

      const knownIds = new Set(state.repositories.map(repositoryId));
      if (!state.selectedRepository || !knownIds.has(state.selectedRepository)) {
        state.selectedRepository = state.repositories.length ? repositoryId(state.repositories[0]) : null;
        state.repositoryDetail = null;
        state.tags = [];
        saveState();
      }
      renderMetrics();
      renderRepositories();
      renderOperations();
      let detailLoaded = true;
      if (state.selectedRepository) detailLoaded = await loadRepository(state.selectedRepository);
      else renderRepositoryDetail();
      state.lastRefresh = new Date();
      nodes.sync.className = "sync-state online";
      nodes.sync.lastElementChild.textContent = t("live", { time: new Intl.DateTimeFormat(state.language, { timeStyle: "medium" }).format(state.lastRefresh) });
      if (detailLoaded) nodes.toast.hidden = true;
      if (eventResult?.fullBatch && eventResult.advanced) scheduleEventCatchUp();
    } catch (error) {
      nodes.sync.className = "sync-state error";
      nodes.sync.lastElementChild.textContent = t("retrying");
      showError(error);
    } finally {
      state.refreshInFlight = false;
      if (state.refreshQueued) {
        state.refreshQueued = false;
        queueMicrotask(refresh);
      }
    }
  }

  function showError(error) {
    nodes.errorMessage.textContent = error instanceof Error ? error.message : t("connectionErrorHelp");
    nodes.toast.hidden = false;
  }

  function rerenderLocalizedViews() {
    applyTranslations();
    renderMetrics();
    renderRepositories();
    renderRepositoryDetail();
    renderOperations();
    if (state.lastRefresh) {
      nodes.sync.lastElementChild.textContent = t("live", { time: new Intl.DateTimeFormat(state.language, { timeStyle: "medium" }).format(state.lastRefresh) });
    }
  }

  function initialize() {
    nodes.language.value = state.language;
    nodes.theme.value = state.theme;
    nodes.search.value = state.query;
    nodes.filter.value = state.filter;
    document.documentElement.dataset.theme = state.theme;
    applyTranslations();
    renderMetrics();
    renderRepositories();
    renderRepositoryDetail();
    renderOperations();

    nodes.language.addEventListener("change", () => {
      state.language = nodes.language.value === "zh-CN" ? "zh-CN" : "en";
      saveState();
      rerenderLocalizedViews();
    });
    nodes.theme.addEventListener("change", () => {
      state.theme = ["system", "light", "dark"].includes(nodes.theme.value) ? nodes.theme.value : "system";
      document.documentElement.dataset.theme = state.theme;
      saveState();
    });
    nodes.search.addEventListener("input", () => {
      state.query = nodes.search.value;
      saveState();
      renderRepositories();
    });
    nodes.filter.addEventListener("change", () => {
      state.filter = nodes.filter.value;
      saveState();
      renderRepositories();
    });

    void refresh();
    window.setInterval(refresh, REFRESH_INTERVAL_MS);
  }

  initialize();
})();
