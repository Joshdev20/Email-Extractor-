(() => {
  "use strict";

  const PREF_KEY = "leadscope_prefs_v1";
  const HISTORY_KEY = "leadscope_history_v1";
  const MAX_HISTORY = 8;
  const PAGE_STEP = 50;
  const Exporter = window.LeadScopeExport;

  const CATEGORY_META = {
    general: { label: "General", cls: "cat-general" },
    sales: { label: "Sales", cls: "cat-sales" },
    marketing: { label: "Marketing", cls: "cat-marketing" },
    support: { label: "Support", cls: "cat-support" },
    careers: { label: "Careers", cls: "cat-careers" },
    press: { label: "Press", cls: "cat-press" },
    owner: { label: "Business owner", cls: "cat-owner" },
    other: { label: "Other", cls: "cat-other" }
  };

  const METHOD_LABELS = {
    text: "Page text",
    mailto: "Mailto link",
    attribute: "HTML attribute",
    obfuscated: "Obfuscated",
    spaced: "Obfuscated",
    jsonld: "Structured data"
  };

  const TOAST_ICONS = {
    success: "fa-circle-check",
    error: "fa-circle-exclamation",
    info: "fa-circle-info",
    warning: "fa-triangle-exclamation"
  };

  const state = {
    mode: "nl",
    jobId: null,
    eventSource: null,
    pollTimer: null,
    syncAbort: null,
    jobFinished: false,
    busy: false,
    results: [],
    filtered: [],
    selected: new Set(),
    stats: emptyStats(),
    parsed: null,
    websites: [],
    visibleCount: PAGE_STEP,
    history: [],
    config: null,
    currentLabel: ""
  };

  const el = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    loadPreferences();
    loadHistory();
    bindEvents();
    renderHistory();
    updateDomainCount();
    updateRangeLabels();
    setMode(state.mode || "nl", false);
    loadConfig();
  }

  function cacheElements() {
    const ids = [
      "mobileMenuButton", "mobileMenu",
      "panelNl", "panelDomains", "panelSingle",
      "queryInput", "domainsInput", "fileInput", "domainCount", "urlInput",
      "exampleChips", "planPanel", "startButton", "startButtonLabel", "previewButton",
      "industryInput", "countrySelect", "cityInput", "categorySelect",
      "targetEmails", "targetWebsites", "concurrencyRange", "concurrencyValue",
      "maxPagesRange", "maxPagesValue", "usePlacesToggle",
      "progressSection", "progressStateBadge", "progressPercent", "cancelButton",
      "progressMessage", "progressBar", "progressBarWrap", "progressCurrentDomain",
      "progressDiscovered", "progressCrawled", "progressUnique", "progressDuplicates",
      "progressWebsites", "progressIssues",
      "results", "resultsCountLabel", "clearResultsButton",
      "statDiscovered", "statCrawled", "statFailed", "statEmails", "statUnique", "statDuplicates",
      "resultSearch", "categoryFilter", "domainFilter", "sortSelect",
      "selectAll", "selectedCount", "copySelectedButton", "copyAllButton",
      "exportWrap", "exportButton", "exportMenu", "selectionHint",
      "resultsTableBody", "resultsTableWrap", "resultsCards", "resultsEmpty", "loadMoreButton",
      "historyList", "historyEmpty", "clearHistoryButton",
      "toastContainer"
    ];
    ids.forEach((id) => {
      el[id] = document.getElementById(id);
    });
    el.modeTabs = Array.from(document.querySelectorAll(".mode-tab"));
  }

  function emptyStats() {
    return {
      websites_discovered: 0,
      websites_crawled: 0,
      websites_failed: 0,
      emails_discovered: 0,
      unique_emails: 0,
      duplicates_removed: 0,
      pages_crawled: 0,
      pages_failed: 0,
      target_emails: 0,
      target_websites: 0,
      target_reached: false
    };
  }

  function bindEvents() {
    el.modeTabs.forEach((tab) => {
      tab.addEventListener("click", () => setMode(tab.dataset.mode));
    });

    el.exampleChips.addEventListener("click", (event) => {
      const chip = event.target.closest(".example-chip");
      if (!chip) return;
      el.queryInput.value = chip.dataset.example || "";
      setMode("nl");
      el.queryInput.focus();
      savePreferences();
    });

    el.mobileMenuButton.addEventListener("click", () => {
      const isHidden = el.mobileMenu.classList.toggle("hidden");
      el.mobileMenuButton.setAttribute("aria-expanded", String(!isHidden));
    });
    el.mobileMenu.addEventListener("click", (event) => {
      if (event.target.closest("a")) {
        el.mobileMenu.classList.add("hidden");
        el.mobileMenuButton.setAttribute("aria-expanded", "false");
      }
    });

    el.domainsInput.addEventListener("input", updateDomainCount);
    el.fileInput.addEventListener("change", handleFileUpload);

    el.startButton.addEventListener("click", startExtraction);
    el.previewButton.addEventListener("click", previewPlan);
    el.cancelButton.addEventListener("click", cancelExtraction);

    ["industryInput", "countrySelect", "cityInput", "categorySelect", "targetEmails", "targetWebsites", "usePlacesToggle"]
      .forEach((id) => el[id].addEventListener("change", savePreferences));
    el.concurrencyRange.addEventListener("input", () => {
      updateRangeLabels();
      savePreferences();
    });
    el.maxPagesRange.addEventListener("input", () => {
      updateRangeLabels();
      savePreferences();
    });

    const debouncedFilter = debounce(applyFilters, 140);
    el.resultSearch.addEventListener("input", debouncedFilter);
    el.categoryFilter.addEventListener("change", applyFilters);
    el.domainFilter.addEventListener("change", applyFilters);
    el.sortSelect.addEventListener("change", applyFilters);

    el.selectAll.addEventListener("change", toggleSelectAll);
    el.copySelectedButton.addEventListener("click", copySelected);
    el.copyAllButton.addEventListener("click", copyAll);
    el.exportButton.addEventListener("click", toggleExportMenu);
    el.exportMenu.addEventListener("click", (event) => {
      const button = event.target.closest("[data-format]");
      if (!button) return;
      closeExportMenu();
      exportRows(button.dataset.format);
    });
    el.clearResultsButton.addEventListener("click", clearResults);

    el.resultsTableBody.addEventListener("change", handleResultChange);
    el.resultsCards.addEventListener("change", handleResultChange);
    el.resultsTableBody.addEventListener("click", handleResultClick);
    el.resultsCards.addEventListener("click", handleResultClick);

    el.loadMoreButton.addEventListener("click", () => {
      state.visibleCount += PAGE_STEP;
      renderRows();
    });

    el.historyList.addEventListener("click", (event) => {
      const restore = event.target.closest("[data-history-restore]");
      if (restore) {
        restoreHistory(restore.dataset.historyRestore);
        return;
      }
      const remove = event.target.closest("[data-history-delete]");
      if (remove) deleteHistory(remove.dataset.historyDelete);
    });
    el.clearHistoryButton.addEventListener("click", clearHistory);

    el.planPanel.addEventListener("click", (event) => {
      if (event.target.closest("[data-action='close-plan']")) {
        el.planPanel.classList.add("hidden");
      }
    });

    document.addEventListener("click", (event) => {
      if (!el.exportWrap.contains(event.target)) closeExportMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeExportMenu();
        el.planPanel.classList.add("hidden");
        el.mobileMenu.classList.add("hidden");
        el.mobileMenuButton.setAttribute("aria-expanded", "false");
      }
    });
  }

  function setMode(mode, persist = true) {
    state.mode = mode || "nl";
    el.modeTabs.forEach((tab) => {
      const active = tab.dataset.mode === state.mode;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    el.panelNl.classList.toggle("hidden", state.mode !== "nl");
    el.panelDomains.classList.toggle("hidden", state.mode !== "domains");
    el.panelSingle.classList.toggle("hidden", state.mode !== "single");
    if (persist) savePreferences();
  }

  function updateRangeLabels() {
    el.concurrencyValue.textContent = el.concurrencyRange.value;
    el.maxPagesValue.textContent = el.maxPagesRange.value;
  }

  function updateDomainCount() {
    const parsed = parseDomainLines(el.domainsInput.value);
    const total = parsed.domains.length + parsed.urls.length;
    el.domainCount.textContent = `${total} website${total === 1 ? "" : "s"} queued`;
  }

  function parseDomainLines(text) {
    const domains = [];
    const urls = [];
    const seen = new Set();
    String(text || "")
      .split(/[\n,;]+/)
      .map((item) => item.replace(/^\s*[-*]\s*/, "").trim())
      .filter((item) => item && !item.startsWith("#"))
      .forEach((item) => {
        const isUrl = /^https?:\/\//i.test(item);
        const value = isUrl ? item : item.replace(/^\/+/, "");
        const key = value.toLowerCase().replace(/^www\./, "").replace(/\/.*$/, "");
        if (seen.has(key)) return;
        seen.add(key);
        if (isUrl) urls.push(value);
        else domains.push(value);
      });
    return { domains, urls };
  }

  async function handleFileUpload(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    if (file.size > 1024 * 1024) {
      toast("File too large", "Upload a TXT file smaller than 1 MB.", "warning");
      return;
    }
    try {
      const text = await file.text();
      const current = el.domainsInput.value.trim();
      el.domainsInput.value = current ? `${current}\n${text.trim()}` : text.trim();
      updateDomainCount();
      setMode("domains");
      toast("File loaded", `${file.name} was added to the domain list.`, "success");
    } catch {
      toast("Could not read file", "Please upload a plain text file.", "error");
    } finally {
      event.target.value = "";
    }
  }

  function readSettings() {
    const numberOrNull = (value) => {
      if (value === null || String(value).trim() === "") return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    return {
      target_emails: numberOrNull(el.targetEmails.value),
      target_websites: numberOrNull(el.targetWebsites.value),
      country: el.countrySelect.value || null,
      industry: el.industryInput.value.trim() || null,
      city: el.cityInput.value.trim() || null,
      email_category: el.categorySelect.value || null,
      concurrency: Number(el.concurrencyRange.value),
      max_pages_per_domain: Number(el.maxPagesRange.value),
      timeout: 15.0,
      use_places: el.usePlacesToggle.checked,
      respect_robots: true
    };
  }

  function collectInput() {
    const settings = readSettings();
    if (state.mode === "domains") {
      const parsed = parseDomainLines(el.domainsInput.value);
      if (!parsed.domains.length && !parsed.urls.length) {
        throw new Error("Add at least one domain or URL.");
      }
      const total = parsed.domains.length + parsed.urls.length;
      return {
        endpoint: "/api/bulk-extract",
        payload: { domains: parsed.domains, urls: parsed.urls, settings },
        label: `${total} website${total === 1 ? "" : "s"}`
      };
    }
    if (state.mode === "single") {
      const url = el.urlInput.value.trim();
      if (!url) throw new Error("Enter a website URL.");
      return {
        endpoint: "/api/extract",
        payload: { urls: [url], settings },
        label: url
      };
    }
    const query = el.queryInput.value.trim();
    if (query.length < 3) {
      throw new Error("Describe what you are looking for in at least 3 characters.");
    }
    return {
      endpoint: "/api/extract",
      payload: { query, settings },
      label: query
    };
  }

  async function startExtraction() {
    if (state.busy) return;
    let input;
    try {
      input = collectInput();
    } catch (error) {
      toast("Check your input", error.message, "warning");
      return;
    }

    state.jobFinished = false;
    state.jobId = null;
    state.results = [];
    state.filtered = [];
    state.selected.clear();
    state.stats = emptyStats();
    state.websites = [];
    state.parsed = null;
    state.currentLabel = input.label;
    state.visibleCount = PAGE_STEP;
    resetProgress();
    showProgress();
    setBusy(true);

    if (!state.config) await loadConfig();
    if (state.config && state.config.sync_extraction) {
      await runSyncExtraction(input);
      return;
    }

    try {
      const data = await api(input.endpoint, {
        method: "POST",
        body: JSON.stringify(input.payload)
      });
      state.jobId = data.job_id;
      state.parsed = data.parsed || null;
      if (Array.isArray(data.warnings) && data.warnings.length) {
        renderProgressIssues(data.warnings, []);
      }
      connectProgress(data.job_id);
      toast("Extraction started", "Discovering websites and crawling public contact pages.", "info");
    } catch (error) {
      setBusy(false);
      showProgressError(error.message);
      toast("Could not start extraction", error.message, "error");
    }
  }

  async function runSyncExtraction(input) {
    if (state.mode === "single") {
      return runSingleSyncExtraction(input);
    }
    return runBatchedSyncExtraction(input);
  }

  async function runSingleSyncExtraction(input) {
    const controller = new AbortController();
    state.syncAbort = controller;
    let synthetic = 8;
    el.progressMessage.textContent = "Running extraction";
    el.progressCurrentDomain.textContent = "This deployment processes the job inside one request";
    const timer = window.setInterval(() => {
      synthetic = Math.min(92, synthetic + 3);
      el.progressBar.style.width = `${synthetic}%`;
      el.progressBarWrap.setAttribute("aria-valuenow", String(synthetic));
      el.progressPercent.textContent = `${synthetic}%`;
    }, 1500);

    try {
      const data = await api("/api/extract-sync", {
        method: "POST",
        body: JSON.stringify(input.payload),
        signal: controller.signal
      });
      window.clearInterval(timer);
      state.jobId = data.job_id || null;
      state.parsed = data.parsed || null;
      state.stats = data.stats || emptyStats();
      state.websites = data.websites || [];
      state.results = Array.isArray(data.results) ? data.results : [];
      state.selected.clear();
      state.visibleCount = PAGE_STEP;
      el.progressBar.style.width = "100%";
      el.progressBarWrap.setAttribute("aria-valuenow", "100");
      el.progressPercent.textContent = "100%";
      el.progressMessage.textContent = data.message || "Extraction complete";
      el.progressCurrentDomain.textContent = `${formatNumber(state.stats.unique_emails)} unique email(s) found`;
      setProgressState(data.partial ? "partial" : (data.state || "done"));
      renderProgressStats(state.stats);
      renderProgressWebsites(state.websites);
      renderProgressIssues(data.warnings || [], data.errors || []);
      setBusy(false);
      renderResults();

      if (state.results.length) {
        el.results.classList.remove("hidden");
        el.results.scrollIntoView({ behavior: "smooth", block: "start" });
      }

      if (data.state === "done") {
        addHistory();
        if (data.partial) {
          toast("Partial results", data.message || "The serverless time limit was reached.", "warning");
        } else {
          toast(
            "Extraction complete",
            `${formatNumber(state.stats.unique_emails)} unique email(s) from ${formatNumber(state.stats.websites_crawled)} website(s).`,
            "success"
          );
        }
      } else if (data.state === "cancelled") {
        toast("Extraction cancelled", "Partial results were kept in this session.", "warning");
      } else {
        const lastError = (data.errors || []).slice(-1)[0];
        toast("Extraction failed", lastError ? lastError.message : "Please check the server logs.", "error");
      }
    } catch (error) {
      window.clearInterval(timer);
      setBusy(false);
      if (error.name === "AbortError") {
        setProgressState("cancelled");
        el.progressMessage.textContent = "Extraction cancelled";
        toast("Extraction cancelled", "The request was stopped in this browser.", "warning");
        return;
      }
      showProgressError(error.message);
      toast("Extraction failed", error.message, "error");
    } finally {
      state.syncAbort = null;
    }
  }

  async function runBatchedSyncExtraction(input) {
    const controller = new AbortController();
    state.syncAbort = controller;
    let synthetic = 6;
    const timer = window.setInterval(() => {
      synthetic = Math.min(90, synthetic + 2);
      const current = Number.parseFloat(el.progressBar.style.width) || 0;
      if (current < synthetic) {
        el.progressBar.style.width = `${synthetic}%`;
        el.progressBarWrap.setAttribute("aria-valuenow", String(synthetic));
        el.progressPercent.textContent = `${synthetic}%`;
      }
    }, 2000);

    const aggregate = emptyStats();
    const results = [];
    const websites = [];
    const warnings = [];
    const errors = [];
    let parsed = null;
    let targetEmails = null;
    let targetWebsites = 0;
    let hadPartial = false;
    let hadFailure = false;
    let cancelled = false;

    el.progressMessage.textContent = "Preparing serverless batches";
    el.progressCurrentDomain.textContent = "The browser will combine several bounded API requests";

    try {
      let targets = [];
      const settings = { ...input.payload.settings };

      if (state.mode === "nl") {
        el.progressMessage.textContent = "Discovering business websites";
        const plan = await api("/api/parse-query", {
          method: "POST",
          body: JSON.stringify({ query: input.payload.query, settings }),
          signal: controller.signal
        });
        parsed = plan.parsed || null;
        const discoveryLimit = Math.min(
          200,
          Math.max(1, Number(parsed && parsed.target_websites) || Number(settings.target_websites) || 25)
        );
        const discovery = await api("/api/search", {
          method: "POST",
          body: JSON.stringify({
            query: input.payload.query,
            settings,
            max_results: discoveryLimit,
            use_places: settings.use_places !== false
          }),
          signal: controller.signal
        });
        parsed = discovery.parsed || parsed;
        targets = dedupeBatchTargets((discovery.websites || []).map((website) => ({
          ...website,
          domain: website.domain || targetDomain(website.url),
          url: website.url || `https://${targetDomain(website.domain)}`
        })));
        appendUnique(warnings, discovery.warnings || []);
        if (!targets.length) throw new Error("No websites were discovered for this search.");
      } else {
        const payload = input.payload || {};
        const items = [];
        (payload.domains || []).forEach((domain) => {
          items.push({
            domain,
            url: /^https?:\/\//i.test(domain) ? domain : `https://${domain}`,
            source: "provided",
            title: domain
          });
        });
        (payload.urls || []).forEach((url) => {
          const domain = targetDomain(url);
          items.push({ domain, url, source: "provided", title: domain });
        });
        targets = dedupeBatchTargets(items);
        if (!targets.length) throw new Error("No valid websites were provided.");
      }

      targetEmails = Number(parsed && parsed.target_emails) || Number(settings.target_emails) || null;
      targetWebsites = Number(parsed && parsed.target_websites) || Number(settings.target_websites) || targets.length;
      aggregate.websites_discovered = targets.length;
      aggregate.target_emails = targetEmails || 0;
      aggregate.target_websites = targetWebsites;
      websites.push(...targets);
      state.parsed = parsed;
      state.websites = websites;
      renderProgressStats(aggregate);
      renderProgressWebsites(websites);

      const batchSize = syncBatchSize();
      const batches = [];
      for (let offset = 0; offset < targets.length; offset += batchSize) {
        batches.push(targets.slice(offset, offset + batchSize));
      }

      for (let index = 0; index < batches.length; index += 1) {
        if (controller.signal.aborted) {
          cancelled = true;
          break;
        }
        if (targetEmails && results.length >= targetEmails) break;

        const batch = batches[index];
        const remaining = targetEmails ? Math.max(1, targetEmails - results.length) : null;
        const percent = Math.min(95, 8 + Math.round(((index + 1) / batches.length) * 87));
        el.progressBar.style.width = `${percent}%`;
        el.progressBarWrap.setAttribute("aria-valuenow", String(percent));
        el.progressPercent.textContent = `${percent}%`;
        el.progressMessage.textContent = `Crawling batch ${index + 1} of ${batches.length}`;
        el.progressCurrentDomain.textContent = `${formatNumber(results.length)} unique email(s) so far`;

        const batchSettings = {
          ...settings,
          target_emails: remaining,
          target_websites: batch.length,
          use_places: false
        };

        let data;
        try {
          data = await api("/api/extract-sync", {
            method: "POST",
            body: JSON.stringify({
              urls: batch.map((target) => target.url),
              settings: batchSettings
            }),
            signal: controller.signal
          });
        } catch (error) {
          if (error.name === "AbortError") {
            cancelled = true;
            break;
          }
          hadFailure = true;
          appendError(errors, `batch ${index + 1}`, error.message);
          if (error.status === 429) break;
          continue;
        }

        if (data.state === "error") {
          hadFailure = true;
          (data.errors || []).forEach((item) => appendError(errors, item.scope || `batch ${index + 1}`, item.message));
        } else if (data.partial) {
          hadPartial = true;
        }

        state.jobId = data.job_id || state.jobId;
        mergeBatchData(data, aggregate, results, websites);
        appendUnique(warnings, data.warnings || []);
        (data.errors || []).forEach((item) => appendError(errors, item.scope || `batch ${index + 1}`, item.message));
        state.results = results.slice();
        state.stats = { ...aggregate, unique_emails: results.length };
        renderProgressStats(state.stats);
        renderProgressWebsites(websites);
        renderProgressIssues(warnings, errors);
        renderResults();
        if (results.length) {
          el.results.classList.remove("hidden");
        }
      }

      window.clearInterval(timer);
      aggregate.unique_emails = results.length;
      aggregate.duplicates_removed = Math.max(0, Number(aggregate.emails_discovered) - results.length);
      aggregate.target_reached = Boolean(targetEmails && results.length >= targetEmails);
      aggregate.websites_discovered = Math.max(Number(aggregate.websites_discovered), targets.length);
      aggregate.target_emails = targetEmails || 0;
      aggregate.target_websites = targetWebsites;
      state.stats = aggregate;
      state.results = results.sort((a, b) => String(a.email).localeCompare(String(b.email)));
      state.websites = websites;
      state.selected.clear();
      state.visibleCount = PAGE_STEP;

      const partial = !aggregate.target_reached && (hadPartial || (hadFailure && results.length > 0));
      el.progressBar.style.width = "100%";
      el.progressBarWrap.setAttribute("aria-valuenow", "100");
      el.progressPercent.textContent = "100%";
      el.progressCurrentDomain.textContent = `${formatNumber(results.length)} unique email(s) from ${formatNumber(aggregate.websites_crawled)} website(s)`;

      if (cancelled) {
        el.progressMessage.textContent = "Extraction cancelled";
        setProgressState("cancelled");
        if (results.length) addHistory();
        toast("Extraction cancelled", "Partial results were kept in this session.", "warning");
      } else if (partial) {
        el.progressMessage.textContent = `Partial extraction: ${formatNumber(results.length)} unique email(s) from ${formatNumber(aggregate.websites_crawled)} website(s)`;
        warnings.push("Some batches did not finish. The results shown are partial.");
        setProgressState("partial");
        if (results.length) addHistory();
        toast("Partial results", el.progressMessage.textContent, "warning");
      } else if (results.length) {
        el.progressMessage.textContent = `Extraction complete: ${formatNumber(results.length)} unique email(s) from ${formatNumber(aggregate.websites_crawled)} website(s)`;
        if (targetEmails && !aggregate.target_reached) {
          warnings.push(`Only ${formatNumber(results.length)} of ${formatNumber(targetEmails)} requested emails were found.`);
        }
        setProgressState("done");
        addHistory();
        toast("Extraction complete", `${formatNumber(results.length)} unique email(s) from ${formatNumber(aggregate.websites_crawled)} website(s).`, "success");
      } else {
        el.progressMessage.textContent = hadFailure ? "Extraction failed" : "No publicly listed emails were found";
        setProgressState(hadFailure ? "error" : "done");
        if (hadFailure) {
          toast("Extraction failed", errors.length ? errors[errors.length - 1].message : "No batch completed successfully.", "error");
        } else {
          warnings.push("No publicly listed email addresses were found. Try more websites, a different location, or a broader industry.");
          toast("No emails found", "Try a broader query or add websites manually.", "warning");
        }
      }

      renderProgressStats(aggregate);
      renderProgressWebsites(websites);
      renderProgressIssues(warnings, errors);
      renderResults();
      if (state.results.length) {
        el.results.classList.remove("hidden");
        el.results.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      setBusy(false);
    } catch (error) {
      window.clearInterval(timer);
      setBusy(false);
      if (error.name === "AbortError") {
        setProgressState("cancelled");
        el.progressMessage.textContent = "Extraction cancelled";
        toast("Extraction cancelled", "The request was stopped in this browser.", "warning");
        return;
      }
      showProgressError(error.message);
      toast("Extraction failed", error.message, "error");
    } finally {
      window.clearInterval(timer);
      state.syncAbort = null;
    }
  }

  function syncBatchSize() {
    const configured = Number(state.config && state.config.sync_batch_size);
    return Number.isFinite(configured) ? Math.max(3, Math.min(50, configured)) : 10;
  }

  function targetDomain(value) {
    const text = String(value || "").trim();
    try {
      return new URL(text.includes("://") ? text : `https://${text}`).hostname.toLowerCase().replace(/^www\./, "");
    } catch {
      return text.toLowerCase().replace(/^www\./, "").replace(/\/.*$/, "");
    }
  }

  function dedupeBatchTargets(items) {
    const seen = new Set();
    return (items || []).filter((item) => {
      const domain = targetDomain(item.domain || item.url);
      if (!domain || seen.has(domain)) return false;
      seen.add(domain);
      item.domain = domain;
      if (!item.url) item.url = `https://${domain}`;
      return true;
    });
  }

  function appendUnique(list, values) {
    (values || []).forEach((value) => {
      const text = String(value || "").trim();
      if (text && !list.includes(text) && list.length < 20) list.push(text);
    });
  }

  function appendError(list, scope, message) {
    const text = String(message || "").trim();
    if (!text) return;
    const item = { scope: String(scope || "").slice(0, 120), message: text.slice(0, 240) };
    if (list.some((existing) => existing.scope === item.scope && existing.message === item.message)) return;
    if (list.length < 50) list.push(item);
  }

  function mergeBatchData(data, aggregate, results, websites) {
    const seen = new Set(results.map((row) => String(row.email || "").toLowerCase()));
    for (const row of data.results || []) {
      const email = String(row.email || "").toLowerCase();
      if (!email || seen.has(email)) continue;
      seen.add(email);
      results.push(row);
    }
    const stats = data.stats || {};
    aggregate.websites_crawled += Number(stats.websites_crawled) || 0;
    aggregate.websites_failed += Number(stats.websites_failed) || 0;
    aggregate.pages_crawled += Number(stats.pages_crawled) || 0;
    aggregate.pages_failed += Number(stats.pages_failed) || 0;
    aggregate.emails_discovered += Number(stats.emails_discovered) || 0;
    aggregate.unique_emails = results.length;
    aggregate.duplicates_removed = Math.max(0, aggregate.emails_discovered - results.length);
    (data.websites || []).forEach((website) => {
      const domain = website.domain || targetDomain(website.url);
      if (!domain || !websites.some((item) => (item.domain || targetDomain(item.url)) === domain)) {
        websites.push(website);
      }
    });
  }

  async function previewPlan() {
    if (state.mode !== "nl") {
      toast("Preview unavailable", "Switch to the Describe tab to preview a natural-language plan.", "info");
      return;
    }
    const query = el.queryInput.value.trim();
    if (query.length < 3) {
      toast("Add a description", "Describe the companies you want to find first.", "warning");
      return;
    }
    el.previewButton.disabled = true;
    try {
      const data = await api("/api/parse-query", {
        method: "POST",
        body: JSON.stringify({ query, settings: readSettings(), max_results: 20, use_places: el.usePlacesToggle.checked })
      });
      renderPlan(data.parsed);
    } catch (error) {
      toast("Could not build a plan", error.message, "error");
    } finally {
      el.previewButton.disabled = false;
    }
  }

  function renderPlan(parsed) {
    if (!parsed) return;
    const item = (label, value) => `
      <div class="plan-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "Any")}</strong></div>`;
    const queries = (parsed.search_queries || [])
      .map((query) => `<code>${escapeHtml(query)}</code>`)
      .join("");
    el.planPanel.innerHTML = `
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-[10px] font-extrabold uppercase tracking-[0.14em] text-emerald-700">Search plan</p>
          <p class="mt-1 text-[11px] text-emerald-900">Review how your description was interpreted before crawling.</p>
        </div>
        <button type="button" class="row-action" data-action="close-plan" aria-label="Close search plan"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button>
      </div>
      <div class="plan-grid mt-3">
        ${item("Industry", parsed.industry)}
        ${item("Location", parsed.location_label)}
        ${item("Category", parsed.email_category)}
        ${item("Target", `${parsed.target_emails} emails / ${parsed.target_websites} sites`)}
      </div>
      ${parsed.domains && parsed.domains.length ? `<p class="mt-3 text-[11px]"><strong>Domains:</strong> ${escapeHtml(parsed.domains.join(", "))}</p>` : ""}
      ${parsed.audience ? `<p class="mt-1 text-[11px]"><strong>Audience:</strong> ${escapeHtml(parsed.audience)}</p>` : ""}
      <div class="plan-queries">${queries}</div>
    `;
    el.planPanel.classList.remove("hidden");
    el.planPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function connectProgress(jobId) {
    closeProgressStream();
    if (typeof EventSource === "undefined") {
      pollJob(jobId);
      return;
    }
    const source = new EventSource(`/api/progress/${jobId}`);
    state.eventSource = source;
    let errors = 0;

    source.onmessage = (event) => {
      try {
        handleProgress(JSON.parse(event.data));
      } catch {
        /* ignore malformed event */
      }
    };
    source.addEventListener("done", (event) => {
      try {
        handleProgress(JSON.parse(event.data));
      } catch {
        /* ignore malformed event */
      }
      finishJob(jobId);
    });
    source.addEventListener("error", (event) => {
      if (event.data) {
        try {
          const payload = JSON.parse(event.data);
          toast("Progress unavailable", payload.detail || "The job may have expired.", "warning");
        } catch {
          /* ignore */
        }
        closeProgressStream();
        setBusy(false);
      }
    });
    source.onerror = () => {
      errors += 1;
      if (errors >= 3) {
        closeProgressStream();
        pollJob(jobId);
      }
    };
  }

  function closeProgressStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function pollJob(jobId) {
    closeProgressStream();
    let attempts = 0;
    state.pollTimer = setInterval(async () => {
      attempts += 1;
      try {
        const job = await api(`/api/jobs/${jobId}`);
        handleProgress(job);
        if (["done", "error", "cancelled"].includes(job.state)) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          finishJob(jobId);
        }
      } catch (error) {
        if (attempts >= 30) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          setBusy(false);
          toast("Lost connection", error.message, "error");
        }
      }
    }, 1500);
  }

  function handleProgress(job) {
    if (!job) return;
    if (job.stats) state.stats = job.stats;
    if (job.parsed) state.parsed = job.parsed;
    if (Array.isArray(job.websites)) state.websites = job.websites;

    const percent = Math.max(0, Math.min(100, Number(job.progress) || 0));
    el.progressBar.style.width = `${percent}%`;
    el.progressBarWrap.setAttribute("aria-valuenow", String(percent));
    el.progressPercent.textContent = `${percent}%`;
    el.progressMessage.textContent = job.message || "Working";
    el.progressCurrentDomain.textContent = job.current_domain
      ? `Current website: ${job.current_domain}`
      : job.phase === "searching"
        ? "Searching for matching business websites"
        : "Waiting for the first website";

    setProgressState(job.state);
    renderProgressStats(job.stats || state.stats);
    renderProgressWebsites(job.websites || []);
    renderProgressIssues(job.warnings || [], job.errors || []);

    if (["done", "error", "cancelled"].includes(job.state)) {
      setBusy(false);
      finishJob(job.job_id || state.jobId);
    }
  }

  function setProgressState(jobState) {
    const states = {
      queued: ["badge-navy", "fa-clock", "Queued"],
      running: ["badge-emerald", "fa-circle-notch fa-spin", "Running"],
      done: ["badge-emerald", "fa-circle-check", "Complete"],
      partial: ["badge-amber", "fa-triangle-exclamation", "Partial"],
      error: ["badge-rose", "fa-circle-exclamation", "Failed"],
      cancelled: ["badge-amber", "fa-stop", "Cancelled"]
    };
    const [badgeClass, icon, label] = states[jobState] || states.running;
    el.progressStateBadge.className = `badge ${badgeClass}`;
    el.progressStateBadge.innerHTML = `<i class="fa-solid ${icon}" aria-hidden="true"></i> ${label}`;
    const active = jobState === "queued" || jobState === "running";
    el.cancelButton.classList.toggle("hidden", !active);
    el.cancelButton.disabled = !active;
  }

  function renderProgressStats(stats) {
    el.progressDiscovered.textContent = formatNumber(stats.websites_discovered);
    el.progressCrawled.textContent = formatNumber(stats.websites_crawled);
    el.progressUnique.textContent = formatNumber(stats.unique_emails);
    el.progressDuplicates.textContent = formatNumber(stats.duplicates_removed);
  }

  function renderProgressWebsites(websites) {
    if (!Array.isArray(websites) || !websites.length) {
      el.progressWebsites.innerHTML = "";
      return;
    }
    const visible = websites.slice(0, 12);
    const chips = visible.map((website) => {
      const domain = website.domain || "";
      const icon = website.source === "places" ? "fa-location-dot" : website.source === "provided" ? "fa-link" : "fa-globe";
      return `<span class="inline-flex max-w-full items-center gap-1.5 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-semibold text-slate-600"><i class="fa-solid ${icon} text-emerald-600" aria-hidden="true"></i><span class="truncate">${escapeHtml(domain)}</span></span>`;
    }).join("");
    const more = websites.length > visible.length
      ? `<span class="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-bold text-slate-500">+${websites.length - visible.length} more</span>`
      : "";
    el.progressWebsites.innerHTML = chips + more;
  }

  function renderProgressIssues(warnings, errors) {
    const messages = [];
    (warnings || []).forEach((warning) => messages.push(String(warning)));
    (errors || []).forEach((error) => {
      const prefix = error.scope ? `${error.scope}: ` : "";
      messages.push(`${prefix}${error.message || ""}`);
    });
    const filtered = messages.filter(Boolean);
    if (!filtered.length) {
      el.progressIssues.classList.add("hidden");
      el.progressIssues.innerHTML = "";
      return;
    }
    el.progressIssues.classList.remove("hidden");
    el.progressIssues.innerHTML = `
      <p class="font-bold">${filtered.length} notice${filtered.length === 1 ? "" : "s"}</p>
      <ul class="mt-1 space-y-1">${filtered.slice(0, 6).map((message) => `<li>${escapeHtml(message)}</li>`).join("")}</ul>
    `;
  }

  function resetProgress() {
    setProgressState("running");
    el.progressPercent.textContent = "0%";
    el.progressBar.style.width = "0%";
    el.progressBarWrap.setAttribute("aria-valuenow", "0");
    el.progressMessage.textContent = "Preparing extraction";
    el.progressCurrentDomain.textContent = "Waiting for the first website";
    el.progressDiscovered.textContent = "0";
    el.progressCrawled.textContent = "0";
    el.progressUnique.textContent = "0";
    el.progressDuplicates.textContent = "0";
    el.progressWebsites.innerHTML = "";
    el.progressIssues.classList.add("hidden");
    el.progressIssues.innerHTML = "";
  }

  function showProgress() {
    el.progressSection.classList.remove("hidden");
    el.progressSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function showProgressError(message) {
    el.progressSection.classList.remove("hidden");
    setProgressState("error");
    el.progressMessage.textContent = message || "Extraction failed";
    el.progressPercent.textContent = "0%";
    el.progressBar.style.width = "0%";
  }

  async function finishJob(jobId) {
    if (!jobId || state.jobFinished) return;
    state.jobFinished = true;
    closeProgressStream();
    setBusy(false);

    try {
      const data = await api(`/api/jobs/${jobId}/results`);
      state.stats = data.stats || state.stats;
      state.parsed = data.parsed || state.parsed;
      state.websites = data.websites || state.websites;
      state.results = Array.isArray(data.results) ? data.results : [];
      state.selected.clear();
      state.visibleCount = PAGE_STEP;
      renderResults();

      if (state.results.length > 0) {
        el.results.classList.remove("hidden");
        el.results.scrollIntoView({ behavior: "smooth", block: "start" });
      }

      if (data.state === "done") {
        addHistory();
        if (data.partial) {
          toast("Partial results", data.message || "The serverless time limit was reached.", "warning");
        } else {
          toast(
            "Extraction complete",
            `${formatNumber(state.stats.unique_emails)} unique email(s) from ${formatNumber(state.stats.websites_crawled)} website(s).`,
            "success"
          );
        }
      } else if (data.state === "cancelled") {
        if (state.results.length) addHistory();
        toast("Extraction cancelled", "Partial results were kept in this session.", "warning");
      } else if (data.state === "error") {
        const lastError = (data.errors || []).slice(-1)[0];
        toast("Extraction failed", lastError ? lastError.message : "Please check the server logs.", "error");
      }
    } catch (error) {
      toast("Could not load results", error.message, "error");
    }
  }

  async function cancelExtraction() {
    if (state.syncAbort) {
      state.syncAbort.abort();
      return;
    }
    if (!state.jobId) return;
    el.cancelButton.disabled = true;
    try {
      const data = await api("/api/cancel", {
        method: "POST",
        body: JSON.stringify({ job_id: state.jobId })
      });
      toast("Cancellation requested", data.message || "The job is stopping.", "info");
    } catch (error) {
      toast("Could not cancel", error.message, "error");
      el.cancelButton.disabled = false;
    }
  }

  function renderResults() {
    renderStats();
    populateDomainFilter();
    applyFilters();
    const count = state.results.length;
    el.resultsCountLabel.textContent = `${formatNumber(count)} record${count === 1 ? "" : "s"}`;
    if (count === 0) {
      el.resultsEmpty.classList.remove("hidden");
    }
  }

  function renderStats() {
    const stats = state.stats || emptyStats();
    el.statDiscovered.textContent = formatNumber(stats.websites_discovered);
    el.statCrawled.textContent = formatNumber(stats.websites_crawled);
    el.statFailed.textContent = formatNumber(stats.websites_failed);
    el.statEmails.textContent = formatNumber(stats.emails_discovered);
    el.statUnique.textContent = formatNumber(stats.unique_emails);
    el.statDuplicates.textContent = formatNumber(stats.duplicates_removed);
  }

  function populateDomainFilter() {
    const current = el.domainFilter.value;
    const domains = Array.from(new Set(state.results.map((row) => row.website_domain || row.domain).filter(Boolean))).sort();
    el.domainFilter.innerHTML = `<option value="">All domains</option>` +
      domains.map((domain) => `<option value="${escapeHtml(domain)}">${escapeHtml(domain)}</option>`).join("");
    if (domains.includes(current)) el.domainFilter.value = current;
  }

  function applyFilters() {
    const query = el.resultSearch.value.trim().toLowerCase();
    const category = el.categoryFilter.value;
    const domain = el.domainFilter.value;
    const sort = el.sortSelect.value;

    let filtered = state.results.filter((row) => {
      if (category && row.category !== category) return false;
      if (domain && (row.website_domain || row.domain) !== domain) return false;
      if (query) {
        const haystack = [row.email, row.company, row.website_domain, row.domain, row.country, row.category_label]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });

    if (sort === "email-desc") {
      filtered = filtered.slice().sort((a, b) => String(b.email).localeCompare(String(a.email)));
    } else if (sort === "newest") {
      filtered = filtered.slice().sort((a, b) => String(b.extracted_at || "").localeCompare(String(a.extracted_at || "")));
    } else if (sort === "category") {
      filtered = filtered.slice().sort((a, b) => {
        const cat = String(a.category || "").localeCompare(String(b.category || ""));
        return cat !== 0 ? cat : String(a.email).localeCompare(String(b.email));
      });
    } else {
      filtered = filtered.slice().sort((a, b) => String(a.email).localeCompare(String(b.email)));
    }

    state.filtered = filtered;
    state.visibleCount = Math.min(state.visibleCount, Math.max(PAGE_STEP, filtered.length));
    renderRows();
    updateSelectionUI();
    el.resultsCountLabel.textContent = `${formatNumber(filtered.length)} of ${formatNumber(state.results.length)} records`;
  }

  function renderRows() {
    const visible = state.filtered.slice(0, state.visibleCount);
    const hasRows = visible.length > 0;

    if (!hasRows) {
      el.resultsTableBody.innerHTML = `<tr><td colspan="10" class="empty-row">No emails match the current filters.</td></tr>`;
      el.resultsCards.innerHTML = `<div class="rounded-xl border border-dashed border-slate-200 px-4 py-8 text-center text-xs text-slate-500">No emails match the current filters.</div>`;
      el.resultsTableWrap.classList.remove("is-visible");
      el.resultsCards.classList.add("hidden");
      el.resultsEmpty.classList.remove("hidden");
      el.loadMoreButton.classList.add("hidden");
      return;
    }

    el.resultsEmpty.classList.add("hidden");
    el.resultsTableWrap.classList.add("is-visible");
    el.resultsCards.classList.remove("hidden");

    el.resultsTableBody.innerHTML = visible.map((row) => tableRow(row)).join("");
    el.resultsCards.innerHTML = visible.map((row) => mobileCard(row)).join("");
    el.loadMoreButton.classList.toggle("hidden", state.filtered.length <= state.visibleCount);
  }

  function tableRow(row) {
    const selected = state.selected.has(row.email);
    const category = categoryMeta(row.category);
    const source = sourceLink(row.source_url);
    const valid = Boolean(row.syntax_valid);
    return `
      <tr>
        <td>
          <input type="checkbox" class="checkbox result-check" data-email="${escapeHtml(row.email)}" ${selected ? "checked" : ""} aria-label="Select ${escapeHtml(row.email)}">
        </td>
        <td>
          <div class="email-cell">${escapeHtml(row.email)}</div>
          <div class="mt-0.5 text-[9px] font-semibold uppercase tracking-wide text-slate-400">${escapeHtml(METHOD_LABELS[row.method] || "Page text")}</div>
        </td>
        <td class="cell-strong">${escapeHtml(row.company || "—")}</td>
        <td class="cell-muted">${escapeHtml(row.website_domain || row.domain || "—")}</td>
        <td><span class="category-badge ${category.cls}">${escapeHtml(category.label)}</span></td>
        <td class="cell-muted">${escapeHtml(countryName(row.country))}</td>
        <td>${source}</td>
        <td class="cell-muted whitespace-nowrap">${escapeHtml(formatDate(row.extracted_at, true))}</td>
        <td>
          <span class="validation-badge ${valid ? "" : "is-warning"}" title="Syntax validated. Deliverability is not guaranteed.">
            <i class="fa-solid ${valid ? "fa-check" : "fa-question"}" aria-hidden="true"></i>${valid ? "Valid" : "Check"}
          </span>
        </td>
        <td>
          <button type="button" class="row-action remove-result" data-email="${escapeHtml(row.email)}" aria-label="Remove ${escapeHtml(row.email)}">
            <i class="fa-regular fa-trash-can" aria-hidden="true"></i>
          </button>
        </td>
      </tr>
    `;
  }

  function mobileCard(row) {
    const selected = state.selected.has(row.email);
    const category = categoryMeta(row.category);
    const source = sourceLink(row.source_url);
    const valid = Boolean(row.syntax_valid);
    return `
      <article class="mobile-result-card">
        <div class="mobile-card-head flex items-start gap-2.5">
          <input type="checkbox" class="checkbox result-check mt-0.5" data-email="${escapeHtml(row.email)}" ${selected ? "checked" : ""} aria-label="Select ${escapeHtml(row.email)}">
          <div class="mobile-card-body min-w-0 flex-1">
            <p class="email-cell">${escapeHtml(row.email)}</p>
            <p class="mt-1 truncate text-[11px] font-semibold text-slate-600">${escapeHtml(row.company || row.website_domain || "Unknown company")}</p>
            <div class="mt-2 flex flex-wrap items-center gap-1.5">
              <span class="category-badge ${category.cls}">${escapeHtml(category.label)}</span>
              <span class="validation-badge ${valid ? "" : "is-warning"}"><i class="fa-solid ${valid ? "fa-check" : "fa-question"}" aria-hidden="true"></i>${valid ? "Valid syntax" : "Check syntax"}</span>
              <span class="rounded-full bg-slate-100 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide text-slate-500">${escapeHtml(countryName(row.country))}</span>
            </div>
          </div>
          <button type="button" class="row-action remove-result" data-email="${escapeHtml(row.email)}" aria-label="Remove ${escapeHtml(row.email)}">
            <i class="fa-regular fa-trash-can" aria-hidden="true"></i>
          </button>
        </div>
        <dl class="result-meta">
          <div><dt>Website</dt><dd>${escapeHtml(row.website_domain || row.domain || "—")}</dd></div>
          <div><dt>Email domain</dt><dd>${escapeHtml(row.domain || "—")}</dd></div>
          <div><dt>Extracted</dt><dd>${escapeHtml(formatDate(row.extracted_at))}</dd></div>
          <div><dt>Source</dt><dd>${source}</dd></div>
        </dl>
      </article>
    `;
  }

  function sourceLink(value) {
    const href = safeUrl(value);
    if (!href) return `<span class="cell-muted">—</span>`;
    let label = href;
    try {
      const parsed = new URL(href);
      label = `${parsed.hostname}${parsed.pathname === "/" ? "" : parsed.pathname}`;
    } catch {
      /* keep full value */
    }
    const short = label.length > 42 ? `${label.slice(0, 39)}...` : label;
    return `<a class="source-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(href)}">${escapeHtml(short)}</a>`;
  }

  function handleResultChange(event) {
    const checkbox = event.target.closest(".result-check");
    if (!checkbox) return;
    const email = checkbox.dataset.email;
    if (checkbox.checked) state.selected.add(email);
    else state.selected.delete(email);
    document.querySelectorAll(".result-check").forEach((item) => {
      if (item.dataset.email === email) item.checked = checkbox.checked;
    });
    updateSelectionUI();
  }

  function handleResultClick(event) {
    const remove = event.target.closest(".remove-result");
    if (!remove) return;
    removeResult(remove.dataset.email);
  }

  function updateSelectionUI() {
    const selectedCount = state.filtered.filter((row) => state.selected.has(row.email)).length;
    el.selectedCount.textContent = `${formatNumber(selectedCount)} selected`;
    const allSelected = state.filtered.length > 0 && selectedCount === state.filtered.length;
    el.selectAll.checked = allSelected;
    el.selectAll.indeterminate = selectedCount > 0 && !allSelected;
    el.copySelectedButton.disabled = selectedCount === 0;
    el.selectionHint.textContent = selectedCount > 0
      ? `${formatNumber(selectedCount)} selected for export`
      : "Export uses all filtered rows.";
  }

  function toggleSelectAll() {
    if (el.selectAll.checked) {
      state.filtered.forEach((row) => state.selected.add(row.email));
    } else {
      state.filtered.forEach((row) => state.selected.delete(row.email));
    }
    renderRows();
    updateSelectionUI();
  }

  async function copySelected() {
    const rows = state.filtered.filter((row) => state.selected.has(row.email));
    if (!rows.length) {
      toast("Nothing selected", "Select one or more email addresses first.", "warning");
      return;
    }
    await copyRows(rows, "Selected emails copied");
  }

  async function copyAll() {
    const rows = state.filtered.length ? state.filtered : state.results;
    if (!rows.length) {
      toast("No emails to copy", "Run an extraction first.", "warning");
      return;
    }
    await copyRows(rows, "All filtered emails copied");
  }

  async function copyRows(rows, successTitle) {
    const text = rows.map((row) => row.email).join("\n");
    const copied = await copyText(text);
    if (copied) toast(successTitle, `${formatNumber(rows.length)} email address(es) on your clipboard.`, "success");
    else toast("Copy failed", "Your browser blocked clipboard access. Use an export instead.", "error");
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch {
        ok = false;
      }
      area.remove();
      return ok;
    }
  }

  function toggleExportMenu() {
    const hidden = el.exportMenu.classList.toggle("hidden");
    el.exportButton.setAttribute("aria-expanded", String(!hidden));
  }

  function closeExportMenu() {
    el.exportMenu.classList.add("hidden");
    el.exportButton.setAttribute("aria-expanded", "false");
  }

  function exportRows(format) {
    const base = state.filtered.length ? state.filtered : state.results;
    const selected = base.filter((row) => state.selected.has(row.email));
    const rows = selected.length ? selected : base;
    if (!rows.length) {
      toast("Nothing to export", "Run an extraction or adjust your filters.", "warning");
      return;
    }
    if (format === "csv") {
      downloadBlob(Exporter.toCsv(rows), "text/csv;charset=utf-8", Exporter.filename("csv"));
      toast("CSV exported", `${formatNumber(rows.length)} rows downloaded.`, "success");
      return;
    }
    if (format === "txt") {
      downloadBlob(Exporter.toTxt(rows), "text/plain;charset=utf-8", Exporter.filename("txt"));
      toast("Text file exported", `${formatNumber(rows.length)} email addresses downloaded.`, "success");
      return;
    }
    if (format === "json") {
      downloadBlob(Exporter.toJson(rows), "application/json;charset=utf-8", Exporter.filename("json"));
      toast("JSON exported", `${formatNumber(rows.length)} records downloaded.`, "success");
      return;
    }
    if (format === "xlsx") {
      if (typeof XLSX === "undefined") {
        toast("Excel library unavailable", "Falling back to CSV export.", "warning");
        exportRows("csv");
        return;
      }
      const data = Exporter.toSheetRows(rows);
      const worksheet = XLSX.utils.json_to_sheet(data);
      worksheet["!cols"] = [
        { wch: 34 }, { wch: 24 }, { wch: 24 }, { wch: 24 }, { wch: 16 },
        { wch: 12 }, { wch: 46 }, { wch: 14 }, { wch: 14 }, { wch: 24 }
      ];
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, worksheet, "Emails");
      XLSX.writeFile(workbook, Exporter.filename("xlsx"));
      toast("Excel exported", `${formatNumber(rows.length)} rows downloaded.`, "success");
    }
  }

  function downloadBlob(content, mimeType, filename) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  function removeResult(email) {
    state.results = state.results.filter((row) => row.email !== email);
    state.selected.delete(email);
    state.stats.unique_emails = state.results.length;
    state.stats.duplicates_removed = Math.max(0, Number(state.stats.emails_discovered || 0) - state.results.length);
    renderResults();
    if (!state.results.length) {
      el.resultsEmpty.classList.remove("hidden");
    }
    toast("Result removed", "The email was removed from this session.", "info");
  }

  function clearResults() {
    if (!state.results.length) return;
    if (!window.confirm("Clear all results from this session? Exported files are not affected.")) return;
    state.results = [];
    state.filtered = [];
    state.selected.clear();
    state.stats = emptyStats();
    el.results.classList.add("hidden");
    el.resultSearch.value = "";
    el.categoryFilter.value = "";
    el.domainFilter.value = "";
    el.sortSelect.value = "email-asc";
    el.resultsTableBody.innerHTML = "";
    el.resultsCards.innerHTML = "";
    el.resultsCountLabel.textContent = "0 records";
    toast("Results cleared", "The session results were removed from this page.", "info");
  }

  function renderHistory() {
    if (!state.history.length) {
      el.historyList.innerHTML = "";
      el.historyEmpty.classList.remove("hidden");
      return;
    }
    el.historyEmpty.classList.add("hidden");
    el.historyList.innerHTML = state.history.map((entry) => {
      const stats = entry.stats || {};
      const label = entry.label || "Manual website list";
      const industry = entry.parsed && entry.parsed.industry ? entry.parsed.industry : "Manual domains";
      return `
        <article class="history-card">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="history-title">${escapeHtml(label)}</p>
              <p class="history-meta mt-1">${escapeHtml(formatDate(entry.created_at))} · ${escapeHtml(industry)}</p>
            </div>
            <span class="badge badge-emerald">${formatNumber(stats.unique_emails)} emails</span>
          </div>
          <div class="history-stats">
            <span class="history-pill">${formatNumber(stats.websites_crawled)} crawled</span>
            <span class="history-pill">${formatNumber(stats.websites_failed)} failed</span>
            <span class="history-pill">${formatNumber(stats.duplicates_removed)} duplicates</span>
          </div>
          <div class="flex flex-wrap gap-2">
            <button type="button" class="btn-secondary text-xs" data-history-restore="${escapeHtml(entry.id)}"><i class="fa-solid fa-rotate-left" aria-hidden="true"></i> Restore</button>
            <button type="button" class="btn-ghost text-xs" data-history-delete="${escapeHtml(entry.id)}"><i class="fa-regular fa-trash-can" aria-hidden="true"></i> Delete</button>
          </div>
        </article>
      `;
    }).join("");
  }

  function addHistory() {
    if (!state.results.length) return;
    const entry = {
      id: state.jobId || `local-${Date.now()}`,
      label: state.currentLabel || "Manual website list",
      created_at: new Date().toISOString(),
      parsed: state.parsed,
      stats: { ...state.stats, unique_emails: state.results.length },
      results: state.results.slice(0, 3000),
      websites: state.websites.slice(0, 100)
    };
    state.history = [entry, ...state.history.filter((item) => item.id !== entry.id)].slice(0, MAX_HISTORY);
    persistHistory();
    renderHistory();
  }

  function persistHistory() {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history));
      return;
    } catch {
      /* quota exceeded; trim below */
    }
    state.history = state.history.map((entry) => ({ ...entry, results: entry.results.slice(0, 500) }));
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history));
    } catch {
      state.history = state.history.slice(0, 3);
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history));
      } catch {
        toast("History storage full", "Could not save local history. Export your results instead.", "warning");
      }
    }
  }

  function loadHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      state.history = Array.isArray(parsed) ? parsed.filter((entry) => entry && entry.id && Array.isArray(entry.results)) : [];
    } catch {
      state.history = [];
    }
  }

  function restoreHistory(id) {
    const entry = state.history.find((item) => item.id === id);
    if (!entry) return;
    state.results = Array.isArray(entry.results) ? entry.results : [];
    state.stats = { ...emptyStats(), ...(entry.stats || {}) };
    state.stats.unique_emails = state.results.length;
    state.parsed = entry.parsed || null;
    state.websites = Array.isArray(entry.websites) ? entry.websites : [];
    state.currentLabel = entry.label || "Restored extraction";
    state.selected.clear();
    state.visibleCount = PAGE_STEP;
    state.jobFinished = true;
    renderResults();
    el.results.classList.remove("hidden");
    el.results.scrollIntoView({ behavior: "smooth", block: "start" });
    toast("History restored", `${formatNumber(state.results.length)} records loaded from this browser.`, "success");
  }

  function deleteHistory(id) {
    state.history = state.history.filter((entry) => entry.id !== id);
    persistHistory();
    renderHistory();
    toast("History entry deleted", "The local record was removed.", "info");
  }

  function clearHistory() {
    if (!state.history.length) return;
    if (!window.confirm("Erase all local extraction history from this browser? This cannot be undone.")) return;
    state.history = [];
    try {
      localStorage.removeItem(HISTORY_KEY);
    } catch {
      /* ignore */
    }
    renderHistory();
    toast("Local history erased", "No extraction history remains in this browser.", "success");
  }

  async function loadConfig() {
    try {
      state.config = await api("/api/config");
    } catch {
      state.config = null;
    }
  }

  function loadPreferences() {
    try {
      const prefs = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
      if (prefs.mode) state.mode = prefs.mode;
      if (prefs.industry) el.industryInput.value = prefs.industry;
      if (prefs.country) el.countrySelect.value = prefs.country;
      if (prefs.city) el.cityInput.value = prefs.city;
      if (prefs.category) el.categorySelect.value = prefs.category;
      if (prefs.targetEmails) el.targetEmails.value = prefs.targetEmails;
      if (prefs.targetWebsites) el.targetWebsites.value = prefs.targetWebsites;
      if (prefs.concurrency) el.concurrencyRange.value = prefs.concurrency;
      if (prefs.maxPages) el.maxPagesRange.value = prefs.maxPages;
      if (typeof prefs.usePlaces === "boolean") el.usePlacesToggle.checked = prefs.usePlaces;
    } catch {
      /* ignore malformed preferences */
    }
  }

  function savePreferences() {
    const prefs = {
      mode: state.mode,
      industry: el.industryInput.value.trim(),
      country: el.countrySelect.value,
      city: el.cityInput.value.trim(),
      category: el.categorySelect.value,
      targetEmails: el.targetEmails.value,
      targetWebsites: el.targetWebsites.value,
      concurrency: el.concurrencyRange.value,
      maxPages: el.maxPagesRange.value,
      usePlaces: el.usePlacesToggle.checked
    };
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch {
      /* preferences are optional */
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    el.startButton.disabled = busy;
    el.previewButton.disabled = busy;
    el.startButtonLabel.textContent = busy ? "Extraction running" : "Start extraction";
    const icon = el.startButton.querySelector("i");
    if (icon) icon.className = busy ? "fa-solid fa-circle-notch fa-spin" : "fa-solid fa-magnifying-glass";
  }

  function categoryMeta(category) {
    return CATEGORY_META[category] || CATEGORY_META.other;
  }

  function countryName(code) {
    if (!code || code === "Unknown") return "—";
    const fixed = { UK: "GB", USA: "US", UAE: "AE" }[code] || code;
    if (/^[A-Za-z]{2}$/.test(fixed)) {
      try {
        const names = new Intl.DisplayNames(["en"], { type: "region" });
        return names.of(fixed.toUpperCase()) || fixed.toUpperCase();
      } catch {
        return fixed.toUpperCase();
      }
    }
    return code;
  }

  function formatDate(value, compact = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    try {
      return date.toLocaleString(undefined, compact
        ? { month: "short", day: "numeric", year: "2-digit" }
        : { dateStyle: "medium", timeStyle: "short" });
    } catch {
      return date.toISOString().slice(0, 10);
    }
  }

  function formatNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat().format(number) : "0";
  }

  function safeUrl(value) {
    if (!value) return "";
    try {
      const url = new URL(value, window.location.origin);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
    } catch {
      return "";
    }
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function toast(title, message, type = "info") {
    const container = el.toastContainer;
    if (!container) return;
    while (container.children.length >= 4) container.firstElementChild.remove();
    const node = document.createElement("div");
    node.className = `toast toast-${type}`;
    node.setAttribute("role", "status");
    node.innerHTML = `
      <span class="toast-icon"><i class="fa-solid ${TOAST_ICONS[type] || TOAST_ICONS.info}" aria-hidden="true"></i></span>
      <div class="min-w-0 flex-1">
        <p class="toast-title">${escapeHtml(title)}</p>
        ${message ? `<p class="toast-message">${escapeHtml(message)}</p>` : ""}
      </div>
      <button type="button" class="row-action shrink-0" aria-label="Dismiss notification"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button>
    `;
    const dismiss = () => node.remove();
    node.querySelector("button").addEventListener("click", dismiss);
    container.appendChild(node);
    setTimeout(dismiss, 6500);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {})
      }
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : `Request failed (${response.status})`;
      const error = new Error(detail);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }
})();
