class BiblioCommonsCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity && !config.entities) {
      throw new Error("entity or entities is required");
    }
    this.config = {
      title: "Library Books",
      allow_assignment: false,
      assignee_entity_id: "",
      show_history: true,
      columns: "auto",
      min_card_width: 220,
      library_card_number: "",
      ...config,
    };
    this._expandedLibraryBooks = new Set();
    this._expandedLibraryHistory = new Set();
    this._barcodeContext = null;
    this._syncing = false;
    this._reportContext = null;
    this._expandedReports = new Set();
    this._showApprovedReports = new Set();
    this._reportDrafts = new Map();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._reportContext && this.querySelector(".report-form")) {
      return;
    }
    this.render();
  }

  getCardSize() {
    return 4;
  }

  render() {
    if (!this.config || !this._hass) return;

    const assigneeFilter = this.personEntityId(this.config.assignee_entity_id);
    const groups = this.libraryGroups(assigneeFilter);

    this.innerHTML = `
      <ha-card>
        <div class="card">
          <div class="header">${this.escape(this.config.title)}</div>
          ${this.isFilteredCard() ? "" : this.reportQueueTemplate(groups)}
          ${
            groups.length
              ? groups.map((group) => this.libraryTemplate(group)).join("")
              : `<div class="empty">No library books to show.</div>`
          }
          ${this.reportDialogTemplate(groups)}
          ${this.fullscreenBarcodeTemplate()}
        </div>
      </ha-card>
      <style>
        .card {
          padding: 16px;
        }
        .header {
          font-size: 20px;
          font-weight: 600;
          margin-bottom: 12px;
        }
        .library {
          margin-top: 14px;
        }
        .library-barcode-card {
          display: grid;
          gap: 6px;
          margin-bottom: 10px;
          padding: 10px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--secondary-background-color);
        }
        .barcode-label {
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 650;
        }
        .barcode-svg {
          width: 100%;
          max-width: 460px;
          height: 86px;
          background: #fff;
          border-radius: 4px;
        }
        .barcode-actions {
          max-width: 460px;
        }
        .fullscreen-barcode-button {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          width: 100%;
          min-height: 36px;
          padding: 6px 10px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font-size: 13px;
          font-weight: 650;
          line-height: 1.2;
          text-align: center;
          text-decoration: none;
          cursor: pointer;
        }
        .fullscreen-barcode-button ha-icon {
          width: 18px;
          height: 18px;
          flex: 0 0 auto;
        }
        .fullscreen-barcode-button:disabled {
          cursor: not-allowed;
          opacity: 0.72;
        }
        .barcode-backdrop {
          position: fixed;
          inset: 0;
          z-index: 10000;
          display: grid;
          place-items: center;
          padding: 18px;
          background: #fff;
          color: #111;
        }
        .barcode-fullscreen {
          width: min(100%, 760px);
          display: grid;
          gap: 16px;
          justify-items: center;
        }
        .barcode-fullscreen-header {
          width: 100%;
          display: grid;
          grid-template-columns: 36px minmax(0, 1fr) 40px;
          align-items: center;
          gap: 10px;
        }
        .barcode-fullscreen-icon {
          width: 32px;
          height: 32px;
          object-fit: contain;
          border-radius: 6px;
        }
        .barcode-fullscreen-title {
          min-width: 0;
          font-size: 20px;
          font-weight: 750;
          text-align: center;
        }
        .barcode-close {
          width: 40px;
          height: 40px;
          border: 1px solid #ddd;
          border-radius: 999px;
          background: #fff;
          color: #111;
          cursor: pointer;
          display: grid;
          place-items: center;
        }
        .barcode-fullscreen .barcode-svg {
          width: 100%;
          max-width: none;
          height: auto;
          min-height: 170px;
          border: 1px solid #ddd;
        }
        .barcode-fullscreen-number {
          font-family: monospace;
          font-size: 22px;
          font-weight: 700;
          letter-spacing: 0;
        }
        .library:first-of-type {
          margin-top: 0;
        }
        .library-header {
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 15px;
          font-weight: 650;
          margin: 0 0 8px;
          color: var(--primary-text-color);
        }
        .library-name {
          min-width: 0;
          flex: 1 1 auto;
        }
        .library-counts {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          flex: 0 0 auto;
        }
        .count-badge {
          display: inline-grid;
          place-items: center;
          min-width: 22px;
          height: 22px;
          padding: 0 6px;
          border-radius: 999px;
          color: #fff;
          font-size: 12px;
          font-weight: 750;
          line-height: 1;
        }
        .count-badge.overdue {
          background: #d94141;
        }
        .count-badge.due-soon {
          background: #d2a019;
        }
        .count-badge.checked-out {
          background: #2b78d0;
        }
        .library-icon {
          width: 20px;
          height: 20px;
          border-radius: 4px;
          object-fit: contain;
        }
        .library-section-actions {
          display: grid;
          gap: 10px;
          margin: 0 0 12px;
        }
        .library-expander {
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          overflow: hidden;
        }
        .section-toggle {
          appearance: none;
          border: 0;
          border-bottom: 1px solid transparent;
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 6px;
          width: 100%;
          font: inherit;
          font-size: 14px;
          font-weight: 700;
          padding: 10px 12px;
          text-align: left;
        }
        .library-expander.open .section-toggle {
          border-bottom-color: var(--divider-color);
        }
        .section-toggle:hover {
          color: var(--primary-color);
        }
        .section-toggle ha-icon {
          width: 18px;
          height: 18px;
          flex: 0 0 auto;
        }
        .section-toggle span:last-child {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          color: var(--secondary-text-color);
        }
        .section-content {
          padding: 10px;
        }
        .books {
          display: grid;
          gap: 10px;
        }
        .book {
          display: grid;
          grid-template-columns: 64px 1fr auto;
          gap: 12px;
          min-height: 92px;
          padding: 10px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
        }
        .book.due-soon {
          background: color-mix(in srgb, #f8c537 24%, var(--card-background-color));
          border-color: color-mix(in srgb, #f8c537 60%, var(--divider-color));
        }
        .book.overdue {
          background: color-mix(in srgb, #d94141 22%, var(--card-background-color));
          border-color: color-mix(in srgb, #d94141 64%, var(--divider-color));
        }
        .cover {
          width: 64px;
          height: 92px;
          border-radius: 4px;
          object-fit: cover;
          background: var(--secondary-background-color);
          border: 1px solid var(--divider-color);
        }
        .cover-link {
          display: block;
          width: 64px;
          height: 92px;
        }
        .cover-placeholder {
          display: grid;
          place-items: center;
          color: var(--secondary-text-color);
          font-size: 28px;
        }
        .details {
          min-width: 0;
          display: grid;
          align-content: start;
          gap: 4px;
        }
        .book-actions {
          display: flex;
          align-items: center;
          align-self: start;
          gap: 8px;
          flex-wrap: wrap;
        }
        .report-button {
          width: 32px;
          height: 32px;
          display: inline-grid;
          place-items: center;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          cursor: pointer;
        }
        .report-button.done {
          border-color: var(--primary-color);
          color: var(--primary-color);
        }
        .reports-panel {
          grid-column: 1 / -1;
          display: grid;
          gap: 8px;
          margin-top: 4px;
          padding-top: 8px;
          border-top: 1px solid var(--divider-color);
        }
        .report-summary {
          display: grid;
          gap: 6px;
          padding: 8px;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          background: var(--secondary-background-color);
        }
        .report-queue {
          display: grid;
          gap: 8px;
          margin: 8px 0 12px;
        }
        .report-queue-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          color: var(--secondary-text-color);
          font-size: 13px;
          font-weight: 650;
        }
        .approved-toggle {
          padding: 5px 8px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          cursor: pointer;
        }
        .title {
          font-weight: 650;
          line-height: 1.25;
        }
        .title-link {
          color: var(--primary-text-color);
          text-decoration: none;
        }
        .title-link:hover {
          text-decoration: underline;
        }
        .meta,
        .status,
        .assigned {
          color: var(--secondary-text-color);
          font-size: 13px;
          line-height: 1.25;
        }
        .status {
          color: var(--primary-text-color);
        }
        select {
          width: 100%;
          max-width: 220px;
          margin-top: 4px;
          padding: 4px 6px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--card-background-color);
          color: var(--primary-text-color);
        }
        .assignment-menu {
          width: min(220px, 100%);
          margin-top: 4px;
          position: relative;
        }
        .assignment-menu summary {
          list-style: none;
          cursor: pointer;
          padding: 5px 8px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font-size: 13px;
        }
        .assignment-menu summary::-webkit-details-marker {
          display: none;
        }
        .assignment-options {
          position: absolute;
          z-index: 3;
          display: grid;
          gap: 4px;
          width: 100%;
          box-sizing: border-box;
          margin-top: 4px;
          padding: 8px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--card-background-color);
          box-shadow: var(--ha-card-box-shadow, 0 4px 12px rgba(0, 0, 0, 0.16));
        }
        .assignment-option {
          display: flex;
          align-items: center;
          gap: 6px;
          color: var(--primary-text-color);
          font-size: 13px;
        }
        .sync-button,
        .history-toggle {
          margin-top: 12px;
          padding: 6px 10px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          cursor: pointer;
        }
        .sync-button {
          flex: 0 0 auto;
          margin-top: 0;
        }
        .sync-button[disabled] {
          cursor: wait;
          opacity: 0.65;
        }
        .history {
          margin-top: 12px;
        }
        .history-list {
          display: grid;
          gap: 6px;
        }
        .history-item {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 2px;
          padding: 8px 0;
          border-top: 1px solid var(--divider-color);
        }
        .history-item .book-actions {
          grid-row: 1 / span 4;
          grid-column: 2;
          align-self: start;
        }
        .history-details {
          grid-column: 1;
          min-width: 0;
        }
        .empty {
          color: var(--secondary-text-color);
          padding: 8px 0;
        }
        .report-backdrop {
          position: fixed;
          inset: 0;
          z-index: 10;
          display: grid;
          place-items: center;
          padding: 16px;
          background: rgba(0, 0, 0, 0.42);
        }
        .report-dialog {
          width: min(680px, 100%);
          max-height: min(760px, 92vh);
          overflow: auto;
          padding: 16px;
          border-radius: 8px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          box-shadow: var(--ha-card-box-shadow, 0 8px 24px rgba(0, 0, 0, 0.24));
        }
        .report-title {
          font-size: 18px;
          font-weight: 650;
          margin-bottom: 4px;
        }
        .report-form {
          display: grid;
          gap: 12px;
          margin-top: 12px;
        }
        .report-field {
          display: grid;
          gap: 4px;
        }
        .report-label {
          font-weight: 650;
        }
        .report-help {
          color: var(--secondary-text-color);
          font-size: 12px;
          line-height: 1.35;
        }
        .report-form textarea,
        .report-form input,
        .report-form select {
          width: 100%;
          box-sizing: border-box;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--card-background-color);
          color: var(--primary-text-color);
          padding: 8px;
          font: inherit;
        }
        .report-form textarea {
          min-height: 84px;
          resize: vertical;
        }
        .report-actions {
          display: flex;
          justify-content: flex-end;
          gap: 8px;
        }
        .report-actions button {
          padding: 7px 12px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          cursor: pointer;
        }
        .report-actions .primary {
          background: var(--primary-color);
          border-color: var(--primary-color);
          color: var(--text-primary-color);
        }
        .report-answer {
          white-space: pre-wrap;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 8px;
          background: var(--secondary-background-color);
        }
        @media (max-width: 900px), (pointer: coarse) {
          .books {
            grid-template-columns: minmax(0, 1fr) !important;
          }
          .book {
            grid-template-columns: 64px 1fr auto;
          }
        }
      </style>
    `;

    this.querySelectorAll(".assignment-menu input[data-book-key]").forEach((checkbox) => {
      checkbox.addEventListener("change", (event) => {
        const target = event.currentTarget;
        const menu = target.closest(".assignment-menu");
        const assignees = Array.from(menu.querySelectorAll("input[data-book-key]:checked")).map((input) => input.value);
        const data = {
          book_key: target.dataset.bookKey,
          assignee: assignees,
        };
        if (target.dataset.entryId) {
          data.entry_id = target.dataset.entryId;
        }
        this._hass.callService("bibliocommons", "assign_book", data);
      });
    });

    this.querySelectorAll(".section-toggle[data-library-key][data-section]").forEach((button) => {
      button.addEventListener("click", (event) => {
        const target = event.currentTarget;
        this.toggleLibrarySection(target.dataset.libraryKey || "", target.dataset.section || "");
      });
    });

    this.querySelectorAll(".fullscreen-barcode-button[data-card-number]").forEach((button) => {
      button.addEventListener("click", (event) => {
        const target = event.currentTarget;
        this.openFullscreenBarcode({
          number: target.dataset.cardNumber || "",
          name: target.dataset.libraryName || "Library card",
          favicon: target.dataset.libraryFavicon || "",
        });
      });
    });

    const closeBarcode = this.querySelector(".barcode-close");
    if (closeBarcode) {
      closeBarcode.addEventListener("click", () => {
        this._barcodeContext = null;
        this.render();
      });
    }

    const barcodeBackdrop = this.querySelector(".barcode-backdrop");
    if (barcodeBackdrop) {
      barcodeBackdrop.addEventListener("click", (event) => {
        if (event.target === barcodeBackdrop) {
          this._barcodeContext = null;
          this.render();
        }
      });
    }

    this.querySelectorAll(".sync-button[data-entry-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        const entryId = event.currentTarget.dataset.entryId || "";
        this.syncBooks(entryId);
      });
    });

    this.querySelectorAll(".report-button[data-book-key]").forEach((button) => {
      button.addEventListener("click", (event) => {
        const dataset = event.currentTarget.dataset;
        if (dataset.mode === "expand") {
          this.toggleReports(dataset.bookKey || "");
        } else {
          this.openReportDialog(dataset);
        }
      });
    });

    this.querySelectorAll(".approved-toggle[data-report-group]").forEach((button) => {
      button.addEventListener("click", (event) => {
        this.toggleApprovedReports(event.currentTarget.dataset.reportGroup || "");
      });
    });

    const cancelReport = this.querySelector(".report-cancel");
    if (cancelReport) {
      cancelReport.addEventListener("click", () => {
        this.clearReportDraft();
        this._reportContext = null;
        this.render();
      });
    }

    const reportBackdrop = this.querySelector(".report-backdrop");
    if (reportBackdrop) {
      reportBackdrop.addEventListener("click", (event) => {
        if (event.target === reportBackdrop) {
          this.clearReportDraft();
          this._reportContext = null;
          this.render();
        }
      });
    }

    const reportForm = this.querySelector(".report-form");
    if (reportForm) {
      reportForm.addEventListener("input", () => this.saveReportDraft(reportForm));
      reportForm.addEventListener("change", () => this.saveReportDraft(reportForm));
      reportForm.addEventListener("submit", (event) => this.submitBookReport(event));
    }
  }

  async syncBooks(entryId) {
    if (this._syncing) return;
    this._syncing = entryId || true;
    this.render();

    try {
      if (entryId) {
        await this._hass.callService("bibliocommons", "sync_books", {
          entry_id: entryId,
        });
      } else {
        await this._hass.callService("bibliocommons", "sync_books", {});
      }
    } finally {
      this._syncing = false;
      this.render();
    }
  }

  libraryGroups(assigneeFilter) {
    return this.entityIds().map((entityId) => {
      const state = this._hass.states[entityId];
      const attrs = state?.attributes || {};
      const books = [...(attrs.books || [])].filter((book) => {
        const bookAssignees = this.bookAssignees(book);
        return !assigneeFilter || bookAssignees.includes(assigneeFilter);
      });
      const history = [...(attrs.reading_history || [])].filter((book) => {
        const bookAssignees = this.bookAssignees(book);
        return !assigneeFilter || bookAssignees.includes(assigneeFilter);
      });
      return {
        entityId,
        entryId: attrs.config_entry_id || "",
        name: attrs.library_name || state?.attributes?.friendly_name || entityId,
        url: attrs.library_url || "",
        favicon: attrs.library_favicon || "",
        cardNumber: attrs.library_card_number || "",
        assignees: attrs.assignees || [],
        books,
        history,
      };
    }).filter((group) => group.books.length || group.history.length || group.cardNumber);
  }

  entityIds() {
    if (Array.isArray(this.config.entities)) {
      return this.config.entities.filter((entityId) => typeof entityId === "string" && entityId);
    }
    return [this.config.entity];
  }

  libraryCardNumber(group) {
    if (group?.cardNumber) return `${group.cardNumber}`.trim();
    return `${this.config.library_card_number || ""}`.trim();
  }

  libraryKey(group) {
    return group?.entryId || group?.entityId || group?.url || group?.name || "library";
  }

  checkoutCounts(group) {
    return (group.books || []).reduce(
      (counts, book) => {
        const state = this.bookStateClass(book);
        if (state === "overdue") {
          counts.overdue += 1;
        } else if (state === "due-soon") {
          counts.dueSoon += 1;
        } else {
          counts.other += 1;
        }
        return counts;
      },
      { overdue: 0, dueSoon: 0, other: 0 },
    );
  }

  libraryCountBadges(counts) {
    const badges = [];
    if (counts.overdue) {
      badges.push(`<span class="count-badge overdue" title="${counts.overdue} overdue">${this.escape(counts.overdue)}</span>`);
    }
    if (counts.dueSoon) {
      badges.push(`<span class="count-badge due-soon" title="${counts.dueSoon} due soon">${this.escape(counts.dueSoon)}</span>`);
    }
    if (counts.other) {
      badges.push(`<span class="count-badge checked-out" title="${counts.other} other checked out">${this.escape(counts.other)}</span>`);
    }
    return badges.length ? `<span class="library-counts">${badges.join("")}</span>` : "";
  }

  libraryBarcodeTemplate(cardNumber) {
    const number = `${cardNumber || ""}`.trim();
    const barcode = number ? this.code39Svg(number) : "";
    return `
      <section class="library-barcode-card">
        <div class="barcode-label">Library card</div>
        ${
          barcode
            ? barcode
            : `<div class="empty">Library card number not available yet.</div>`
        }
      </section>
    `;
  }

  barcodeActionsTemplate(group) {
    const cardNumber = this.libraryCardNumber(group);
    const disabled = cardNumber ? "" : " disabled";
    return `
      <div class="barcode-actions">
        <button
          class="fullscreen-barcode-button"
          type="button"
          data-card-number="${this.escapeAttr(cardNumber)}"
          data-library-name="${this.escapeAttr(group.name || "Library card")}"
          data-library-favicon="${this.escapeAttr(group.favicon || "")}"
          ${disabled}
        ><ha-icon icon="mdi:barcode-scan"></ha-icon><span>Fullscreen barcode</span></button>
      </div>
    `;
  }

  fullscreenBarcodeTemplate() {
    if (!this._barcodeContext?.number) return "";
    const icon = this._barcodeContext.favicon
      ? `<img class="barcode-fullscreen-icon" src="${this.escapeAttr(this._barcodeContext.favicon)}" alt="">`
      : `<span></span>`;
    return `
      <div class="barcode-backdrop" role="dialog" aria-modal="true">
        <div class="barcode-fullscreen">
          <div class="barcode-fullscreen-header">
            ${icon}
            <div class="barcode-fullscreen-title">${this.escape(this._barcodeContext.name || "Library card")}</div>
            <button class="barcode-close" type="button" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          ${this.code39Svg(this._barcodeContext.number)}
          <div class="barcode-fullscreen-number">${this.escape(this._barcodeContext.number)}</div>
        </div>
      </div>
    `;
  }

  openFullscreenBarcode(context) {
    this._barcodeContext = context;
    this.render();
  }

  code39Svg(value) {
    const patterns = {
      "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn",
      "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw",
      "8": "wnnwnnwnn", "9": "nnwwnnwnn", "A": "wnnnnwnnw", "B": "nnwnnwnnw",
      "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn", "F": "nnwnwwnnn",
      "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
      "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww",
      "O": "wnnnwnnwn", "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn",
      "S": "nnwnnnwwn", "T": "nnnnwnwwn", "U": "wwnnnnnnw", "V": "nwwnnnnnw",
      "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn", "Z": "nwwnwnnnn",
      "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "$": "nwnwnwnnn",
      "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn", "*": "nwnnwnwnn",
    };
    const displayValue = `${value}`.trim();
    const data = `*${displayValue.toUpperCase().replace(/[^0-9A-Z .+$/%-]/g, "")}*`;
    const modules = [];
    for (const char of data) {
      const pattern = patterns[char];
      if (!pattern) continue;
      [...pattern].forEach((width, index) => {
        modules.push({ bar: index % 2 === 0, width: width === "w" ? 3 : 1 });
      });
      modules.push({ bar: false, width: 1 });
    }
    const totalWidth = modules.reduce((sum, module) => sum + module.width, 0);
    let x = 10;
    const rects = modules.map((module) => {
      const current = x;
      x += module.width;
      return module.bar
        ? `<rect x="${current}" y="8" width="${module.width}" height="54" fill="#111"></rect>`
        : "";
    }).join("");
    return `
      <svg class="barcode-svg" viewBox="0 0 ${totalWidth + 20} 88" role="img" aria-label="Library card barcode ${this.escapeAttr(displayValue)}">
        <rect x="0" y="0" width="${totalWidth + 20}" height="88" fill="#fff"></rect>
        ${rects}
        <text x="${(totalWidth + 20) / 2}" y="80" text-anchor="middle" font-family="monospace" font-size="13" fill="#111">${this.escape(displayValue)}</text>
      </svg>
    `;
  }

  libraryTemplate(group) {
    const libraryKey = this.libraryKey(group);
    const showBooks = this._expandedLibraryBooks.has(libraryKey);
    const showHistory = this._expandedLibraryHistory.has(libraryKey);
    const counts = this.checkoutCounts(group);
    const icon = group.favicon
      ? `<img class="library-icon" src="${this.escapeAttr(group.favicon)}" alt="">`
      : "";
    const name = group.url
      ? `<a class="title-link" href="${this.escapeAttr(group.url)}" target="_blank" rel="noopener noreferrer">${this.escape(group.name)}</a>`
      : this.escape(group.name);
    const countBadges = this.libraryCountBadges(counts);
    const syncButton = this.config.allow_assignment
      ? `<button class="sync-button" type="button" title="Sync ${this.escapeAttr(group.name)}" data-entry-id="${this.escapeAttr(group.entryId)}"${this._syncing === group.entryId || this._syncing === true ? " disabled" : ""}>
          ${this._syncing === group.entryId || this._syncing === true ? "Syncing..." : "Sync"}
        </button>`
      : "";
    const historyToggle = this.config.show_history
      ? this.libraryExpanderTemplate(
          libraryKey,
          "history",
          showHistory,
          `${showHistory ? "Hide" : "Show"} previously checked out books`,
          group.history.length ? `${group.history.length}` : "",
          showHistory ? this.historyListTemplate(group) : "",
        )
      : "";
    const activeBooks = `<div class="books" style="${this.gridStyle()}">
          ${
            group.books.length
              ? group.books.map((book) => this.bookTemplate(book, group.assignees, group.entryId)).join("")
              : `<div class="empty">No checked-out books for this library.</div>`
          }
        </div>`;
    const activeSection = this.libraryExpanderTemplate(
      libraryKey,
      "books",
      showBooks,
      `${showBooks ? "Hide" : "Show"} checked out books`,
      "",
      showBooks ? activeBooks : "",
    );

    return `
      <section class="library">
        <div class="library-header">${icon}<span class="library-name">${name}</span>${countBadges}${syncButton}</div>
        ${this.libraryBarcodeTemplate(this.libraryCardNumber(group))}
        ${this.barcodeActionsTemplate(group)}
        <div class="library-section-actions">
          ${activeSection}
          ${historyToggle}
        </div>
      </section>
    `;
  }

  libraryExpanderTemplate(libraryKey, section, open, title, trailing, content) {
    return `
      <section class="library-expander ${open ? "open" : ""}">
        <button class="section-toggle" type="button" data-library-key="${this.escapeAttr(libraryKey)}" data-section="${this.escapeAttr(section)}">
          <span>${this.escape(title)}</span>
          <span>${trailing ? this.escape(trailing) : ""}<ha-icon icon="${open ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon></span>
        </button>
        ${open ? `<div class="section-content">${content}</div>` : ""}
      </section>
    `;
  }

  reportQueueTemplate(groups) {
    const reports = this.groupBookReports(groups);
    const unapprovedReports = reports.filter((item) => !this.isApprovedReport(item.report));
    const approvedReports = reports.filter((item) => this.isApprovedReport(item.report));
    const groupKey = "all-book-reports";
    const showApproved = this._showApprovedReports.has(groupKey);
    const approvedButton = approvedReports.length
      ? `<button class="approved-toggle" type="button" data-report-group="${this.escapeAttr(groupKey)}">
          ${showApproved ? "Hide" : "Show"} ${approvedReports.length} approved
        </button>`
      : "";
    const unapprovedList = unapprovedReports.length
      ? unapprovedReports.map((item) => this.reportQueueItemTemplate(item)).join("")
      : `<div class="empty">No unapproved book reports.</div>`;
    const approvedList = showApproved
      ? `<div class="report-queue-header"><span>Approved book reports</span></div>
        ${approvedReports.map((item) => this.reportQueueItemTemplate(item)).join("")}`
      : "";

    return `
      <div class="report-queue">
        <div class="report-queue-header">
          <span>Unapproved book reports</span>
          ${approvedButton}
        </div>
        ${unapprovedList}
        ${approvedList}
      </div>
    `;
  }

  groupBookReports(groups) {
    return groups.flatMap((group) => {
      const books = [...group.books, ...group.history];
      return books.flatMap((book) =>
        (book.book_reports || []).map((report) => ({
          book,
          report,
          entryId: group.entryId,
          libraryName: group.name,
        })),
      );
    });
  }

  reportQueueItemTemplate(item) {
    const { book, report, entryId, libraryName } = item;
    const status = report.report_status ? `Status: ${report.report_status}` : "";
    const person = report.person_name || report.person_entity_id || "Unknown person";
    const context = [person, libraryName || ""].filter(Boolean).join(" · ");
    const screenTime = report.screen_time_minutes ? `Screen time: ${report.screen_time_minutes} minutes` : "";
    return `
      <div class="report-summary">
        <div class="title">${this.escape(book.title || "Unknown Title")}</div>
        <div class="meta">${this.escape(context)}</div>
        ${status || screenTime ? `<div class="meta">${this.escape([status, screenTime].filter(Boolean).join(" · "))}</div>` : ""}
        <div class="report-actions">
          <button
            type="button"
            class="report-button done"
            title="Review this report"
            data-entry-id="${this.escapeAttr(entryId || "")}"
            data-book-key="${this.escapeAttr(book.book_key || "")}"
            data-person="${this.escapeAttr(report.person_entity_id || "")}"
          ><ha-icon icon="mdi:book-edit"></ha-icon></button>
        </div>
      </div>
    `;
  }

  historyListTemplate(group) {
    if (!group.history.length) {
      return `<section class="history"><div class="empty">No previously checked out books for this library.</div></section>`;
    }

    return `
      <section class="history">
        <div class="history-list">
          ${group.history.map((book) => this.historyItemTemplate(book, group.assignees, group.entryId)).join("")}
        </div>
      </section>
    `;
  }

  historyItemTemplate(book, assignees, entryId) {
    const returned = this.formatReturnedAt(book.returned_at || "");
    const byline = [book.author || "", book.assignee_name ? `Read by ${book.assignee_name}` : ""]
      .filter(Boolean)
      .join(" · ");
    const readingLevel = [book.lexile_level || "", book.reading_level || ""].filter(Boolean).join(" · ");
    return `
      <div class="history-item">
        <div class="book-actions">${this.reportButtonTemplate(book, assignees, entryId)}</div>
        <div class="history-details">
          <div class="title">${this.escape(book.title || "Unknown Title")}</div>
          ${byline ? `<div class="meta">${this.escape(byline)}</div>` : ""}
          ${readingLevel ? `<div class="meta">Reading level: ${this.escape(readingLevel)}</div>` : ""}
          ${returned ? `<div class="meta">Returned ${this.escape(returned)}</div>` : ""}
        </div>
      </div>
    `;
  }

  bookTemplate(book, assignees, entryId) {
    const stateClass = this.bookStateClass(book);
    const bookUrl = book.book_url || "";
    const cover = book.cover_image
      ? `<img class="cover" src="${this.escapeAttr(book.cover_image)}" alt="">`
      : `<div class="cover cover-placeholder">BOOK</div>`;
    const linkedCover = bookUrl
      ? `<a class="cover-link" href="${this.escapeAttr(bookUrl)}" target="_blank" rel="noopener noreferrer">${cover}</a>`
      : cover;
    const title = bookUrl
      ? `<a class="title-link" href="${this.escapeAttr(bookUrl)}" target="_blank" rel="noopener noreferrer">${this.escape(book.title || "Unknown Title")}</a>`
      : this.escape(book.title || "Unknown Title");
    const readingLevel = [book.lexile_level || "", book.reading_level || ""].filter(Boolean).join(" · ");
    const assignment = this.config.allow_assignment
      ? this.assignmentTemplate(book, assignees, entryId)
      : book.assignee_name
        ? `<div class="assigned">${this.escape(book.assignee_name)}</div>`
        : "";
    const actions = this.reportButtonTemplate(book, assignees, entryId);
    const expandedReports = !this.isFilteredCard() && this._expandedReports.has(book.book_key)
      ? this.reportsPanelTemplate(book, entryId)
      : "";

    return `
      <div class="book ${stateClass}">
        ${linkedCover}
        <div class="details">
          <div class="title">${title}</div>
          <div class="meta">${this.escape(book.author || "")}</div>
          <div class="status">${this.escape(book.status || "Checked Out")} · ${this.escape(book.due_date || "")}</div>
          ${readingLevel ? `<div class="meta">Reading level: ${this.escape(readingLevel)}</div>` : ""}
          ${book.description ? `<div class="meta">${this.escape(book.description)}</div>` : ""}
          ${assignment}
        </div>
        <div class="book-actions">${actions}</div>
        ${expandedReports}
      </div>
    `;
  }

  reportButtonTemplate(book, assignees, entryId) {
    const person = this.defaultReportPerson(book);
    const report = person ? this.reportForPerson(book, person) : (book.book_reports || [])[0];
    const hasReport = Boolean(report);
    const reviewMode = !this.isFilteredCard();
    const reportCount = (book.book_reports || []).length;
    if (reviewMode) {
      return "";
    }
    return `
      <button
        class="report-button ${hasReport ? "done" : ""}"
        type="button"
        title="${reviewMode ? "Review book report" : hasReport ? "Edit book report" : "Add book report"}"
        data-entry-id="${this.escapeAttr(entryId || "")}"
        data-book-key="${this.escapeAttr(book.book_key || "")}"
        data-person="${this.escapeAttr((reviewMode ? report?.person_entity_id : person) || "")}"
      ><ha-icon icon="mdi:book-edit"></ha-icon></button>
    `;
  }

  reportsPanelTemplate(book, entryId) {
    const reports = book.book_reports || [];
    if (!reports.length) {
      return `<div class="reports-panel"><div class="empty">No book reports submitted.</div></div>`;
    }
    return `
      <div class="reports-panel">
        ${reports.map((report) => this.reportSummaryTemplate(book, report, entryId)).join("")}
      </div>
    `;
  }

  reportSummaryTemplate(book, report, entryId) {
    const status = report.report_status ? `Status: ${report.report_status}` : "";
    const screenTime = report.screen_time_minutes ? `Screen time: ${report.screen_time_minutes} minutes` : "";
    return `
      <div class="report-summary">
        <div class="title">${this.escape(report.person_name || report.person_entity_id || "Book report")}</div>
        ${status || screenTime ? `<div class="meta">${this.escape([status, screenTime].filter(Boolean).join(" · "))}</div>` : ""}
        <div class="report-actions">
          <button
            type="button"
            class="report-button done"
            title="Review this report"
            data-entry-id="${this.escapeAttr(entryId || "")}"
            data-book-key="${this.escapeAttr(book.book_key || "")}"
            data-person="${this.escapeAttr(report.person_entity_id || "")}"
          ><ha-icon icon="mdi:book-edit"></ha-icon></button>
        </div>
      </div>
    `;
  }

  gridStyle() {
    const columns = this.config.columns;
    const minWidth = Number(this.config.min_card_width) || 260;
    if (Number.isInteger(columns) && columns > 0) {
      return `grid-template-columns: repeat(${columns}, minmax(0, 1fr));`;
    }
    if (typeof columns === "string" && /^[1-9]\\d*$/.test(columns)) {
      return `grid-template-columns: repeat(${Number(columns)}, minmax(0, 1fr));`;
    }
    return `grid-template-columns: repeat(auto-fit, minmax(${minWidth}px, 1fr));`;
  }

  assignmentTemplate(book, assignees, entryId) {
    const selectedAssignees = new Set(this.bookAssignees(book));
    const selectedNames = (book.assignee_names || [])
      .filter(Boolean)
      .join(", ");
    const label = selectedNames || "Unassigned";
    const options = assignees
      .filter((assignee) => this.personEntityId(assignee.entity_id || assignee.value))
      .map((assignee) => {
        const value = this.personEntityId(assignee.entity_id || assignee.value);
        const name = assignee.name || value;
        const checked = selectedAssignees.has(value) ? " checked" : "";
        return `
          <label class="assignment-option">
            <input
              type="checkbox"
              value="${this.escapeAttr(value)}"
              data-book-key="${this.escapeAttr(book.book_key || "")}"
              data-entry-id="${this.escapeAttr(entryId || "")}"
              ${checked}
            >
            <span>${this.escape(name)}</span>
          </label>
        `;
      })
      .join("");

    return `
      <details class="assignment-menu">
        <summary>${this.escape(label)}</summary>
        <div class="assignment-options">${options}</div>
      </details>
    `;
  }

  reportDialogTemplate(groups) {
    if (!this._reportContext) return "";
    const book = this.findBookForReport(groups, this._reportContext.bookKey);
    if (!book) return "";
    if (!this.isFilteredCard()) {
      return this.reviewDialogTemplate(book);
    }
    const selectedPerson = this.personEntityId(this.config.assignee_entity_id);
    const report = {
      ...(this.reportForPerson(book, selectedPerson) || {}),
      ...this.reportDraft(this._reportContext.entryId || "", book.book_key || "", selectedPerson),
    };

    return `
      <div class="report-backdrop">
        <div class="report-dialog" role="dialog" aria-modal="true">
          <div class="report-title">${this.escape(book.title || "Book Report")}</div>
          <div class="meta">${this.escape(book.author || "")}</div>
          <form class="report-form">
            <input type="hidden" name="entry_id" value="${this.escapeAttr(this._reportContext.entryId || "")}">
            <input type="hidden" name="book_key" value="${this.escapeAttr(report.book_key || book.book_key || "")}">
            <input type="hidden" name="person_entity_id" value="${this.escapeAttr(selectedPerson)}">
            ${this.reportTextField(
              "main_conflict",
              "What is the main conflict of the story?",
              "What to answer: Describe the biggest problem the main character faces and explain exactly how they overcome it by the end of the book.",
              report.main_conflict || "",
            )}
            ${this.reportTextField(
              "character_change",
              "How does the main character change?",
              "What to answer: Pick the main character and describe their personality at the beginning of the book. Explain how their experiences and challenges cause them to grow or change by the end.",
              report.character_change || "",
            )}
            ${this.reportTextField(
              "theme",
              "What is the author's main message (theme)?",
              "What to answer: Think about what the author wants the reader to learn from reading this story. What is the hidden lesson or moral of the book?",
              report.theme || "",
            )}
            ${this.reportTextField(
              "recommendation",
              "Would you recommend this book to a friend?",
              "What to answer: State whether you liked or disliked the book. Provide specific reasons for your opinion and explain what type of person would enjoy reading it the most.",
              report.recommendation || "",
            )}
            <label class="report-field">
              <span class="report-label">How many hours did this take to read?</span>
              <input name="reading_hours" type="number" min="0" step="0.25" required value="${this.escapeAttr(this.readingHoursInputValue(report.minutes_reading))}">
            </label>
            <div class="report-actions">
              <button class="report-cancel" type="button">Cancel</button>
              <button class="primary" type="submit">Save</button>
            </div>
          </form>
        </div>
      </div>
    `;
  }

  reviewDialogTemplate(book) {
    const report = this.reportForPerson(book, this._reportContext.person) || (book.book_reports || [])[0];
    if (!report) {
      return `
        <div class="report-backdrop">
          <div class="report-dialog" role="dialog" aria-modal="true">
            <div class="report-title">${this.escape(book.title || "Book Report")}</div>
            <div class="empty">No book report has been submitted for this book.</div>
            <div class="report-actions">
              <button class="report-cancel" type="button">Close</button>
            </div>
          </div>
        </div>
      `;
    }
    return `
      <div class="report-backdrop">
        <div class="report-dialog" role="dialog" aria-modal="true">
          <div class="report-title">${this.escape(book.title || "Book Report")}</div>
          <div class="meta">${this.escape(report.person_name || report.person_entity_id || "")}</div>
          <form class="report-form review-form">
            <input type="hidden" name="entry_id" value="${this.escapeAttr(this._reportContext.entryId || "")}">
            <input type="hidden" name="book_key" value="${this.escapeAttr(report.book_key || book.book_key || "")}">
            <input type="hidden" name="person_entity_id" value="${this.escapeAttr(report.person_entity_id || "")}">
            ${this.reportReadOnlyAnswer("What is the main conflict of the story?", report.main_conflict || "")}
            ${this.reportReadOnlyAnswer("How does the main character change?", report.character_change || "")}
            ${this.reportReadOnlyAnswer("What is the author's main message (theme)?", report.theme || "")}
            ${this.reportReadOnlyAnswer("Would you recommend this book to a friend?", report.recommendation || "")}
            <div class="report-field">
              <span class="report-label">How many hours did this take to read?</span>
              <div class="report-answer">${this.escape(this.formatReadingHours(report.minutes_reading))}</div>
            </div>
            <label class="report-field">
              <span class="report-label">Screen time minutes</span>
              <input name="screen_time_minutes" type="number" min="0" step="1" value="${this.escapeAttr(report.screen_time_minutes || "")}">
            </label>
            <label class="report-field">
              <span class="report-label">Review note</span>
              <textarea name="review_note">${this.escape(report.review_note || "")}</textarea>
            </label>
            <div class="report-actions">
              <button class="report-cancel" type="button">Cancel</button>
              <button type="submit" data-status="redo">Return to redo</button>
              <button class="primary" type="submit" data-status="approved">Approve</button>
            </div>
          </form>
        </div>
      </div>
    `;
  }

  reportReadOnlyAnswer(label, value) {
    return `
      <div class="report-field">
        <span class="report-label">${this.escape(label)}</span>
        <div class="report-answer">${this.escape(value || "")}</div>
      </div>
    `;
  }

  reportTextField(name, label, help, value) {
    return `
      <label class="report-field">
        <span class="report-label">${this.escape(label)}</span>
        <span class="report-help">${this.escape(help)}</span>
        <textarea name="${this.escapeAttr(name)}" required>${this.escape(value)}</textarea>
      </label>
    `;
  }

  openReportDialog(dataset) {
    this._reportContext = {
      entryId: dataset.entryId || "",
      bookKey: dataset.bookKey || "",
      person: this.personEntityId(dataset.person || ""),
    };
    this.render();
  }

  reportDraftKey(entryId, bookKey, person) {
    return [entryId || "", bookKey || "", this.personEntityId(person || "")].join("|");
  }

  currentReportDraftKey() {
    if (!this._reportContext) return "";
    const person = this._reportContext.person || this.personEntityId(this.config.assignee_entity_id);
    return this.reportDraftKey(this._reportContext.entryId, this._reportContext.bookKey, person);
  }

  reportDraft(entryId, bookKey, person) {
    return this._reportDrafts.get(this.reportDraftKey(entryId, bookKey, person)) || {};
  }

  saveReportDraft(form) {
    const key = this.currentReportDraftKey();
    if (!key || form.classList.contains("review-form")) return;
    this._reportDrafts.set(key, Object.fromEntries(new FormData(form).entries()));
  }

  clearReportDraft() {
    const key = this.currentReportDraftKey();
    if (key) {
      this._reportDrafts.delete(key);
    }
  }

  toggleReports(bookKey) {
    if (!bookKey) return;
    if (this._expandedReports.has(bookKey)) {
      this._expandedReports.delete(bookKey);
    } else {
      this._expandedReports.add(bookKey);
    }
    this.render();
  }

  toggleApprovedReports(groupKey) {
    if (!groupKey) return;
    if (this._showApprovedReports.has(groupKey)) {
      this._showApprovedReports.delete(groupKey);
    } else {
      this._showApprovedReports.add(groupKey);
    }
    this.render();
  }

  toggleLibrarySection(libraryKey, section) {
    if (!libraryKey) return;
    const target = section === "history" ? this._expandedLibraryHistory : this._expandedLibraryBooks;
    if (target.has(libraryKey)) {
      target.delete(libraryKey);
    } else {
      target.add(libraryKey);
    }
    this.render();
  }

  isApprovedReport(report) {
    return `${report?.report_status || ""}`.toLowerCase() === "approved";
  }

  async submitBookReport(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form).entries());
    if (form.classList.contains("review-form")) {
      const submitter = event.submitter;
      await this._hass.callService("bibliocommons", "review_book_report", {
        entry_id: data.entry_id || "",
        book_key: data.book_key || "",
        person_entity_id: data.person_entity_id || "",
        report_status: submitter?.dataset?.status || "redo",
        screen_time_minutes: Number(data.screen_time_minutes || 0),
        review_note: data.review_note || "",
      });
      this.clearReportDraft();
      this._reportContext = null;
      this.render();
      return;
    }
    await this._hass.callService("bibliocommons", "submit_book_report", {
      entry_id: data.entry_id || "",
      book_key: data.book_key || "",
      person_entity_id: data.person_entity_id || "",
      main_conflict: data.main_conflict || "",
      character_change: data.character_change || "",
      theme: data.theme || "",
      recommendation: data.recommendation || "",
      minutes_reading: Math.round(Number(data.reading_hours || 0) * 60),
    });
    this.clearReportDraft();
    this._reportContext = null;
    this.render();
  }

  findBookForReport(groups, bookKey) {
    for (const group of groups) {
      const book = [...group.books, ...group.history].find((candidate) => candidate.book_key === bookKey);
      if (book) return book;
    }
    return null;
  }

  findAssigneesForReport(groups, entryId) {
    const group = groups.find((candidate) => candidate.entryId === entryId) || groups[0];
    return group?.assignees || [];
  }

  defaultReportPerson(book) {
    return this.personEntityId(this.config.assignee_entity_id) || this.bookAssignees(book)[0] || "";
  }

  bookAssignees(book) {
    const values = Array.isArray(book.assignee_entity_ids)
      ? book.assignee_entity_ids
      : Array.isArray(book.assignees)
        ? book.assignees
        : [book.assignee_entity_id || book.assignee || ""];
    const people = [];
    const seen = new Set();
    values.forEach((value) => {
      const person = this.personEntityId(value);
      if (person && !seen.has(person)) {
        seen.add(person);
        people.push(person);
      }
    });
    return people;
  }

  isFilteredCard() {
    return Boolean(this.personEntityId(this.config.assignee_entity_id));
  }

  reportForPerson(book, personEntityId) {
    const person = this.personEntityId(personEntityId);
    if (!person) return null;
    return (book.book_reports || []).find((report) => this.personEntityId(report.person_entity_id) === person) || null;
  }

  personEntityId(value) {
    const entityId = `${value || ""}`.trim();
    return entityId.startsWith("person.") ? entityId : "";
  }

  bookStateClass(book) {
    const status = `${book.status || ""}`.toLowerCase();
    if (book.overdue || status.includes("overdue")) return "overdue";
    const due = this.parseDueDate(book.due_date || "");
    if (!due) return "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diffDays = Math.ceil((due.getTime() - today.getTime()) / 86400000);
    return diffDays >= 0 && diffDays <= 3 ? "due-soon" : "";
  }

  parseDueDate(value) {
    const match = value.match(/([A-Za-z]+)\\s+(\\d{1,2}),\\s*(\\d{4})/);
    if (!match) return null;
    const parsed = new Date(`${match[1]} ${match[2]}, ${match[3]}`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  formatReturnedAt(value) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleDateString();
  }

  readingHoursInputValue(minutes) {
    const value = Number(minutes || 0);
    if (!value) return "";
    const hours = value / 60;
    return Number.isInteger(hours) ? `${hours}` : `${Math.round(hours * 100) / 100}`;
  }

  formatReadingHours(minutes) {
    const value = Number(minutes || 0);
    if (!value) return "0 hours";
    const hours = value / 60;
    const rounded = Number.isInteger(hours) ? hours : Math.round(hours * 100) / 100;
    return `${rounded} ${rounded === 1 ? "hour" : "hours"}`;
  }

  escape(value) {
    return `${value ?? ""}`.replace(/[&<>"']/g, (char) => {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char];
    });
  }

  escapeAttr(value) {
    return this.escape(value);
  }
}

if (!customElements.get("bibliocommons-card")) {
  customElements.define("bibliocommons-card", BiblioCommonsCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "bibliocommons-card",
  name: "BiblioCommons Card",
  description: "Display checked-out BiblioCommons books with due status and person assignment.",
});
