(() => {
  const normalize = (value) => String(value || "").toLowerCase().trim().replace(/\s+/g, " ");
  const escapeHtml = (value) => String(value || "").replace(/[&<>"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"
  })[character]);

  function installMenuSearch() {
    const input = document.querySelector("#si-menu-search");
    const results = document.querySelector("#si-menu-search-results");
    const navigation = document.querySelector(".si-menu-tree");
    if (!input || !results || !navigation) return;

    const entries = [...navigation.querySelectorAll("a[href]")].map((link) => ({
      title: link.textContent.trim(),
      group: link.dataset.searchGroup || "Navigation",
      keywords: link.dataset.searchKeywords || "",
      href: link.href
    }));
    let matches = [];
    let activeIndex = -1;

    const close = () => {
      results.hidden = true;
      results.innerHTML = "";
      input.setAttribute("aria-expanded", "false");
      activeIndex = -1;
    };

    const activate = (index) => {
      const options = [...results.querySelectorAll("a")];
      options.forEach((option, optionIndex) => option.classList.toggle("active", optionIndex === index));
      activeIndex = index;
      if (options[index]) options[index].scrollIntoView({ block: "nearest" });
    };

    const render = () => {
      const query = normalize(input.value);
      if (!query) return close();
      const terms = query.split(" ");
      matches = entries.filter((entry) => {
        const haystack = normalize(`${entry.title} ${entry.group} ${entry.keywords}`);
        return terms.every((term) => haystack.includes(term));
      }).slice(0, 9);
      results.innerHTML = matches.length
        ? matches.map((entry, index) => `<a role="option" href="${escapeHtml(entry.href)}" data-result-index="${index}"><strong>${escapeHtml(entry.title)}</strong><span>${escapeHtml(entry.group)}</span></a>`).join("")
        : '<p class="si-search-empty">No matching tools or pages</p>';
      results.hidden = false;
      input.setAttribute("aria-expanded", "true");
      activeIndex = -1;
    };

    input.addEventListener("input", render);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        input.value = "";
        close();
        return;
      }
      if (!matches.length || results.hidden) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        activate((activeIndex + 1) % matches.length);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        activate((activeIndex - 1 + matches.length) % matches.length);
      } else if (event.key === "Enter") {
        event.preventDefault();
        window.location.href = matches[Math.max(activeIndex, 0)].href;
      }
    });
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        input.focus();
        input.select();
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".si-menu-search-wrap")) close();
    });
  }

  function installPageSearch() {
    const container = document.querySelector(".si-page-search[data-site]");
    if (!container) return;
    const input = container.querySelector("input");
    const results = container.querySelector(".si-page-search-results");
    const site = container.dataset.site;
    if (!input || !results || !site) return;

    let pages = [];
    let loaded = false;
    let loading = null;
    const loadPages = () => {
      if (loaded) return Promise.resolve(pages);
      if (loading) return loading;
      loading = fetch(`/api/crawl-pages?site=${encodeURIComponent(site)}&limit=500`, { headers: { Accept: "application/json" } })
        .then((response) => {
          if (!response.ok) throw new Error("Page inventory is unavailable");
          return response.json();
        })
        .then((data) => {
          pages = Array.isArray(data) ? data : [];
          loaded = true;
          return pages;
        });
      return loading;
    };

    const close = () => {
      results.hidden = true;
      input.setAttribute("aria-expanded", "false");
    };
    const render = () => {
      const query = normalize(input.value);
      if (query.length < 2) return close();
      results.innerHTML = '<p class="si-search-empty">Searching page inventory...</p>';
      results.hidden = false;
      input.setAttribute("aria-expanded", "true");
      loadPages().then(() => {
        const terms = query.split(" ");
        const matches = pages.filter((page) => {
          const haystack = normalize(`${page.title || ""} ${page.url || ""}`);
          return terms.every((term) => haystack.includes(term));
        }).slice(0, 8);
        results.innerHTML = matches.length
          ? matches.map((page) => `<a role="option" href="/pages/inspect?site=${encodeURIComponent(site)}&url=${encodeURIComponent(page.url)}"><strong>${escapeHtml(page.title || "Untitled page")}</strong><span>${escapeHtml(page.url)}</span><em class="status-${Number(page.status_code) >= 400 ? "bad" : "good"}">${escapeHtml(page.status_code || "-")}</em></a>`).join("")
          : '<p class="si-search-empty">No crawled pages match this search</p>';
      }).catch(() => {
        results.innerHTML = '<p class="si-search-empty">Could not load the page inventory</p>';
      });
    };

    input.setAttribute("aria-expanded", "false");
    input.addEventListener("input", render);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        input.value = "";
        close();
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".si-page-search")) close();
    });
  }

  function installNavigationDrawer() {
    const toggle = document.querySelector(".si-drawer-toggle");
    const drawer = document.querySelector("#si-navigation-drawer");
    const scrim = document.querySelector(".si-drawer-scrim");
    if (!toggle || !drawer || !scrim) return;

    const close = () => {
      document.body.classList.remove("si-drawer-open");
      toggle.setAttribute("aria-expanded", "false");
    };
    const open = () => {
      document.body.classList.add("si-drawer-open");
      toggle.setAttribute("aria-expanded", "true");
      const search = drawer.querySelector("#si-menu-search");
      if (search) search.focus();
    };

    toggle.addEventListener("click", () => {
      if (document.body.classList.contains("si-drawer-open")) close();
      else open();
    });
    scrim.addEventListener("click", close);
    drawer.addEventListener("click", (event) => {
      if (event.target.closest("a[href]") && window.matchMedia("(max-width: 1080px)").matches) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && document.body.classList.contains("si-drawer-open")) close();
    });
    window.matchMedia("(min-width: 1081px)").addEventListener("change", (event) => {
      if (event.matches) close();
    });
  }

  function installCopyButtons() {
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-copy-text]");
      if (!button) return;
      const text = button.dataset.copyText || "";
      const markCopied = () => {
        const original = button.textContent;
        button.textContent = "Copied";
        button.disabled = true;
        window.setTimeout(() => {
          button.textContent = original;
          button.disabled = false;
        }, 1400);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(markCopied).catch(() => {});
        return;
      }
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand("copy");
        markCopied();
      } finally {
        textarea.remove();
      }
    });
  }

  installMenuSearch();
  installPageSearch();
  installNavigationDrawer();
  installCopyButtons();
})();
