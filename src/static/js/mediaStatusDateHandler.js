function trackModalCurrentTimeSegment(input) {
  if (input?.value && input.value.includes("T")) {
    return input.value.split("T")[1].slice(0, 5);
  }

  return new Date(new Date().getTime() - new Date().getTimezoneOffset() * 60000)
    .toISOString()
    .slice(11, 16);
}

function trackModalDispatchInputEvents(input) {
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function trackModalParseProgressMinutes(value) {
  if (!value) {
    return null;
  }

  const normalizedValue = value.trim().toLowerCase();
  if (!normalizedValue) {
    return null;
  }

  if (/^\d+(?:\.\d+)?$/.test(normalizedValue)) {
    const convertedValue = Number.parseFloat(normalizedValue);
    return Number.isFinite(convertedValue) && convertedValue >= 0
      ? Math.trunc(convertedValue * 60)
      : null;
  }

  if (normalizedValue.includes(":")) {
    const chunks = normalizedValue.split(":");
    if (chunks.length !== 2) {
      return null;
    }

    const [hoursString, minutesString] = chunks;
    if (!/^\d+$/.test(hoursString) || !/^\d+$/.test(minutesString)) {
      return null;
    }

    const minutes = Number.parseInt(minutesString, 10);
    if (minutes < 0 || minutes > 59) {
      return null;
    }

    return Number.parseInt(hoursString, 10) * 60 + minutes;
  }

  const unitPattern = /(\d+(?:\.\d+)?)\s*(hours?|hrs?|hr|h|minutes?|mins?|min)/g;
  const matches = [...normalizedValue.matchAll(unitPattern)];
  if (!matches.length) {
    return null;
  }

  if (normalizedValue.replace(unitPattern, "").trim()) {
    return null;
  }

  let totalMinutes = 0;
  for (const match of matches) {
    const amount = Number.parseFloat(match[1]);
    if (!Number.isFinite(amount)) {
      return null;
    }

    const unit = match[2];
    if (unit.startsWith("h") || unit.startsWith("hr")) {
      totalMinutes += amount * 60;
    } else {
      totalMinutes += amount;
    }
  }

  return Math.trunc(totalMinutes);
}

function trackModalClearAutoFilledField(form, fieldName) {
  if (!form || !window.Alpine) {
    return;
  }

  try {
    const data = Alpine.$data(form);
    if (data?.autoFilled && fieldName in data.autoFilled) {
      data.autoFilled[fieldName] = false;
    }
  } catch {
    // Ignore Alpine lookup failures and still update the DOM field value.
  }
}

function trackModalFormatLocalDateTime(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");

  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function trackModalParseLocalDateTime(value) {
  if (!value || !value.includes("T")) {
    return null;
  }

  const [datePart, timePart] = value.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hours, minutes] = timePart.split(":").map(Number);

  if ([year, month, day, hours, minutes].some(Number.isNaN)) {
    return null;
  }

  return new Date(year, month - 1, day, hours, minutes, 0, 0);
}

window.applyTrackModalReleaseDate = function applyTrackModalReleaseDate(
  button,
  releaseDate,
  fieldName,
  runtimeMinutes,
) {
  if (!button || !releaseDate) {
    return;
  }

  const container = button.closest(".relative");
  const input =
    container?.querySelector(`[name="${fieldName}"]`) ||
    container?.querySelector("input");
  if (!input) {
    return;
  }

  input.value =
    input.type === "datetime-local"
      ? `${releaseDate}T${trackModalCurrentTimeSegment(input)}`
      : releaseDate;

  const form = button.closest("form");
  trackModalClearAutoFilledField(form, fieldName);
  trackModalDispatchInputEvents(input);

  const parsedRuntimeMinutes = Number.parseInt(runtimeMinutes, 10);
  const startDateInput = form?.querySelector('[name="start_date"]');
  if (
    fieldName !== "end_date" ||
    !Number.isFinite(parsedRuntimeMinutes) ||
    parsedRuntimeMinutes <= 0 ||
    !startDateInput ||
    startDateInput === input ||
    input.type !== "datetime-local" ||
    startDateInput.type !== "datetime-local"
  ) {
    return;
  }

  const endDateTime = trackModalParseLocalDateTime(input.value);
  if (!endDateTime) {
    return;
  }

  startDateInput.value = trackModalFormatLocalDateTime(
    new Date(endDateTime.getTime() - parsedRuntimeMinutes * 60000),
  );
  trackModalClearAutoFilledField(form, "start_date");
  trackModalDispatchInputEvents(startDateInput);
};

function trackModalResolveElement(target) {
  if (typeof target === "string") {
    return document.getElementById(target);
  }

  if (target instanceof Element) {
    return target;
  }

  return null;
}

function trackModalGetStateKeyFromExpression(expression) {
  if (!expression) {
    return null;
  }

  if (expression.includes("createTrackOpen")) {
    return "createTrackOpen";
  }
  if (expression.includes("editTrackOpen")) {
    return "editTrackOpen";
  }
  if (expression.includes("trackOpen")) {
    return "trackOpen";
  }

  return null;
}

function trackModalFindStateTarget(target) {
  const element = trackModalResolveElement(target);
  if (!element || !window.Alpine) {
    return null;
  }

  let node = element;
  while (node) {
    if (node.hasAttribute?.("x-show")) {
      const stateKey = trackModalGetStateKeyFromExpression(
        node.getAttribute("x-show"),
      );
      if (stateKey) {
        let host = node;
        while (host && !host.hasAttribute?.("x-data")) {
          host = host.parentElement;
        }
        if (host) {
          return { host, stateKey };
        }
      }
    }
    node = node.parentElement;
  }

  node = element;
  while (node) {
    if (node.hasAttribute?.("x-data")) {
      try {
        const data = Alpine.$data(node);
        if (data) {
          if (Object.prototype.hasOwnProperty.call(data, "createTrackOpen")) {
            return { host: node, stateKey: "createTrackOpen" };
          }
          if (Object.prototype.hasOwnProperty.call(data, "editTrackOpen")) {
            return { host: node, stateKey: "editTrackOpen" };
          }
          if (Object.prototype.hasOwnProperty.call(data, "trackOpen")) {
            return { host: node, stateKey: "trackOpen" };
          }
        }
      } catch {
        // Ignore Alpine lookup failures and keep searching.
      }
    }
    node = node.parentElement;
  }

  return null;
}

function trackModalSetOpen(target, isOpen) {
  const stateTarget = trackModalFindStateTarget(target);
  if (!stateTarget || !window.Alpine) {
    return false;
  }

  try {
    const data = Alpine.$data(stateTarget.host);
    if (!data || !(stateTarget.stateKey in data)) {
      return false;
    }
    data[stateTarget.stateKey] = isOpen;
    return true;
  } catch {
    return false;
  }
}

function trackModalOpenWhenReady(formId, attempt = 0) {
  const form = document.getElementById(formId);
  if (form && trackModalSetOpen(form, true)) {
    return true;
  }

  if (attempt >= 10) {
    return false;
  }

  window.setTimeout(() => {
    trackModalOpenWhenReady(formId, attempt + 1);
  }, 25);

  return false;
}

function trackModalCloseAll() {
  document.querySelectorAll("[x-show]").forEach((node) => {
    if (node.querySelector("[data-track-modal-root]")) {
      trackModalSetOpen(node, false);
    }
  });
}

function trackModalToastOutlet() {
  const outlet = document.getElementById("htmx-toast-outlet");
  return outlet?.firstElementChild || outlet;
}

function trackModalCreateToast(detail) {
  const message = (detail && detail.message) || "";
  if (!message) {
    return null;
  }

  const type = (detail && detail.type) || "info";
  const toast = document.createElement("div");
  toast.className = `flex items-center gap-2 rounded-md border px-3 py-2 shadow-lg text-white toast-${type} opacity-0`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  toast.innerHTML = `
    <p class="text-sm font-medium flex-1"></p>
    <button type="button"
            class="text-current opacity-70 hover:opacity-100 transition-opacity cursor-pointer"
            aria-label="Dismiss notification">x</button>
  `;
  toast.querySelector("p").textContent = message;
  toast
    .querySelector("button")
    .addEventListener("click", () => toast.remove());

  toast.classList.add(
    "transition-all",
    "duration-300",
    "ease-out",
    "transform",
    "translate-y-[-1rem]",
  );
  window.setTimeout(() => {
    toast.classList.add("opacity-100", "transform-none");
  }, 10);

  const duration = Number.parseInt(detail.duration, 10);
  const timeout = Number.isFinite(duration)
    ? duration
    : type === "warning" || type === "error"
      ? 8000
      : 5000;
  window.setTimeout(() => toast.remove(), timeout);
  return toast;
}

window.clearTrackModalDate = function clearTrackModalDate(button, fieldName) {
  const input = button
    .closest(".relative")
    ?.querySelector(`[name="${fieldName}"]`);
  if (!input) {
    return;
  }
  input.value = "";
  const form = button.closest("form");
  trackModalClearAutoFilledField(form, fieldName);
  if (fieldName === "start_date" && form) {
    if (window.Alpine) {
      try {
        Alpine.$data(form).manualStartDate = false;
      } catch {
        // Ignore Alpine lookup failures.
      }
    }
    const sentinel = form.querySelector('[name="start_date_cleared"]');
    if (sentinel) {
      sentinel.value = "1";
    }
  }
  trackModalDispatchInputEvents(input);
};

window.closeTrackModal = function closeTrackModal(target) {
  return trackModalSetOpen(target, false);
};

window.openTrackModal = function openTrackModal(target) {
  return trackModalSetOpen(target, true);
};

window.showTrackToast = function showTrackToast(detail) {
  const outlet = trackModalToastOutlet();
  const toast = trackModalCreateToast(detail || {});
  if (!outlet || !toast) {
    return false;
  }

  outlet.appendChild(toast);
  return true;
};

function trackModalHandleClose(event) {
  const formId = event.detail?.formId;
  if (formId) {
    const form = document.getElementById(formId);
    if (form) {
      trackModalSetOpen(form, false);
    }
    return;
  }

  trackModalCloseAll();
}

function trackModalHandleOpen(event) {
  const formId = event.detail?.formId;
  if (!formId) {
    return;
  }

  trackModalOpenWhenReady(formId);
}

function trackModalHandleToast(event) {
  window.showTrackToast(event.detail || {});
}

document.addEventListener("closeModal", trackModalHandleClose);
document.addEventListener("openModal", trackModalHandleOpen);
document.addEventListener("showToast", trackModalHandleToast);

document.addEventListener("alpine:init", () => {
  Alpine.data("mediaForm", () => ({
    autoFilled: {
      start_date: false,
      end_date: false,
    },
    manualStartDate: false,
    // Track original values to detect intentionally empty dates
    original: {
      status: null,
      start_date: null,
      end_date: null,
    },

    syncStartDateFromProgress() {
      const progressField = this.$el.querySelector('[name="progress"]');
      const startDateField = this.$el.querySelector('[name="start_date"]');
      const endDateField = this.$el.querySelector('[name="end_date"]');

      if (
        !progressField ||
        progressField.type !== "text" ||
        !startDateField ||
        !endDateField ||
        this.manualStartDate
      ) {
        return;
      }

      const progressValue = progressField.value.trim();
      if (!progressValue) {
        if (this.autoFilled.start_date) {
          startDateField.value = "";
          this.autoFilled.start_date = false;
          trackModalDispatchInputEvents(startDateField);
        }
        return;
      }

      const progressMinutes = trackModalParseProgressMinutes(progressValue);
      if (progressMinutes === null || progressMinutes <= 0) {
        return;
      }

      const endDateTime = trackModalParseLocalDateTime(endDateField.value);
      if (!endDateTime) {
        return;
      }

      const nextStartDate = trackModalFormatLocalDateTime(
        new Date(endDateTime.getTime() - progressMinutes * 60000),
      );
      if (startDateField.value === nextStartDate) {
        return;
      }

      startDateField.value = nextStartDate;
      this.autoFilled.start_date = true;
      trackModalDispatchInputEvents(startDateField);
    },

    init() {
      const statusField = this.$el.querySelector('[name="status"]');
      const endDateField = this.$el.querySelector('[name="end_date"]');
      const startDateField = this.$el.querySelector('[name="start_date"]');
      const progressField = this.$el.querySelector('[name="progress"]');
      const instanceIdField = this.$el.querySelector('[name="instance_id"]');

      // Check if this is a new form (no instance_id) vs editing existing record
      const isNewForm = !instanceIdField || !instanceIdField.value;

      // Store original values for edit forms
      if (!isNewForm) {
        this.original.status = statusField?.value || null;
        this.original.start_date = startDateField?.value || null;
        this.original.end_date = endDateField?.value || null;
      }

      // Disable HTML5 validation on the form to prevent browser from blocking submission
      // We'll rely on Django backend validation instead
      const form = this.$el.tagName === "FORM" ? this.$el : this.$el.closest("form");
      if (form) {
        // Set novalidate attribute (Safari sometimes needs both the attribute and property)
        form.setAttribute("novalidate", "novalidate");
        form.noValidate = true;

        // Also explicitly remove required from date fields for Safari compatibility
        if (endDateField) {
          endDateField.removeAttribute("required");
          endDateField.required = false;
        }
        if (startDateField) {
          startDateField.removeAttribute("required");
          startDateField.required = false;
        }

        // Safari-specific: Also handle form submission to prevent validation
        form.addEventListener(
          "submit",
          () => {
            // Ensure novalidate is set right before submission
            form.setAttribute("novalidate", "novalidate");
            form.noValidate = true;

            // Remove required from date fields one more time
            if (endDateField) {
              endDateField.removeAttribute("required");
              endDateField.required = false;
            }
            if (startDateField) {
              startDateField.removeAttribute("required");
              startDateField.required = false;
            }
          },
          { capture: true },
        );
      }

      if (
        progressField &&
        progressField.type === "text" &&
        startDateField &&
        endDateField
      ) {
        const syncStartDateFromProgress = () => this.syncStartDateFromProgress();
        progressField.addEventListener("input", syncStartDateFromProgress);
        endDateField.addEventListener("input", syncStartDateFromProgress);
        startDateField.addEventListener("input", (event) => {
          if (!event.isTrusted) {
            return;
          }

          this.manualStartDate = Boolean(startDateField.value);
          if (this.manualStartDate) {
            this.autoFilled.start_date = false;
            const sentinel =
              this.$el.querySelector('[name="start_date_cleared"]') ??
              this.$el.closest("form")?.querySelector('[name="start_date_cleared"]');
            if (sentinel) {
              sentinel.value = "";
            }
          }
        });

        if (progressField.value.trim() && endDateField.value) {
          this.syncStartDateFromProgress();
        }
      }

      // Initial load handling - only auto-fill for new forms
      // For existing records, respect the saved values (even if empty)
      if (
        isNewForm &&
        statusField &&
        statusField.value === "Completed" &&
        endDateField &&
        !endDateField.value
      ) {
        endDateField.value = this.getCurrentDateTime(endDateField);
        this.autoFilled.end_date = true;
      } else if (
        isNewForm &&
        statusField &&
        statusField.value === "In progress" &&
        endDateField &&
        !endDateField.value
      ) {
        endDateField.value = this.getCurrentDateTime(endDateField);
        this.autoFilled.end_date = true;
      }
      if (isNewForm && statusField && statusField.value === "In progress") {
        this.syncStartDateFromProgress();
      }

      // Status change handler
      if (statusField) {
        statusField.addEventListener("change", (e) => {
          const status = e.target.value;

          // Clear previously auto-filled fields when status changes
          if (this.autoFilled.start_date && startDateField) {
            startDateField.value = "";
            this.autoFilled.start_date = false;
          }
          if (this.autoFilled.end_date && endDateField) {
            endDateField.value = "";
            this.autoFilled.end_date = false;
          }

          // For edit forms: don't auto-fill if returning to original status
          // where the date was intentionally left empty
          const isReturningToOriginalCompleted =
            status === "Completed" &&
            this.original.status === "Completed" &&
            this.original.end_date === null;

          const isReturningToOriginalInProgress =
            status === "In progress" &&
            this.original.status === "In progress" &&
            this.original.end_date === null;

          // Set new dates based on new status
          if (
            status === "Completed" &&
            endDateField &&
            !endDateField.value &&
            !isReturningToOriginalCompleted
          ) {
            endDateField.value = this.getCurrentDateTime(endDateField);
            this.autoFilled.end_date = true;
          } else if (
            status === "In progress" &&
            endDateField &&
            !endDateField.value &&
            !isReturningToOriginalInProgress
          ) {
            endDateField.value = this.getCurrentDateTime(endDateField);
            this.autoFilled.end_date = true;
          }

          if (status === "Completed" || status === "In progress") {
            this.syncStartDateFromProgress();
          }
        });
      }
    },

    getCurrentDateTime(field) {
      const date = new Date();

      if (field.type === "datetime-local") {
        return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
          .toISOString()
          .slice(0, 16);
      } else if (field.type === "date") {
        return date.toISOString().slice(0, 10);
      }

      // Fallback to date format
      return date.toISOString().slice(0, 10);
    },
  }));
});
