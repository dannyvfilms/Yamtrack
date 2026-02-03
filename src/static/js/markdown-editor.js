(function () {
  "use strict";

  const SAFE_PROTOCOLS = ["http:", "https:", "mailto:"];

  const escapeHtml = (value) =>
    String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const sanitizeLink = (href) => {
    if (!href) {
      return null;
    }

    try {
      const url = new URL(href, window.location.origin);
      if (SAFE_PROTOCOLS.includes(url.protocol)) {
        return url.href;
      }
    } catch (error) {
      // Ignore invalid URLs and fall through to null.
    }

    return null;
  };

  const getRenderer = (() => {
    let renderer = null;
    return () => {
      if (!window.marked) {
        return null;
      }

      if (renderer) {
        return renderer;
      }

      renderer = new marked.Renderer();

      renderer.html = (html) => escapeHtml(html);
      renderer.heading = (text, level) =>
        `<h${level} class="text-sm font-semibold text-gray-100 mb-2">${text}</h${level}>`;
      renderer.paragraph = (text) => `<p class="text-sm text-gray-200 mb-2">${text}</p>`;
      renderer.list = (body, ordered) => {
        const listClass = ordered ? "list-decimal" : "list-disc";
        return `<${ordered ? "ol" : "ul"} class="${listClass} list-inside text-sm text-gray-200 mb-2 space-y-1">${body}</${ordered ? "ol" : "ul"}>`;
      };
      renderer.blockquote = (quote) =>
        `<blockquote class="border-l-2 border-gray-600 pl-3 italic text-gray-300 mb-2">${quote}</blockquote>`;
      renderer.code = (code) =>
        `<pre class="bg-[#1f2328] text-gray-200 text-xs rounded-md p-3 mb-2 overflow-x-auto"><code>${escapeHtml(code)}</code></pre>`;
      renderer.codespan = (code) =>
        `<code class="bg-[#1f2328] text-indigo-300 text-xs rounded px-1 py-0.5">${escapeHtml(code)}</code>`;
      renderer.hr = () => '<hr class="border-gray-700 my-3" />';
      renderer.link = (href, title, text) => {
        const safeHref = sanitizeLink(href);
        if (!safeHref) {
          return text;
        }
        const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
        return `<a class="text-indigo-300 hover:text-indigo-200 underline" href="${escapeHtml(safeHref)}" target="_blank" rel="noopener noreferrer"${titleAttr}>${text}</a>`;
      };
      renderer.image = (_href, _title, text) =>
        `<span class="text-gray-400">[image${text ? `: ${escapeHtml(text)}` : ""}]</span>`;

      marked.setOptions({
        gfm: true,
        breaks: true,
        mangle: false,
        headerIds: false,
      });

      return renderer;
    };
  })();

  const renderMarkdown = (input) => {
    const renderer = getRenderer();
    if (renderer && window.marked && typeof marked.parse === "function") {
      return marked.parse(input || "", { renderer });
    }

    return escapeHtml(input || "").replace(/\n/g, "<br>");
  };

  const setSelection = (textarea, start, end) => {
    textarea.setSelectionRange(start, end);
    textarea.focus();
  };

  const wrapSelection = (textarea, before, after, placeholder) => {
    const value = textarea.value;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const selection = value.slice(start, end) || placeholder;
    const nextValue = value.slice(0, start) + before + selection + after + value.slice(end);

    textarea.value = nextValue;
    const cursorStart = start + before.length;
    const cursorEnd = cursorStart + selection.length;
    setSelection(textarea, cursorStart, cursorEnd);
  };

  const prefixLines = (textarea, prefix, ordered = false) => {
    const value = textarea.value;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;

    const lineStart = value.lastIndexOf("\n", start - 1) + 1;
    const lineEndIndex = value.indexOf("\n", end);
    const lineEnd = lineEndIndex === -1 ? value.length : lineEndIndex;

    const block = value.slice(lineStart, lineEnd);
    const lines = block.split("\n");
    const nextLines = lines.map((line, index) => {
      if (!line.trim()) {
        return line;
      }
      const orderedPrefix = ordered ? `${index + 1}. ` : prefix;
      return `${orderedPrefix}${line}`;
    });

    const nextValue = value.slice(0, lineStart) + nextLines.join("\n") + value.slice(lineEnd);
    textarea.value = nextValue;

    const selectionStart = lineStart;
    const selectionEnd = lineStart + nextLines.join("\n").length;
    setSelection(textarea, selectionStart, selectionEnd);
  };

  const applyAction = (textarea, action) => {
    switch (action) {
      case "heading":
        prefixLines(textarea, "# ");
        break;
      case "bold":
        wrapSelection(textarea, "**", "**", "bold text");
        break;
      case "italic":
        wrapSelection(textarea, "*", "*", "italic text");
        break;
      case "code": {
        const selection = textarea.value.slice(textarea.selectionStart, textarea.selectionEnd);
        if (selection.includes("\n")) {
          wrapSelection(textarea, "```\n", "\n```", "code");
        } else {
          wrapSelection(textarea, "`", "`", "code");
        }
        break;
      }
      case "link": {
        const value = textarea.value;
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const selection = value.slice(start, end) || "link text";
        const url = "https://";
        const insertion = `[${selection}](${url})`;
        const nextValue = value.slice(0, start) + insertion + value.slice(end);

        textarea.value = nextValue;
        const urlStart = start + selection.length + 3;
        const urlEnd = urlStart + url.length;
        setSelection(textarea, urlStart, urlEnd);
        break;
      }
      case "quote":
        prefixLines(textarea, "> ");
        break;
      case "ul":
        prefixLines(textarea, "- ");
        break;
      case "ol":
        prefixLines(textarea, "", true);
        break;
      default:
        break;
    }
  };

  const setActiveTab = (editor, tab) => {
    const writePane = editor.querySelector('[data-md-pane="write"]');
    const previewPane = editor.querySelector('[data-md-pane="preview"]');
    const writeTab = editor.querySelector('[data-md-tab="write"]');
    const previewTab = editor.querySelector('[data-md-tab="preview"]');

    if (!writePane || !previewPane || !writeTab || !previewTab) {
      return;
    }

    const isPreview = tab === "preview";
    writePane.classList.toggle("hidden", isPreview);
    previewPane.classList.toggle("hidden", !isPreview);

    writeTab.classList.toggle("bg-[#2a2f35]", !isPreview);
    writeTab.classList.toggle("text-white", !isPreview);
    previewTab.classList.toggle("bg-[#2a2f35]", isPreview);
    previewTab.classList.toggle("text-white", isPreview);
  };

  const updatePreview = (editor) => {
    const textarea = editor.querySelector("textarea");
    const previewPane = editor.querySelector('[data-md-pane="preview"]');

    if (!textarea || !previewPane) {
      return;
    }

    if (!textarea.value.trim()) {
      previewPane.innerHTML = '<p class="text-sm text-gray-400">Nothing to preview yet.</p>';
      return;
    }

    previewPane.innerHTML = renderMarkdown(textarea.value);
  };

  const initEditor = (editor) => {
    if (!editor || editor.dataset.mdInitialized) {
      return;
    }

    const textarea = editor.querySelector("textarea");
    if (!textarea) {
      return;
    }

    editor.dataset.mdInitialized = "true";

    const writeTab = editor.querySelector('[data-md-tab="write"]');
    const previewTab = editor.querySelector('[data-md-tab="preview"]');
    const toolbarButtons = editor.querySelectorAll("[data-md-action]");

    if (writeTab) {
      writeTab.addEventListener("click", () => setActiveTab(editor, "write"));
    }

    if (previewTab) {
      previewTab.addEventListener("click", () => {
        updatePreview(editor);
        setActiveTab(editor, "preview");
      });
    }

    toolbarButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.getAttribute("data-md-action");
        if (!action) {
          return;
        }
        applyAction(textarea, action);
        updatePreview(editor);
      });
    });

    textarea.addEventListener("input", () => {
      const previewPane = editor.querySelector('[data-md-pane="preview"]');
      if (previewPane && !previewPane.classList.contains("hidden")) {
        updatePreview(editor);
      }
    });

    setActiveTab(editor, "write");
  };

  const initAll = (root) => {
    const scope = root || document;
    scope.querySelectorAll("[data-markdown-editor]").forEach(initEditor);
    scope.querySelectorAll("[data-markdown-preview]").forEach((preview) => {
      if (!preview || preview.dataset.mdRendered === "true") {
        return;
      }

      const sourceId = preview.getAttribute("data-markdown-source");
      if (!sourceId) {
        return;
      }

      const source = document.getElementById(sourceId);
      if (!source) {
        return;
      }

      let raw = "";
      try {
        raw = JSON.parse(source.textContent || "");
      } catch (error) {
        raw = source.textContent || "";
      }

      preview.innerHTML = renderMarkdown(raw);
      preview.dataset.mdRendered = "true";
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initAll(document));
  } else {
    initAll(document);
  }

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail && event.detail.elt ? event.detail.elt : event.target;
    initAll(target);
  });
})();
