(() => {
    "use strict";

    const MODES = {
        thinking: {model: "gpt-5-6-thinking", effort: "max"},
        pro: {model: "gpt-5-6-pro", effort: "standard"},
    };
    const ALLOWED_IMAGE_TYPES = new Set(["image/gif", "image/jpeg", "image/png", "image/webp"]);
    const MAX_IMAGE_COUNT = 4;
    const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
    const MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024;
    const MAX_CLOUD_HISTORY_ATTEMPTS = 3;
    const RECOVERY_POLL_INTERVAL_MS = 5000;
    const RECOVERY_RATE_LIMIT_MIN_MS = 60000;
    const RECOVERY_RATE_LIMIT_MAX_MS = 120000;
    const RECOVERY_TOKEN_PATTERN = /^[a-f0-9]{32}$/;
    const RECOVERABLE_IMPORT_CODES = new Set([
        "cloud_history_pending",
        "remote_read_rate_limited",
        "remote_read_unavailable",
        "remote_status_unavailable",
        "remote_turn_running",
    ]);
    const RECOVERABLE_IMPORT_STATUSES = Object.freeze({
        pending: "cloud_history_pending",
        running: "remote_turn_running",
        unknown: "remote_status_unavailable",
    });

    const state = {
        projects: [],
        conversations: [],
        projectKey: "",
        selectedKey: "",
        selectedConversation: null,
        mode: "thinking",
        busy: false,
        sending: false,
        reconciliationRequired: false,
        recoveryUnresolved: false,
        controller: null,
        openController: null,
        toastTimer: null,
        attachments: [],
        followLatest: true,
    };

    const ui = {
        sessionState: document.getElementById("session-state"),
        sessionStateText: document.getElementById("session-state-text"),
        projectSelect: document.getElementById("project-select"),
        search: document.getElementById("conversation-search"),
        refresh: document.getElementById("refresh-project"),
        conversationCount: document.getElementById("conversation-count"),
        list: document.getElementById("conversation-list"),
        clearView: document.getElementById("clear-view"),
        sidebarOpen: document.getElementById("sidebar-open"),
        sidebarClose: document.getElementById("sidebar-close"),
        sidebarBackdrop: document.getElementById("sidebar-backdrop"),
        conversationProject: document.getElementById("conversation-project"),
        conversationTitle: document.getElementById("conversation-title"),
        modeOptions: Array.from(document.querySelectorAll(".mode-option")),
        empty: document.getElementById("empty-state"),
        emptyTitle: document.getElementById("empty-title"),
        emptyDetail: document.getElementById("empty-detail"),
        cancelLoad: document.getElementById("cancel-load"),
        retryLoad: document.getElementById("retry-load"),
        transcript: document.getElementById("transcript"),
        jumpLatest: document.getElementById("jump-latest"),
        composerRegion: document.getElementById("composer-region"),
        recoveryPanel: document.getElementById("recovery-panel"),
        recheckRecovery: document.getElementById("recheck-recovery"),
        openCloudChat: document.getElementById("open-cloud-chat"),
        adoptCurrentBranch: document.getElementById("adopt-current-branch"),
        recoveryDialog: document.getElementById("recovery-dialog"),
        cancelAdopt: document.getElementById("cancel-adopt"),
        confirmAdopt: document.getElementById("confirm-adopt"),
        form: document.getElementById("composer-form"),
        input: document.getElementById("message-input"),
        imageInput: document.getElementById("image-input"),
        attachmentTray: document.getElementById("attachment-tray"),
        attach: document.getElementById("attach-images"),
        send: document.getElementById("send-message"),
        stop: document.getElementById("stop-turn"),
        turnState: document.getElementById("turn-state"),
        toast: document.getElementById("toast"),
    };

    const requestJson = async (url, options = {}) => {
        const response = await fetch(url, {
            credentials: "same-origin",
            headers: {"Accept": "application/json", ...(options.headers || {})},
            ...options,
        });
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            payload = {};
        }
        if (!response.ok) {
            const error = new Error(payload?.error?.message || `Request failed (${response.status})`);
            error.code = payload?.error?.code || "";
            error.status = response.status;
            const retryAfter = Number.parseFloat(response.headers.get("Retry-After") || "");
            error.retryAfterSeconds = Number.isFinite(retryAfter) && retryAfter > 0
                ? retryAfter
                : 0;
            throw error;
        }
        return payload;
    };

    const setSessionState = (kind, message) => {
        ui.sessionState.classList.toggle("connected", kind === "connected");
        ui.sessionState.classList.toggle("error", kind === "error");
        ui.sessionStateText.textContent = message;
    };

    const setTurnState = (message, error = false, working = false, warning = false) => {
        ui.turnState.textContent = message;
        ui.turnState.classList.toggle("error", error);
        ui.turnState.classList.toggle("working", working);
        ui.turnState.classList.toggle("warning", warning);
    };

    const setConversationReadyState = (conversation) => {
        hideRecoveryState();
        const continuationFromTool = conversation?.advisorCloud?.continuationFromTool === true;
        setTurnState(
            continuationFromTool ? "Previous turn ended without a final answer" : "Ready",
            false,
            false,
            continuationFromTool,
        );
    };

    const hideRecoveryState = () => {
        state.recoveryUnresolved = false;
        ui.recoveryPanel.hidden = true;
        if (ui.recoveryDialog.open) ui.recoveryDialog.close();
    };

    const showUnresolvedRecovery = () => {
        state.recoveryUnresolved = true;
        state.reconciliationRequired = true;
        ui.recoveryPanel.hidden = false;
        ui.composerRegion.hidden = false;
        setTurnState("Cloud turn unresolved", false, false, true);
        updateComposerState();
    };

    const currentRecoveryToken = () => {
        const value = state.selectedConversation?.advisorCloud?.recoveryToken;
        return typeof value === "string" && RECOVERY_TOKEN_PATTERN.test(value) ? value : "";
    };

    const showToast = (message, error = false) => {
        window.clearTimeout(state.toastTimer);
        ui.toast.textContent = message;
        ui.toast.classList.toggle("error", error);
        ui.toast.hidden = false;
        state.toastTimer = window.setTimeout(() => {
            ui.toast.hidden = true;
        }, 5000);
    };

    const setEmptyState = (kind, title, detail = "", actions = {}) => {
        ui.empty.dataset.state = kind;
        ui.emptyTitle.textContent = title;
        ui.emptyDetail.textContent = detail;
        ui.emptyDetail.hidden = !detail;
        ui.cancelLoad.hidden = actions.cancel !== true;
        ui.retryLoad.hidden = actions.retry !== true;
    };

    const isRecoverableCloudReadError = (error) => (
        RECOVERABLE_IMPORT_CODES.has(error?.code) || error?.name === "TypeError"
    );

    const recoveryDelayForError = (error) => {
        if (error?.code === "remote_read_rate_limited") {
            const requested = Number(error?.retryAfterSeconds || 0) * 1000;
            return Math.min(
                RECOVERY_RATE_LIMIT_MAX_MS,
                Math.max(RECOVERY_RATE_LIMIT_MIN_MS, requested || 0),
            );
        }
        if (["remote_read_unavailable", "remote_status_unavailable"].includes(error?.code)
                || error?.name === "TypeError") {
            return RECOVERY_POLL_INTERVAL_MS;
        }
        return 0;
    };

    const showRecoveryProgress = (title, detail = "") => {
        if (!ui.transcript.hidden && state.selectedConversation) {
            setTurnState(title, false, true);
            return;
        }
        setEmptyState("loading", title, detail, {cancel: true});
        ui.empty.hidden = false;
    };

    const renderAttachmentTray = () => {
        ui.attachmentTray.replaceChildren();
        ui.attachmentTray.hidden = state.attachments.length === 0;
        for (const attachment of state.attachments) {
            const preview = document.createElement("div");
            preview.className = "attachment-preview";
            const image = document.createElement("img");
            image.src = attachment.url;
            image.alt = "";
            image.width = 72;
            image.height = 72;
            image.decoding = "async";
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "attachment-remove";
            remove.textContent = "×";
            remove.disabled = state.busy || state.sending || state.reconciliationRequired;
            remove.setAttribute("aria-label", `Remove ${attachment.file.name || "image"}`);
            remove.title = "Remove image";
            remove.addEventListener("click", () => {
                const index = state.attachments.findIndex((item) => item.id === attachment.id);
                if (index < 0 || state.sending) return;
                const [removed] = state.attachments.splice(index, 1);
                URL.revokeObjectURL(removed.url);
                renderAttachmentTray();
                updateComposerState();
                ui.input.focus({preventScroll: true});
            });
            preview.append(image, remove);
            ui.attachmentTray.appendChild(preview);
        }
    };

    const clearAttachments = (revoke = true) => {
        if (revoke) {
            state.attachments.forEach((attachment) => URL.revokeObjectURL(attachment.url));
        }
        state.attachments = [];
        ui.imageInput.value = "";
        renderAttachmentTray();
        updateComposerState();
    };

    const addImageFiles = (files) => {
        if (state.busy || state.sending || state.reconciliationRequired) return;
        let totalBytes = state.attachments.reduce((total, item) => total + item.file.size, 0);
        let errorMessage = "";
        for (const file of files) {
            if (!(file instanceof File) || !ALLOWED_IMAGE_TYPES.has(file.type)) {
                errorMessage = "Only JPEG, PNG, WebP, and GIF images are supported.";
                continue;
            }
            if (!file.size || file.size > MAX_IMAGE_BYTES) {
                errorMessage = "Each image must be 8 MiB or smaller.";
                continue;
            }
            if (state.attachments.length >= MAX_IMAGE_COUNT) {
                errorMessage = `You can attach up to ${MAX_IMAGE_COUNT} images.`;
                break;
            }
            if (totalBytes + file.size > MAX_TOTAL_IMAGE_BYTES) {
                errorMessage = "Attached images must total 20 MiB or less.";
                break;
            }
            state.attachments.push({
                id: crypto.randomUUID(),
                file,
                url: URL.createObjectURL(file),
            });
            totalBytes += file.size;
        }
        renderAttachmentTray();
        updateComposerState();
        if (errorMessage) showToast(errorMessage, true);
    };

    const setBusy = (busy) => {
        state.busy = busy;
        ui.list.setAttribute("aria-busy", String(busy));
        ui.projectSelect.disabled = busy || state.sending;
        ui.refresh.disabled = busy || state.sending;
        updateComposerState();
    };

    const updateComposerState = () => {
        const unavailable = !state.selectedKey || state.busy || state.sending || state.reconciliationRequired;
        ui.input.disabled = unavailable;
        ui.attach.disabled = unavailable;
        ui.send.disabled = unavailable || !ui.input.value.trim();
        ui.modeOptions.forEach((button) => { button.disabled = state.sending; });
        ui.recheckRecovery.disabled = state.busy || state.sending;
        ui.adoptCurrentBranch.disabled = state.busy || state.sending || !currentRecoveryToken();
        const observing = Boolean(state.openController && state.busy && !state.sending);
        ui.stop.hidden = !state.sending && !observing;
        ui.stop.setAttribute("aria-label", observing ? "Stop synchronization" : "Stop receiving");
        ui.stop.title = observing ? "Stop synchronization" : "Stop receiving";
        ui.attachmentTray.querySelectorAll("button").forEach((button) => {
            button.disabled = unavailable;
        });
    };

    const formatDate = (value) => {
        if (!value) return "";
        const number = Number(value);
        const date = new Date(number >= 1_000_000_000_000 ? number : number * 1000);
        if (Number.isNaN(date.getTime())) return "";
        return new Intl.DateTimeFormat(undefined, {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        }).format(date);
    };

    const renderLoadingRows = () => {
        ui.conversationCount.textContent = "…";
        ui.list.replaceChildren();
        for (let index = 0; index < 7; index += 1) {
            const row = document.createElement("div");
            row.className = "list-skeleton";
            row.setAttribute("aria-hidden", "true");
            ui.list.appendChild(row);
        }
    };

    const renderListState = (message) => {
        ui.conversationCount.textContent = "0";
        ui.list.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "list-state";
        empty.textContent = message;
        ui.list.appendChild(empty);
    };

    const LOCAL_KEY_PATTERN = /^[a-f0-9]{32}$/;

    const readLocationSelection = () => {
        const parameters = new URLSearchParams(location.hash.startsWith("#")
            ? location.hash.slice(1)
            : "");
        const projectKey = parameters.get("project") || "";
        const conversationKey = parameters.get("chat") || "";
        return {
            projectKey: LOCAL_KEY_PATTERN.test(projectKey) ? projectKey : "",
            conversationKey: LOCAL_KEY_PATTERN.test(conversationKey) ? conversationKey : "",
        };
    };

    const conversationLocation = (conversationKey) => {
        const parameters = new URLSearchParams({
            project: state.projectKey,
            chat: conversationKey,
        });
        return `${location.pathname}#${parameters.toString()}`;
    };

    const activeProject = () => state.projects.find((item) => item.key === state.projectKey) || null;

    const renderConversationList = () => {
        const query = ui.search.value.trim().toLocaleLowerCase();
        const matches = state.conversations.filter((item) => {
            const title = String(item.title || "").toLocaleLowerCase();
            return !query || title.includes(query);
        });
        ui.conversationCount.textContent = String(matches.length);
        ui.list.replaceChildren();

        for (const item of matches) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "conversation-button";
            button.classList.toggle("active", item.key === state.selectedKey);
            button.disabled = state.sending;
            button.setAttribute("aria-current", item.key === state.selectedKey ? "page" : "false");

            const copy = document.createElement("span");
            copy.className = "conversation-copy";
            const title = document.createElement("strong");
            title.textContent = item.title || "Untitled conversation";
            title.title = title.textContent;
            const updated = document.createElement("span");
            updated.textContent = formatDate(item.updatedAt || item.createdAt);
            copy.append(title, updated);
            button.appendChild(copy);

            if (item.needsRefresh) {
                const marker = document.createElement("span");
                marker.className = "conversation-refresh";
                marker.title = "Refresh required";
                marker.setAttribute("aria-label", "Refresh required");
                button.appendChild(marker);
            }

            button.addEventListener("click", () => openConversation(item.key));
            ui.list.appendChild(button);
        }

        if (!matches.length) {
            renderListState(query ? "No matching conversations" : "No conversations");
        }
    };

    const markConversationReconciled = (conversationKey) => {
        const item = state.conversations.find((entry) => entry.key === conversationKey);
        if (item) item.needsRefresh = false;
        renderConversationList();
    };

    const closeSidebar = () => {
        if (window.matchMedia("(max-width: 820px)").matches) {
            document.body.classList.remove("sidebar-visible", "sidebar-collapsed");
        } else {
            document.body.classList.add("sidebar-collapsed");
        }
        ui.sidebarOpen.focus({preventScroll: true});
    };

    const openSidebar = () => {
        document.body.classList.remove("sidebar-collapsed");
        if (window.matchMedia("(max-width: 820px)").matches) {
            document.body.classList.add("sidebar-visible");
        }
        ui.search.focus({preventScroll: true});
    };

    const clearView = () => {
        if (state.sending) return;
        clearAttachments();
        state.selectedKey = "";
        state.selectedConversation = null;
        state.reconciliationRequired = false;
        hideRecoveryState();
        ui.transcript.replaceChildren();
        ui.transcript.hidden = true;
        ui.jumpLatest.hidden = true;
        ui.composerRegion.hidden = true;
        ui.empty.hidden = false;
        setEmptyState("idle", "No conversation selected");
        ui.conversationProject.textContent = activeProject()?.name || "ChatGPT Project";
        ui.conversationTitle.textContent = "No conversation selected";
        history.replaceState(null, "", location.pathname);
        setTurnState("Ready");
        state.followLatest = true;
        renderConversationList();
        updateComposerState();
    };

    const showConversationPlaceholder = (item, message, kind = "loading", detail = "") => {
        hideRecoveryState();
        ui.transcript.replaceChildren();
        ui.transcript.hidden = true;
        ui.jumpLatest.hidden = true;
        ui.composerRegion.hidden = true;
        setEmptyState(kind, message, detail, {
            cancel: kind === "loading",
            retry: kind === "error",
        });
        ui.empty.hidden = false;
        ui.conversationProject.textContent = activeProject()?.name || "ChatGPT Project";
        ui.conversationTitle.textContent = item?.title || "Untitled conversation";
    };

    const loadProjects = async () => {
        setBusy(true);
        setSessionState("loading", "Connecting…");
        renderLoadingRows();
        try {
            const payload = await requestJson("/advisor-api/projects");
            state.projects = Array.isArray(payload.projects) ? payload.projects : [];
            ui.projectSelect.replaceChildren();
            for (const item of state.projects) {
                const option = document.createElement("option");
                option.value = item.key;
                option.textContent = item.name;
                ui.projectSelect.appendChild(option);
            }
            const locationSelection = readLocationSelection();
            state.projectKey = state.projects.some((item) => item.key === locationSelection.projectKey)
                ? locationSelection.projectKey
                : state.projects[0]?.key || "";
            ui.projectSelect.value = state.projectKey;
            setSessionState("connected", "ChatGPT connected");
            if (state.projectKey) {
                await refreshProject(true);
                if (locationSelection.conversationKey && !locationSelection.projectKey
                        && !state.selectedKey) {
                    for (const project of state.projects) {
                        if (project.key === state.projectKey) continue;
                        state.projectKey = project.key;
                        ui.projectSelect.value = project.key;
                        await refreshProject(true);
                        if (state.selectedKey) break;
                    }
                }
            } else {
                renderListState("No registered Projects");
            }
        } catch (error) {
            setSessionState("error", "ChatGPT disconnected");
            renderListState("Cloud conversations unavailable");
            showToast(error?.message || "Could not connect to ChatGPT", true);
        } finally {
            setBusy(false);
        }
    };

    const refreshProject = async (allowCurrentBusy = false) => {
        if (!state.projectKey || (!allowCurrentBusy && state.busy) || state.sending) return;
        setBusy(true);
        renderLoadingRows();
        try {
            const payload = await requestJson(
                `/advisor-api/projects/${encodeURIComponent(state.projectKey)}/refresh`,
                {
                    method: "POST",
                    headers: {"Content-Type": "application/json", "X-Advisor-Cloud": "1"},
                    body: "{}",
                },
            );
            state.conversations = Array.isArray(payload.conversations) ? payload.conversations : [];
            setSessionState("connected", "ChatGPT connected");
            renderConversationList();

            const {conversationKey} = readLocationSelection();
            if (!state.selectedKey && conversationKey
                    && state.conversations.some((item) => item.key === conversationKey)) {
                await openConversation(conversationKey, true);
            }
        } catch (error) {
            state.conversations = [];
            setSessionState("error", "ChatGPT disconnected");
            renderListState("Cloud conversations unavailable");
            showToast(error?.message || "Conversation refresh failed", true);
        } finally {
            setBusy(false);
        }
    };

    const MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML";
    const MATH_SYMBOLS = {
        alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", theta: "θ",
        lambda: "λ", mu: "μ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", phi: "φ",
        chi: "χ", psi: "ψ", omega: "ω", Gamma: "Γ", Delta: "Δ", Theta: "Θ",
        Lambda: "Λ", Pi: "Π", Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
        le: "≤", leq: "≤", ge: "≥", geq: "≥", neq: "≠", approx: "≈", equiv: "≡",
        cdot: "·", times: "×", pm: "±", mp: "∓", infty: "∞", to: "→",
        rightarrow: "→", leftarrow: "←", leftrightarrow: "↔", in: "∈", notin: "∉",
        subset: "⊂", subseteq: "⊆", supset: "⊃", supseteq: "⊇", cup: "∪", cap: "∩",
        land: "∧", lor: "∨", partial: "∂", nabla: "∇", sum: "∑", prod: "∏",
    };
    const MATH_OPERATORS = new Set(["min", "max", "log", "ln", "exp", "sin", "cos", "tan", "Pr"]);

    const mathNode = (name, text = null) => {
        const node = document.createElementNS(MATHML_NAMESPACE, name);
        if (text !== null) node.textContent = text;
        return node;
    };

    class MathParser {
        constructor(source) {
            this.source = source;
            this.index = 0;
        }

        skipSpace() {
            while (/\s/.test(this.source[this.index] || "")) this.index += 1;
        }

        parse(stop = "") {
            const row = mathNode("mrow");
            while (this.index < this.source.length) {
                this.skipSpace();
                if (!this.source[this.index] || (stop && this.source[this.index] === stop)) break;
                let base = this.parseAtom();
                if (!base) break;
                let subscript = null;
                let superscript = null;
                this.skipSpace();
                while (["_", "^"].includes(this.source[this.index])) {
                    const marker = this.source[this.index];
                    this.index += 1;
                    const script = this.parseScript();
                    if (marker === "_") subscript = script;
                    else superscript = script;
                    this.skipSpace();
                }
                if (subscript && superscript) {
                    const scripted = mathNode("msubsup");
                    scripted.append(base, subscript, superscript);
                    base = scripted;
                } else if (subscript) {
                    const scripted = mathNode("msub");
                    scripted.append(base, subscript);
                    base = scripted;
                } else if (superscript) {
                    const scripted = mathNode("msup");
                    scripted.append(base, superscript);
                    base = scripted;
                }
                row.appendChild(base);
            }
            if (stop && this.source[this.index] === stop) this.index += 1;
            return row;
        }

        parseScript() {
            this.skipSpace();
            if (this.source[this.index] === "{") {
                this.index += 1;
                return this.parse("}");
            }
            return this.parseAtom() || mathNode("mrow");
        }

        parseRequiredGroup() {
            this.skipSpace();
            if (this.source[this.index] === "{") {
                this.index += 1;
                return this.parse("}");
            }
            return this.parseAtom() || mathNode("mrow");
        }

        readRawGroup() {
            this.skipSpace();
            if (this.source[this.index] !== "{") return "";
            this.index += 1;
            const start = this.index;
            let depth = 1;
            while (this.index < this.source.length && depth) {
                if (this.source[this.index] === "{") depth += 1;
                if (this.source[this.index] === "}") depth -= 1;
                this.index += 1;
            }
            return this.source.slice(start, depth ? this.index : this.index - 1);
        }

        parseCommand() {
            this.index += 1;
            const start = this.index;
            while (/[A-Za-z]/.test(this.source[this.index] || "")) this.index += 1;
            const command = this.source.slice(start, this.index);
            if (!command) {
                const escaped = this.source[this.index] || "\\";
                this.index += escaped === "\\" ? 0 : 1;
                return mathNode("mo", escaped);
            }
            if (command === "frac") {
                const fraction = mathNode("mfrac");
                fraction.append(this.parseRequiredGroup(), this.parseRequiredGroup());
                return fraction;
            }
            if (command === "sqrt") {
                const root = mathNode("msqrt");
                root.appendChild(this.parseRequiredGroup());
                return root;
            }
            if (command === "text") return mathNode("mtext", this.readRawGroup());
            if (["mathrm", "mathbf", "mathit", "mathbb"].includes(command)) {
                const style = mathNode("mstyle");
                const variants = {
                    mathrm: "normal",
                    mathbf: "bold",
                    mathit: "italic",
                    mathbb: "double-struck",
                };
                style.setAttribute("mathvariant", variants[command]);
                style.appendChild(this.parseRequiredGroup());
                return style;
            }
            if (["left", "right"].includes(command)) return this.parseAtom();
            if (["quad", "qquad"].includes(command)) {
                const space = mathNode("mspace");
                space.setAttribute("width", command === "qquad" ? "2em" : "1em");
                return space;
            }
            if (Object.prototype.hasOwnProperty.call(MATH_SYMBOLS, command)) {
                const symbol = MATH_SYMBOLS[command];
                return mathNode(/[A-Za-zΑ-ω]/.test(symbol) ? "mi" : "mo", symbol);
            }
            if (MATH_OPERATORS.has(command)) {
                const operator = mathNode("mi", command);
                operator.setAttribute("mathvariant", "normal");
                return operator;
            }
            return mathNode("mtext", `\\${command}`);
        }

        parseAtom() {
            this.skipSpace();
            const character = this.source[this.index];
            if (!character) return null;
            if (character === "{") {
                this.index += 1;
                return this.parse("}");
            }
            if (character === "\\") return this.parseCommand();
            if (/[0-9.]/.test(character)) {
                const start = this.index;
                while (/[0-9.]/.test(this.source[this.index] || "")) this.index += 1;
                return mathNode("mn", this.source.slice(start, this.index));
            }
            if (/[A-Za-z]/.test(character)) {
                const start = this.index;
                while (/[A-Za-z]/.test(this.source[this.index] || "")) this.index += 1;
                return mathNode("mi", this.source.slice(start, this.index));
            }
            this.index += 1;
            return mathNode("mo", character);
        }
    }

    const createMath = (source, display = false) => {
        const wrapper = document.createElement(display ? "div" : "span");
        wrapper.className = display ? "math-display" : "math-inline";
        const normalized = String(source || "").trim();
        try {
            const math = mathNode("math");
            math.setAttribute("display", display ? "block" : "inline");
            math.setAttribute("aria-label", normalized);
            math.appendChild(new MathParser(normalized).parse());
            wrapper.appendChild(math);
        } catch (_error) {
            const fallback = document.createElement("code");
            fallback.className = "math-fallback";
            fallback.textContent = normalized;
            wrapper.appendChild(fallback);
        }
        return wrapper;
    };

    const appendInline = (target, text) => {
        const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\\\((?:(?!\\\)).)+\\\))/g;
        let cursor = 0;
        for (const match of text.matchAll(pattern)) {
            if (match.index > cursor) {
                target.appendChild(document.createTextNode(text.slice(cursor, match.index)));
            }
            const token = match[0];
            if (token.startsWith("`")) {
                const code = document.createElement("code");
                code.textContent = token.slice(1, -1);
                target.appendChild(code);
            } else if (token.startsWith("**")) {
                const strong = document.createElement("strong");
                strong.textContent = token.slice(2, -2);
                target.appendChild(strong);
            } else {
                target.appendChild(createMath(token.slice(2, -2)));
            }
            cursor = match.index + token.length;
        }
        if (cursor < text.length) {
            target.appendChild(document.createTextNode(text.slice(cursor)));
        }
    };

    const createCodeBlock = (language, codeText) => {
        const wrapper = document.createElement("div");
        wrapper.className = "code-block";
        const header = document.createElement("div");
        header.className = "code-header";
        const label = document.createElement("span");
        label.textContent = language || "code";
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "code-copy";
        copy.textContent = "Copy";
        copy.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(codeText);
                copy.textContent = "Copied";
                window.setTimeout(() => { copy.textContent = "Copy"; }, 1400);
            } catch (_error) {
                showToast("Clipboard unavailable", true);
            }
        });
        header.append(label, copy);
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = codeText;
        pre.appendChild(code);
        wrapper.append(header, pre);
        return wrapper;
    };

    const splitTableRow = (line) => {
        const source = String(line || "").trim();
        if (!source.includes("|")) return null;
        const cells = [];
        let current = "";
        let code = false;
        for (let index = 0; index < source.length; index += 1) {
            const character = source[index];
            if (character === "`" && source[index - 1] !== "\\") {
                code = !code;
                current += character;
                continue;
            }
            if (character === "\\" && source[index + 1] === "|") {
                current += "|";
                index += 1;
                continue;
            }
            if (character === "|" && !code) {
                cells.push(current.trim());
                current = "";
                continue;
            }
            current += character;
        }
        cells.push(current.trim());
        if (source.startsWith("|")) cells.shift();
        if (source.endsWith("|") && source[source.length - 2] !== "\\") cells.pop();
        return cells;
    };

    const tableAlignments = (line, width) => {
        const cells = splitTableRow(line);
        if (!cells || cells.length !== width) return null;
        const alignments = [];
        for (const cell of cells) {
            const marker = cell.trim();
            if (!/^:?-{3,}:?$/.test(marker)) return null;
            alignments.push(marker.startsWith(":") && marker.endsWith(":") ? "center"
                : (marker.endsWith(":") ? "right" : "left"));
        }
        return alignments;
    };

    const createTable = (lines, start) => {
        const headers = splitTableRow(lines[start]);
        if (!headers || !headers.length || start + 1 >= lines.length) return null;
        const alignments = tableAlignments(lines[start + 1], headers.length);
        if (!alignments) return null;

        const wrapper = document.createElement("div");
        wrapper.className = "markdown-table-scroll";
        wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "region");
        wrapper.setAttribute("aria-label", "Scrollable data table");
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const headerRow = document.createElement("tr");
        headers.forEach((content, column) => {
            const cell = document.createElement("th");
            cell.scope = "col";
            cell.className = `align-${alignments[column]}`;
            appendInline(cell, content);
            headerRow.appendChild(cell);
        });
        head.appendChild(headerRow);
        table.appendChild(head);

        const body = document.createElement("tbody");
        let index = start + 2;
        while (index < lines.length && lines[index].trim()) {
            const values = splitTableRow(lines[index]);
            if (!values) break;
            const row = document.createElement("tr");
            for (let column = 0; column < headers.length; column += 1) {
                const cell = document.createElement("td");
                cell.className = `align-${alignments[column]}`;
                appendInline(cell, values[column] || "");
                row.appendChild(cell);
            }
            body.appendChild(row);
            index += 1;
        }
        table.appendChild(body);
        wrapper.appendChild(table);
        return {element: wrapper, nextIndex: index};
    };

    const renderMessageText = (target, source) => {
        const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
        let index = 0;
        const startsTable = (at) => at + 1 < lines.length
            && Boolean(tableAlignments(lines[at + 1], splitTableRow(lines[at])?.length || 0));
        const startsBlock = (line, at) => /^(?:```|\\\[|\$\$|#{1,4}\s|[-*]\s|\d+\.\s|>\s)/.test(line.trimStart())
            || startsTable(at);

        while (index < lines.length) {
            const line = lines[index];
            if (!line.trim()) {
                index += 1;
                continue;
            }

            const trimmed = line.trim();
            const delimiter = trimmed.startsWith("\\[") ? ["\\[", "\\]"]
                : (trimmed.startsWith("$$") ? ["$$", "$$"] : null);
            if (delimiter) {
                const mathLines = [trimmed.slice(delimiter[0].length)];
                let closed = mathLines[0].includes(delimiter[1]);
                let cursor = index;
                while (!closed && cursor + 1 < lines.length) {
                    cursor += 1;
                    mathLines.push(lines[cursor]);
                    closed = lines[cursor].includes(delimiter[1]);
                }
                if (closed) {
                    const joined = mathLines.join("\n");
                    const end = joined.indexOf(delimiter[1]);
                    target.appendChild(createMath(joined.slice(0, end), true));
                    index = cursor + 1;
                    continue;
                }
            }

            const renderedTable = createTable(lines, index);
            if (renderedTable) {
                target.appendChild(renderedTable.element);
                index = renderedTable.nextIndex;
                continue;
            }

            const fence = line.match(/^```\s*([^\s`]*)/);
            if (fence) {
                const codeLines = [];
                index += 1;
                while (index < lines.length && !/^```\s*$/.test(lines[index])) {
                    codeLines.push(lines[index]);
                    index += 1;
                }
                if (index < lines.length) index += 1;
                target.appendChild(createCodeBlock(fence[1], codeLines.join("\n")));
                continue;
            }

            const heading = line.match(/^(#{1,4})\s+(.+)$/);
            if (heading) {
                const element = document.createElement(heading[1].length === 1 ? "h3" : "h4");
                appendInline(element, heading[2]);
                target.appendChild(element);
                index += 1;
                continue;
            }

            const unordered = line.match(/^[-*]\s+(.+)$/);
            const ordered = line.match(/^\d+\.\s+(.+)$/);
            if (unordered || ordered) {
                const list = document.createElement(unordered ? "ul" : "ol");
                const matcher = unordered ? /^[-*]\s+(.+)$/ : /^\d+\.\s+(.+)$/;
                while (index < lines.length) {
                    const itemMatch = lines[index].match(matcher);
                    if (!itemMatch) break;
                    const item = document.createElement("li");
                    appendInline(item, itemMatch[1]);
                    list.appendChild(item);
                    index += 1;
                }
                target.appendChild(list);
                continue;
            }

            if (/^>\s?/.test(line)) {
                const quoteLines = [];
                while (index < lines.length && /^>\s?/.test(lines[index])) {
                    quoteLines.push(lines[index].replace(/^>\s?/, ""));
                    index += 1;
                }
                const quote = document.createElement("blockquote");
                appendInline(quote, quoteLines.join("\n"));
                target.appendChild(quote);
                continue;
            }

            const paragraphLines = [line];
            index += 1;
            while (index < lines.length && lines[index].trim() && !startsBlock(lines[index], index)) {
                paragraphLines.push(lines[index]);
                index += 1;
            }
            const paragraph = document.createElement("p");
            appendInline(paragraph, paragraphLines.join("\n"));
            target.appendChild(paragraph);
        }
    };

    const createMessage = (role, content, options = {}) => {
        const streaming = Boolean(options.streaming);
        const images = Array.isArray(options.images) ? options.images : [];
        const imageCount = Number.isInteger(options.imageCount) ? Math.max(0, options.imageCount) : 0;
        const article = document.createElement("article");
        article.className = `message ${role === "assistant" ? "assistant" : "user"}`;
        article.classList.toggle("fresh", streaming || options.fresh === true);
        const avatar = document.createElement("div");
        avatar.className = "message-avatar";
        avatar.setAttribute("aria-hidden", "true");
        avatar.textContent = role === "assistant" ? "AI" : "YOU";
        const body = document.createElement("div");
        body.className = "message-body";
        const author = document.createElement("p");
        author.className = "message-author";
        author.textContent = role === "assistant" ? "ChatGPT" : "You";
        const attachments = document.createElement("div");
        attachments.className = "message-attachments";
        for (const attachment of images) {
            const image = document.createElement("img");
            image.src = attachment.url;
            image.alt = attachment.file.name || "Attached image";
            image.width = 180;
            image.height = 180;
            image.loading = "lazy";
            image.decoding = "async";
            attachments.appendChild(image);
        }
        if (!images.length && imageCount) {
            const summary = document.createElement("span");
            summary.className = "message-attachment-count";
            summary.textContent = `${imageCount} image${imageCount === 1 ? "" : "s"}`;
            attachments.appendChild(summary);
        }
        const messageContent = document.createElement("div");
        messageContent.className = "message-content";
        if (streaming) {
            const streamText = document.createElement("p");
            streamText.className = "stream-cursor";
            streamText.textContent = content;
            messageContent.appendChild(streamText);
        } else {
            renderMessageText(messageContent, content);
        }
        body.appendChild(author);
        if (images.length || imageCount) body.appendChild(attachments);
        body.appendChild(messageContent);
        article.append(avatar, body);
        return {article, content: messageContent};
    };

    const createActivity = (content, live = false) => {
        const row = document.createElement("div");
        row.className = "activity-row";
        row.classList.toggle("live", live);
        if (live) {
            row.setAttribute("role", "status");
            row.setAttribute("aria-live", "polite");
            row.setAttribute("aria-atomic", "true");
        }
        const icon = document.createElement("span");
        icon.className = "activity-icon";
        icon.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "activity-text";
        text.textContent = content;
        row.append(icon, text);
        return {row, text};
    };

    const completeActivity = (activity) => {
        if (!activity) return;
        activity.row.classList.remove("live");
        activity.row.removeAttribute("role");
        activity.row.removeAttribute("aria-live");
        activity.row.removeAttribute("aria-atomic");
    };

    const transcriptNearLatest = () => (
        ui.transcript.scrollHeight - ui.transcript.scrollTop - ui.transcript.clientHeight < 120
    );

    const updateLatestControl = () => {
        ui.jumpLatest.hidden = ui.transcript.hidden || transcriptNearLatest();
    };

    const scrollTranscript = (force = false) => {
        if (!force && !state.followLatest) {
            updateLatestControl();
            return;
        }
        ui.transcript.scrollTop = ui.transcript.scrollHeight;
        state.followLatest = true;
        updateLatestControl();
    };

    const renderTranscript = (conversation, live = false) => {
        const wasVisible = !ui.transcript.hidden;
        const previousScrollTop = ui.transcript.scrollTop;
        const shouldFollowLatest = !wasVisible || state.followLatest || transcriptNearLatest();
        ui.transcript.replaceChildren();
        const items = Array.isArray(conversation.items) ? conversation.items : [];
        const liveActivityIndex = live
            ? items.reduce(
                (latest, item, index) => (
                    item?.role === "assistant" && item?.kind === "activity" ? index : latest
                ),
                -1,
            )
            : -1;
        for (const [index, item] of items.entries()) {
            if (!item || !["user", "assistant"].includes(item.role)) continue;
            if (item.role === "assistant" && item.kind === "activity") {
                ui.transcript.appendChild(createActivity(item.content, index === liveActivityIndex).row);
            } else {
                ui.transcript.appendChild(createMessage(
                    item.role,
                    item.content,
                    {imageCount: item.imageCount},
                ).article);
            }
        }
        ui.empty.hidden = true;
        ui.transcript.hidden = false;
        ui.composerRegion.hidden = false;
        requestAnimationFrame(() => {
            if (shouldFollowLatest) {
                scrollTranscript(true);
            } else {
                ui.transcript.scrollTop = previousScrollTop;
                state.followLatest = false;
                updateLatestControl();
            }
        });
    };

    const importConversation = async (conversationKey, signal = undefined) => {
        const payload = await requestJson(
            `/advisor-api/projects/${encodeURIComponent(state.projectKey)}`
            + `/conversations/${encodeURIComponent(conversationKey)}/import`,
            {
            method: "POST",
            signal,
            headers: {"Content-Type": "application/json", "X-Advisor-Cloud": "1"},
            body: "{}",
            },
        );
        if (payload?.status === "unresolved") {
            const error = new Error(payload.message || "Cloud turn unresolved");
            error.code = "cloud_history_unresolved";
            throw error;
        }
        const recoveryCode = RECOVERABLE_IMPORT_STATUSES[payload?.status];
        if (recoveryCode) {
            const error = new Error(payload.message || "Cloud conversation reconciliation is pending");
            error.code = recoveryCode;
            throw error;
        }
        return payload;
    };

    const observeConversation = async (conversationKey, signal = undefined) => requestJson(
        `/advisor-api/projects/${encodeURIComponent(state.projectKey)}/conversations/${encodeURIComponent(conversationKey)}/observe`,
        {signal},
    );

    const waitForRecoveryPoll = (milliseconds, signal = undefined) => new Promise((resolve, reject) => {
        if (signal?.aborted) {
            reject(new DOMException("Cloud conversation loading was cancelled", "AbortError"));
            return;
        }
        const finish = () => {
            window.clearTimeout(timer);
            signal?.removeEventListener("abort", cancel);
            resolve();
        };
        const cancel = () => {
            window.clearTimeout(timer);
            reject(new DOMException("Cloud conversation loading was cancelled", "AbortError"));
        };
        const timer = window.setTimeout(finish, milliseconds);
        signal?.addEventListener("abort", cancel, {once: true});
    });

    const recoverConversationUntilReady = async (
        conversationKey,
        {renderSnapshots = false, observeFirst = true, signal = undefined} = {},
    ) => {
        const projectKey = state.projectKey;
        const ensureCurrentRecovery = () => {
            if (signal?.aborted
                    || state.projectKey !== projectKey
                    || state.selectedKey !== conversationKey) {
                throw new DOMException("Cloud conversation loading was cancelled", "AbortError");
            }
        };
        let shouldImport = !observeFirst;
        let cloudHistoryAttempts = 0;
        while (state.projectKey === projectKey && state.selectedKey === conversationKey) {
            ensureCurrentRecovery();
            if (shouldImport) {
                try {
                    const payload = await importConversation(conversationKey, signal);
                    ensureCurrentRecovery();
                    return payload;
                } catch (error) {
                    if (error?.name === "AbortError") throw error;
                    if (error?.code === "cloud_history_unresolved") throw error;
                    if (!isRecoverableCloudReadError(error)) throw error;
                    if (error?.code === "cloud_history_pending") {
                        cloudHistoryAttempts += 1;
                    }
                    const delayMs = recoveryDelayForError(error);
                    if (delayMs) {
                        shouldImport = false;
                        const rateLimited = error?.code === "remote_read_rate_limited";
                        showRecoveryProgress(
                            rateLimited ? "ChatGPT is rate limited" : "Cloud connection interrupted",
                            rateLimited
                                ? `Retrying read-only synchronization in ${Math.ceil(delayMs / 1000)} seconds…`
                                : "Retrying read-only synchronization…",
                        );
                        await waitForRecoveryPoll(delayMs, signal);
                        continue;
                    }
                }
            }

            let observed;
            try {
                observed = await observeConversation(conversationKey, signal);
                ensureCurrentRecovery();
            } catch (error) {
                if (error?.name === "AbortError") throw error;
                if (!isRecoverableCloudReadError(error)) throw error;
                const delayMs = recoveryDelayForError(error) || RECOVERY_POLL_INTERVAL_MS;
                const rateLimited = error?.code === "remote_read_rate_limited";
                showRecoveryProgress(
                    rateLimited ? "ChatGPT is rate limited" : "Cloud connection interrupted",
                    rateLimited
                        ? `Retrying read-only synchronization in ${Math.ceil(delayMs / 1000)} seconds…`
                        : "Retrying read-only synchronization…",
                );
                await waitForRecoveryPoll(delayMs, signal);
                shouldImport = false;
                continue;
            }
            const conversation = observed?.conversation;
            if (renderSnapshots && conversation?.id && Array.isArray(conversation.items)) {
                state.selectedConversation = conversation;
                renderTranscript(conversation, observed?.status === "streaming");
                updateComposerState();
            }

            if (observed?.status === "ready") {
                shouldImport = true;
                continue;
            }
            if (observed?.status === "complete") {
                if (cloudHistoryAttempts >= MAX_CLOUD_HISTORY_ATTEMPTS) {
                    const error = new Error(
                        "ChatGPT finished, but the interrupted local send cannot be matched to the current cloud branch.",
                    );
                    error.code = "cloud_history_unresolved";
                    throw error;
                }
                shouldImport = true;
                showRecoveryProgress("Synchronizing cloud history…");
                await waitForRecoveryPoll(1000, signal);
                continue;
            }

            shouldImport = false;
            showRecoveryProgress(
                observed?.status === "streaming"
                    ? "ChatGPT is still working…"
                    : "Connection interrupted; checking ChatGPT…",
            );
            await waitForRecoveryPoll(RECOVERY_POLL_INTERVAL_MS, signal);
        }
        throw new DOMException("Cloud conversation loading was cancelled", "AbortError");
    };

    const waitForActivityPoll = (signal, delayMs = 5000) => new Promise((resolve) => {
        if (signal.aborted) {
            resolve();
            return;
        }
        const finish = () => {
            window.clearTimeout(timer);
            signal.removeEventListener("abort", finish);
            resolve();
        };
        const timer = window.setTimeout(finish, delayMs);
        signal.addEventListener("abort", finish, {once: true});
    });

    const pollConversationActivity = async (conversationKey, activityToken, signal, onActivity) => {
        while (!signal.aborted) {
            let delayMs = 5000;
            try {
                const payload = await requestJson(
                    `/advisor-api/projects/${encodeURIComponent(state.projectKey)}`
                    + `/conversations/${encodeURIComponent(conversationKey)}/activity`,
                    {
                        signal,
                        headers: {"X-Advisor-Activity-Token": activityToken},
                    },
                );
                const activities = Array.isArray(payload.activities) ? payload.activities : [];
                activities.forEach((activity) => {
                    if (typeof activity === "string") onActivity(activity);
                });
            } catch (error) {
                if (error?.name === "AbortError" || signal.aborted) return;
                // The primary stream remains authoritative; retry read-only
                // activity history quietly after a transient polling failure.
                if (error?.status === 429 || error?.code === "activity_rate_limited") {
                    const retryAfterMs = Number(error?.retryAfterSeconds || 0) * 1000;
                    delayMs = Math.max(60000, retryAfterMs);
                }
            }
            await waitForActivityPoll(signal, delayMs);
        }
    };

    const openConversation = async (conversationKey, allowCurrentBusy = false) => {
        if (!conversationKey || state.sending || (!allowCurrentBusy && state.busy)) return;
        const item = state.conversations.find((entry) => entry.key === conversationKey);
        if (!item) return;

        if (state.selectedKey && state.selectedKey !== conversationKey) clearAttachments();
        state.selectedKey = conversationKey;
        state.reconciliationRequired = false;
        state.followLatest = true;
        history.replaceState(null, "", conversationLocation(conversationKey));
        const openController = new AbortController();
        state.openController = openController;
        renderConversationList();
        setBusy(true);
        const recovering = Boolean(item.needsRefresh);
        showConversationPlaceholder(
            item,
            recovering ? "Recovering cloud conversation" : "Loading cloud conversation",
            "loading",
            recovering ? "Waiting for the active ChatGPT turn to settle…" : "Synchronizing visible cloud history…",
        );
        setTurnState(
            recovering ? "Waiting for ChatGPT to finish…" : "Refreshing cloud conversation…",
            false,
            true,
        );
        try {
            let payload;
            if (recovering) {
                payload = await recoverConversationUntilReady(
                    conversationKey,
                    {renderSnapshots: true, observeFirst: true, signal: openController.signal},
                );
            } else {
                try {
                    payload = await importConversation(conversationKey, openController.signal);
                } catch (error) {
                    if (!isRecoverableCloudReadError(error)) throw error;
                    payload = await recoverConversationUntilReady(
                        conversationKey,
                        {renderSnapshots: true, observeFirst: true, signal: openController.signal},
                    );
                }
            }
            const conversation = payload.conversation;
            if (!conversation?.id || !Array.isArray(conversation.items)) {
                throw new Error("The cloud conversation returned an invalid response");
            }
            state.selectedConversation = conversation;
            markConversationReconciled(conversationKey);
            ui.conversationProject.textContent = activeProject()?.name || "ChatGPT Project";
            ui.conversationTitle.textContent = conversation.title || item.title || "Untitled conversation";
            renderTranscript(conversation);
            if (conversation.recoveredSubmission) clearAttachments();
            setConversationReadyState(conversation);
            if (window.matchMedia("(max-width: 820px)").matches) closeSidebar();
        } catch (error) {
            if (error?.name === "AbortError") return;
            if (error?.code === "cloud_history_unresolved" && state.selectedConversation) {
                showUnresolvedRecovery();
                showToast("Cloud history needs a recovery decision", true);
                return;
            }
            state.selectedConversation = null;
            state.reconciliationRequired = true;
            showConversationPlaceholder(
                item,
                "Refresh required",
                "error",
                "The cloud turn may still be active. Retry performs a read-only reconciliation first.",
            );
            setTurnState("Refresh required", true);
            showToast(error?.message || "Could not open conversation", true);
        } finally {
            if (state.openController === openController) state.openController = null;
            setBusy(false);
            updateComposerState();
        }
    };

    const parseSseEvent = (eventText) => {
        const data = eventText
            .split(/\r?\n/)
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\n");
        if (!data) return null;
        return JSON.parse(data);
    };

    const consumeStream = async (response, onEvent) => {
        if (!response.body) throw new Error("Streaming response unavailable");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let sawFinish = false;

        while (true) {
            const {done, value} = await reader.read();
            buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
            let boundary = buffer.match(/\r?\n\r?\n/);
            while (boundary && boundary.index !== undefined) {
                const eventText = buffer.slice(0, boundary.index);
                buffer = buffer.slice(boundary.index + boundary[0].length);
                const payload = parseSseEvent(eventText);
                if (payload) {
                    onEvent(payload);
                    if (payload.type === "finish") sawFinish = true;
                }
                boundary = buffer.match(/\r?\n\r?\n/);
            }
            if (done) break;
        }
        if (!sawFinish) throw new Error("The cloud turn ended before completion");
    };

    const setSending = (sending) => {
        state.sending = sending;
        ui.list.querySelectorAll("button").forEach((button) => { button.disabled = sending; });
        ui.projectSelect.disabled = sending;
        ui.refresh.disabled = sending;
        updateComposerState();
    };

    const submitMessage = async () => {
        const prompt = ui.input.value.trim();
        if (!prompt || !state.selectedKey || state.sending || state.reconciliationRequired) return;

        const mode = MODES[state.mode] || MODES.thinking;
        const selectedKey = state.selectedKey;
        const outgoingAttachments = state.attachments.slice();
        const userMessage = createMessage("user", prompt, {images: outgoingAttachments, fresh: true});
        let activeAssistant = null;
        let activeStreamText = null;
        let activeSegmentText = "";
        let activeActivity = createActivity("ChatGPT is working…", true);
        let placeholderActivity = true;
        let lastActivityText = "";
        const seenActivityTexts = new Set();
        let streamedText = "";
        let responseAccepted = false;
        ui.transcript.append(userMessage.article, activeActivity.row);
        scrollTranscript();

        const finishAssistantSegment = () => {
            if (!activeAssistant) return;
            if (activeSegmentText) {
                activeAssistant.content.replaceChildren();
                renderMessageText(activeAssistant.content, activeSegmentText);
            } else {
                activeAssistant.article.remove();
            }
            activeAssistant = null;
            activeStreamText = null;
            activeSegmentText = "";
        };

        const finishActiveActivity = (removePlaceholder = false) => {
            if (!activeActivity) return;
            if (placeholderActivity && removePlaceholder) {
                activeActivity.row.remove();
            } else {
                completeActivity(activeActivity);
            }
            activeActivity = null;
            placeholderActivity = false;
        };

        const appendStreamContent = (content) => {
            finishActiveActivity(true);
            if (!activeAssistant) {
                activeAssistant = createMessage("assistant", "", {streaming: true});
                activeStreamText = activeAssistant.content.querySelector("p");
                ui.transcript.appendChild(activeAssistant.article);
            }
            activeSegmentText += content;
            streamedText += content;
            activeStreamText.textContent = activeSegmentText;
            scrollTranscript();
        };

        const appendActivity = (content) => {
            const text = String(content || "").replace(/\s+/g, " ").trim();
            if (!text) return;
            if (seenActivityTexts.has(text)) return;
            seenActivityTexts.add(text);
            finishAssistantSegment();
            if (text === lastActivityText && activeActivity) return;
            lastActivityText = text;
            if (activeActivity && placeholderActivity) {
                activeActivity.text.textContent = text;
                placeholderActivity = false;
            } else {
                completeActivity(activeActivity);
                activeActivity = createActivity(text, true);
                ui.transcript.appendChild(activeActivity.row);
            }
            setTurnState(text, false, true);
            scrollTranscript();
        };

        state.controller = new AbortController();
        let activityController = null;
        let activityPoll = null;
        const stopActivityPolling = async () => {
            const controller = activityController;
            const poll = activityPoll;
            activityController = null;
            activityPoll = null;
            controller?.abort();
            if (poll) await poll;
        };
        setSending(true);
        setTurnState("Submitting once…", false, true);

        try {
            const requestPayload = {
                provider: "OpenaiAccount",
                model: mode.model,
                thinking_effort: mode.effort,
                messages: [{role: "user", content: prompt}],
                conversation: {
                    advisor_cloud_project: state.projectKey,
                    advisor_cloud_handle: selectedKey,
                },
            };
            const requestHeaders = {
                "Accept": "text/event-stream",
                "X-Advisor-Cloud": "1",
            };
            let requestBody = JSON.stringify(requestPayload);
            if (outgoingAttachments.length) {
                const formData = new FormData();
                const extensions = {
                    "image/gif": "gif",
                    "image/jpeg": "jpg",
                    "image/png": "png",
                    "image/webp": "webp",
                };
                formData.append("json", requestBody);
                outgoingAttachments.forEach((attachment, index) => {
                    const extension = extensions[attachment.file.type] || "img";
                    formData.append("files", attachment.file, `image-${index + 1}.${extension}`);
                });
                requestBody = formData;
            } else {
                requestHeaders["Content-Type"] = "application/json";
            }
            const response = await fetch("/backend-api/v2/conversation", {
                method: "POST",
                credentials: "same-origin",
                signal: state.controller.signal,
                headers: requestHeaders,
                body: requestBody,
            });

            if (!response.ok) {
                let message = `Cloud request failed (${response.status})`;
                try {
                    const payload = await response.json();
                    message = payload?.error?.message || message;
                } catch (_error) {
                    // Keep the status-only error.
                }
                const requestError = new Error(message);
                requestError.definitiveRejection = true;
                throw requestError;
            }

            const activityToken = response.headers.get("X-Advisor-Activity-Token") || "";
            if (/^[0-9a-f]{32}$/.test(activityToken)) {
                activityController = new AbortController();
                activityPoll = pollConversationActivity(
                    selectedKey,
                    activityToken,
                    activityController.signal,
                    appendActivity,
                );
            }

            responseAccepted = true;
            ui.input.value = "";
            resizeComposer();
            setTurnState("ChatGPT is working…", false, true);
            await consumeStream(response, (payload) => {
                if (payload.type === "content" && typeof payload.content === "string") {
                    appendStreamContent(payload.content);
                } else if (payload.type === "activity" && typeof payload.content === "string") {
                    appendActivity(payload.content);
                } else if (payload.type === "reasoning" && typeof payload.status === "string") {
                    appendActivity(payload.status);
                } else if (payload.type === "message" && payload.error) {
                    throw new Error(payload.message || payload.error);
                } else if (["error", "auth"].includes(payload.type)) {
                    throw new Error(payload.error || "ChatGPT rejected the cloud turn");
                }
            });
            await stopActivityPolling();

            finishAssistantSegment();
            finishActiveActivity(true);
            setTurnState("Synchronizing…", false, true);
            const payload = await recoverConversationUntilReady(
                selectedKey,
                {observeFirst: true},
            );
            state.selectedConversation = payload.conversation;
            markConversationReconciled(selectedKey);
            renderTranscript(payload.conversation);
            clearAttachments();
            setConversationReadyState(payload.conversation);
        } catch (error) {
            await stopActivityPolling();
            finishAssistantSegment();
            finishActiveActivity(!streamedText && !lastActivityText);
            state.reconciliationRequired = true;
            const aborted = error?.name === "AbortError";
            state.controller = null;
            ui.stop.hidden = true;
            setTurnState("Recovering cloud conversation…", false, true);
            try {
                const payload = await recoverConversationUntilReady(
                    selectedKey,
                    {observeFirst: true},
                );
                if (
                    !payload.conversation?.recoveredSubmission
                    && !responseAccepted
                    && !error?.definitiveRejection
                ) {
                    throw error;
                }
                state.selectedConversation = payload.conversation;
                state.reconciliationRequired = false;
                markConversationReconciled(selectedKey);
                renderTranscript(payload.conversation);
                if (payload.conversation?.recoveredSubmission || responseAccepted) {
                    clearAttachments();
                }
                if (!error?.definitiveRejection) {
                    ui.input.value = "";
                    resizeComposer();
                    showToast("Recovered the completed cloud turn");
                } else {
                    showToast("Cloud conversation refreshed; your prompt was not sent", true);
                }
                setConversationReadyState(payload.conversation);
            } catch (recoveryError) {
                state.reconciliationRequired = true;
                if (recoveryError?.code === "cloud_history_unresolved") {
                    showUnresolvedRecovery();
                    showToast("Cloud history needs a recovery decision", true);
                } else {
                    setTurnState("Refresh required", true);
                    showToast(
                        aborted
                            ? "Turn stopped locally; refresh before sending again"
                            : (recoveryError?.message || error?.message || "Cloud turn failed"),
                        true,
                    );
                }
            }
        } finally {
            await stopActivityPolling();
            state.controller = null;
            setSending(false);
        }
    };

    const resizeComposer = () => {
        ui.input.style.height = "auto";
        ui.input.style.height = `${Math.min(ui.input.scrollHeight, 210)}px`;
        updateComposerState();
    };

    const cancelConversationLoad = () => {
        if (!state.openController) return;
        state.openController.abort();
        clearView();
        showToast("Cloud conversation loading stopped");
    };

    const retryConversationLoad = () => {
        if (!state.selectedKey || state.busy || state.sending) return;
        state.reconciliationRequired = false;
        openConversation(state.selectedKey);
    };

    const openCurrentConversationInChatGPT = () => {
        if (!state.projectKey || !state.selectedKey) return;
        const url = `/advisor-api/projects/${encodeURIComponent(state.projectKey)}`
            + `/conversations/${encodeURIComponent(state.selectedKey)}/open-chatgpt`;
        window.open(url, "_blank", "noopener,noreferrer");
    };

    const adoptCurrentCloudBranch = async () => {
        if (!state.projectKey || !state.selectedKey || state.busy || state.sending) return;
        const projectKey = state.projectKey;
        const conversationKey = state.selectedKey;
        const recoveryToken = currentRecoveryToken();
        if (!recoveryToken) {
            showToast("Recovery state changed; refresh this conversation", true);
            return;
        }
        let recoveryStateChanged = false;
        setBusy(true);
        setTurnState("Adopting current cloud branch…", false, true);
        try {
            const payload = await requestJson(
                `/advisor-api/projects/${encodeURIComponent(projectKey)}`
                + `/conversations/${encodeURIComponent(conversationKey)}/adopt-current`,
                {
                    method: "POST",
                    headers: {"Content-Type": "application/json", "X-Advisor-Cloud": "1"},
                    body: JSON.stringify({
                        acknowledge: "do_not_resend",
                        recovery_token: recoveryToken,
                        resolution: "adopt_current_branch",
                    }),
                },
            );
            const conversation = payload.conversation;
            if (!conversation?.id || !Array.isArray(conversation.items)) {
                throw new Error("The recovered cloud conversation returned an invalid response");
            }
            if (state.projectKey !== projectKey || state.selectedKey !== conversationKey) return;
            state.selectedConversation = conversation;
            state.reconciliationRequired = false;
            markConversationReconciled(conversationKey);
            renderTranscript(conversation);
            setConversationReadyState(conversation);
            showToast("Current ChatGPT branch adopted; no prompt was resent");
            ui.input.focus({preventScroll: true});
        } catch (error) {
            state.reconciliationRequired = true;
            if (error?.code === "recovery_state_changed") {
                recoveryStateChanged = true;
                hideRecoveryState();
                showToast("Recovery state changed; refreshing the conversation", true);
            } else {
                showUnresolvedRecovery();
                showToast(error?.message || "Could not adopt the current cloud branch", true);
            }
        } finally {
            setBusy(false);
            updateComposerState();
        }
        if (recoveryStateChanged
                && state.projectKey === projectKey
                && state.selectedKey === conversationKey) {
            state.reconciliationRequired = false;
            await openConversation(conversationKey);
        }
    };

    ui.projectSelect.addEventListener("change", () => {
        if (state.sending) return;
        state.projectKey = ui.projectSelect.value;
        state.conversations = [];
        clearView();
        refreshProject();
    });
    ui.search.addEventListener("input", renderConversationList);
    ui.modeOptions.forEach((button) => {
        button.addEventListener("click", () => {
            if (state.sending || !MODES[button.dataset.mode]) return;
            state.mode = button.dataset.mode;
            ui.modeOptions.forEach((option) => {
                const active = option.dataset.mode === state.mode;
                option.classList.toggle("active", active);
                option.setAttribute("aria-pressed", String(active));
            });
        });
    });
    ui.refresh.addEventListener("click", async () => {
        if (state.selectedKey && state.reconciliationRequired) {
            const selected = state.selectedKey;
            state.reconciliationRequired = false;
            await openConversation(selected);
        } else {
            await refreshProject();
        }
    });
    ui.clearView.addEventListener("click", clearView);
    ui.cancelLoad.addEventListener("click", cancelConversationLoad);
    ui.retryLoad.addEventListener("click", retryConversationLoad);
    ui.recheckRecovery.addEventListener("click", retryConversationLoad);
    ui.openCloudChat.addEventListener("click", openCurrentConversationInChatGPT);
    ui.adoptCurrentBranch.addEventListener("click", () => {
        if (!state.recoveryUnresolved || state.busy || state.sending) return;
        ui.recoveryDialog.showModal();
        ui.cancelAdopt.focus({preventScroll: true});
    });
    ui.cancelAdopt.addEventListener("click", () => ui.recoveryDialog.close());
    ui.confirmAdopt.addEventListener("click", () => {
        ui.recoveryDialog.close();
        adoptCurrentCloudBranch();
    });
    ui.sidebarOpen.addEventListener("click", openSidebar);
    ui.sidebarClose.addEventListener("click", closeSidebar);
    ui.sidebarBackdrop.addEventListener("click", closeSidebar);
    ui.form.addEventListener("submit", (event) => {
        event.preventDefault();
        submitMessage();
    });
    ui.attach.addEventListener("click", () => ui.imageInput.click());
    ui.imageInput.addEventListener("change", () => {
        addImageFiles(Array.from(ui.imageInput.files || []));
        ui.imageInput.value = "";
    });
    ui.input.addEventListener("input", resizeComposer);
    ui.input.addEventListener("paste", (event) => {
        const images = Array.from(event.clipboardData?.files || [])
            .filter((file) => file.type.startsWith("image/"));
        if (!images.length) return;
        event.preventDefault();
        const text = event.clipboardData?.getData("text/plain") || "";
        if (text) {
            ui.input.setRangeText(
                text,
                ui.input.selectionStart,
                ui.input.selectionEnd,
                "end",
            );
        }
        addImageFiles(images);
        resizeComposer();
    });
    ui.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            submitMessage();
        }
    });
    ui.transcript.addEventListener("scroll", () => {
        state.followLatest = transcriptNearLatest();
        updateLatestControl();
    }, {passive: true});
    ui.jumpLatest.addEventListener("click", () => scrollTranscript(true));
    ui.stop.addEventListener("click", () => {
        if (state.sending) {
            state.controller?.abort();
        } else {
            cancelConversationLoad();
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            if (state.openController) {
                cancelConversationLoad();
            } else if (document.body.classList.contains("sidebar-visible")) {
                closeSidebar();
            }
        }
    });
    window.addEventListener("pagehide", () => {
        state.attachments.forEach((attachment) => URL.revokeObjectURL(attachment.url));
    });

    loadProjects();
})();
