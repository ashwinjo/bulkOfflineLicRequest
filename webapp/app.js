(function () {
  const saver = document.getElementById("saver");

  function restHelp(id) {
    const node = document.getElementById(id);
    if (node && node.dataset.rest) node.textContent = node.dataset.rest;
    return node;
  }

  function renderLoading(ledger, label) {
    ledger.dataset.state = "loading";
    ledger.replaceChildren();
    const status = document.createElement("p");
    status.className = "ledger__status";
    status.setAttribute("role", "status");
    status.textContent = label;
    const rows = document.createElement("div");
    rows.className = "skeleton";
    rows.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 3; i += 1) {
      const row = document.createElement("div");
      row.className = "skeleton__row";
      rows.append(row);
    }
    ledger.append(status, rows);
  }

  function renderMessage(ledger, text, state) {
    ledger.dataset.state = state;
    ledger.replaceChildren();
    const status = document.createElement("p");
    status.className = "ledger__status";
    status.setAttribute("role", "status");
    status.textContent = text;
    ledger.append(status);
  }

  function renderResult(ledger, data, columns) {
    const rows = Array.isArray(data.results) ? data.results : [];
    ledger.dataset.state = rows.length ? "full" : "error";
    ledger.replaceChildren();

    const problems = Array.isArray(data.problems) ? data.problems : [];
    if (problems.length) {
      const list = document.createElement("ul");
      list.className = "problems";
      problems.forEach(function (problem) {
        const item = document.createElement("li");
        item.textContent = problem;
        list.append(item);
      });
      ledger.append(list);
    }

    if (rows.length) {
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      columns.forEach(function (column) {
        const cell = document.createElement("th");
        cell.scope = "col";
        cell.textContent = column.label;
        headRow.append(cell);
      });
      thead.append(headRow);
      const tbody = document.createElement("tbody");
      rows.forEach(function (row) {
        const line = document.createElement("tr");
        if (!row.ok) line.className = "row--minus";
        columns.forEach(function (column) {
          const cell = document.createElement("td");
          cell.dataset.label = column.label;
          cell.textContent = row[column.key] == null ? "" : String(row[column.key]);
          if (column.measured && (column.always || row.ok)) cell.className = "measured";
          line.append(cell);
        });
        tbody.append(line);
      });
      table.append(thead, tbody);
      ledger.append(table);
    }

    const head = document.createElement("div");
    head.className = "ledger__head";
    const status = document.createElement("p");
    status.className = "ledger__status";
    status.setAttribute("role", "status");
    status.textContent = data.status || "Failed";
    head.append(status);
    if (data.archive) {
      const link = document.createElement("a");
      link.className = "btn btn--quiet";
      link.href = data.archive;
      if (data.archiveName) link.setAttribute("download", data.archiveName);
      link.textContent = "Save zip";
      head.append(link);
      saver.src = data.archive;
    }
    ledger.append(head);
  }

  function bindForm(form, ledger, options) {
    const field = form.elements.crews;
    field.addEventListener("input", function () {
      field.removeAttribute("aria-invalid");
      const help = document.getElementById(field.getAttribute("aria-describedby"));
      if (help && help.dataset.rest) help.textContent = help.dataset.rest;
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const button = form.querySelector("button[type='submit']");
      const idle = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = options.busy;
      renderLoading(ledger, options.busy);
      try {
        const response = await fetch(form.action, options.request(form));
        let data = null;
        try {
          data = await response.json();
        } catch (error) {
          data = null;
        }
        if (!data || typeof data !== "object") {
          renderMessage(ledger, "The local server sent a response this page could not read.", "error");
          return;
        }
        if (!response.ok && !data.report) {
          renderMessage(ledger, data.detail || "The local server refused that request.", "error");
          return;
        }
        renderResult(ledger, data, options.columns);
        const problems = Array.isArray(data.problems) ? data.problems : [];
        if (problems.some(function (item) { return String(item).indexOf("Bad row") === 0; })) {
          field.setAttribute("aria-invalid", "true");
          field.focus();
        }
      } catch (error) {
        renderMessage(ledger, "This page could not reach the local server. Start it on this machine and try the button again.", "error");
      } finally {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = idle;
      }
    });
  }

  bindForm(document.getElementById("download-form"), document.getElementById("download-ledger"), {
    busy: "Downloading",
    columns: [
      { key: "address", label: "IP", measured: true, always: true },
      { key: "file", label: "offline req file name", measured: true, always: false }
    ],
    request: function (form) {
      return {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          crews: form.elements.crews.value,
          concurrency: form.elements.concurrency.value
        })
      };
    }
  });

  bindForm(document.getElementById("install-form"), document.getElementById("install-ledger"), {
    busy: "Installing",
    columns: [
      { key: "address", label: "IP", measured: true, always: true },
      { key: "file", label: "license filename", measured: true, always: false }
    ],
    request: function (form) {
      return {
        method: "POST",
        headers: { "Accept": "application/json" },
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          crews: form.elements.crews.value,
          license: form.elements.license.value
        })
      };
    }
  });

  document.getElementById("copy-list").addEventListener("click", function () {
    const source = document.getElementById("download-crews");
    const target = document.getElementById("install-crews");
    const help = document.getElementById("install-crews-help");
    if (!source.value.trim()) {
      help.textContent = "The download list is empty. Paste a chassis list there first.";
      source.focus();
      return;
    }
    target.value = source.value;
    help.textContent = help.dataset.rest;
    target.removeAttribute("aria-invalid");
    target.focus();
  });
}());
