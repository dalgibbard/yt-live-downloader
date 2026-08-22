(() => {
    "use strict";

    const ACTIVE_STATES = new Set([
        "queued",
        "inspecting",
        "waiting_live",
        "downloading",
        "post_processing",
    ]);
    const RETRYABLE_STATES = new Set(["failed", "cancelled", "interrupted"]);
    const KNOWN_STATES = new Set([
        ...ACTIVE_STATES,
        ...RETRYABLE_STATES,
        "completed",
    ]);
    const KNOWN_FILE_STATES = new Set([
        "pending",
        "available",
        "missing",
        "deleted",
        "unavailable",
    ]);

    const STATE_LABELS = Object.freeze({
        queued: "Queued",
        inspecting: "Reading stream",
        waiting_live: "Waiting for live",
        downloading: "Capturing",
        post_processing: "Finalising",
        completed: "Complete",
        failed: "Failed",
        cancelled: "Cancelled",
        interrupted: "Interrupted",
        unknown: "Unknown",
    });

    const FILE_STATE_LABELS = Object.freeze({
        pending: "Not ready",
        available: "Available",
        missing: "File missing",
        deleted: "Deleted",
        unavailable: "Unavailable",
        unknown: "Unknown",
    });

    const POLL_INTERVAL_ACTIVE = 2_500;
    const POLL_INTERVAL_IDLE = 15_000;
    const POLL_INTERVAL_HIDDEN = 30_000;
    const POLL_INTERVAL_MAX = 60_000;
    const REQUEST_TIMEOUT = 15_000;

    const elements = {
        serviceStatus: document.querySelector("#service-status"),
        serviceStatusText: document.querySelector("#service-status-text"),
        captureForm: document.querySelector("#capture-form"),
        captureInput: document.querySelector("#youtube-url"),
        captureSubmit: document.querySelector(".capture-submit"),
        captureSubmitLabel: document.querySelector(".capture-submit .button-label"),
        captureError: document.querySelector("#capture-error"),
        connectionBanner: document.querySelector("#connection-banner"),
        connectionMessage: document.querySelector("#connection-message"),
        retryConnection: document.querySelector("#retry-connection"),
        refreshButton: document.querySelector("#refresh-downloads"),
        loadingState: document.querySelector("#loading-state"),
        emptyState: document.querySelector("#empty-state"),
        tableFrame: document.querySelector("#downloads-table-frame"),
        downloadsBody: document.querySelector("#downloads-body"),
        downloadCount: document.querySelector("#download-count"),
        rowTemplate: document.querySelector("#download-row-template"),
        playbackDialog: document.querySelector("#playback-dialog"),
        playbackHeading: document.querySelector("#playback-heading"),
        playbackVideo: document.querySelector("#playback-video"),
        playbackError: document.querySelector("#playback-error"),
        playbackFileName: document.querySelector("#playback-file-name"),
        playbackDownload: document.querySelector("#playback-download"),
        renameDialog: document.querySelector("#rename-dialog"),
        renameForm: document.querySelector("#rename-form"),
        renameInput: document.querySelector("#rename-title"),
        renameDescription: document.querySelector("#rename-description"),
        renameError: document.querySelector("#rename-error"),
        confirmationDialog: document.querySelector("#confirmation-dialog"),
        confirmationForm: document.querySelector("#confirmation-form"),
        confirmationHeading: document.querySelector("#confirmation-heading"),
        confirmationDescription: document.querySelector("#confirmation-description"),
        confirmationTarget: document.querySelector("#confirmation-target"),
        confirmationSubmit: document.querySelector("#confirmation-submit"),
        confirmationError: document.querySelector("#confirmation-error"),
        toastRegion: document.querySelector("#toast-region"),
    };

    if (!elements.downloadsBody || !elements.rowTemplate) {
        return;
    }

    let downloads = new Map();
    const rows = new Map();
    const busyJobs = new Set();
    let pollTimer = null;
    let pollInFlight = false;
    let pollFailures = 0;
    let hasLoaded = false;
    let mutationVersion = 0;
    let playbackContext = null;
    let renameContext = null;
    let confirmationContext = null;

    class ApiError extends Error {
        constructor(message, detail = "", status = 0) {
            super(message);
            this.name = "ApiError";
            this.detail = detail;
            this.status = status;
        }
    }

    function finiteNumber(value) {
        if (value === null || value === undefined || value === "") {
            return null;
        }
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function clamp(value, minimum, maximum) {
        return Math.min(Math.max(value, minimum), maximum);
    }

    function asText(value) {
        return typeof value === "string" ? value.trim() : "";
    }

    function safeUrl(value) {
        const raw = asText(value);
        if (!raw) {
            return "";
        }

        try {
            const parsed = new URL(raw, window.location.origin);
            if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
                return "";
            }
            return parsed.href;
        } catch (_error) {
            return "";
        }
    }

    function encodeJobId(id) {
        return encodeURIComponent(String(id));
    }

    function jobEndpoint(id, suffix = "") {
        return `/api/downloads/${encodeJobId(id)}${suffix}`;
    }

    async function requestJson(path, options = {}) {
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
        const headers = new Headers(options.headers || {});
        headers.set("Accept", "application/json");
        if (options.body && !headers.has("Content-Type")) {
            headers.set("Content-Type", "application/json");
        }

        try {
            const response = await fetch(path, {
                ...options,
                headers,
                credentials: "same-origin",
                cache: "no-store",
                signal: controller.signal,
            });
            const responseText = await response.text();
            let payload = null;

            if (responseText) {
                try {
                    payload = JSON.parse(responseText);
                } catch (_error) {
                    if (!response.ok) {
                        throw new ApiError(`Request failed (${response.status})`, responseText, response.status);
                    }
                    throw new ApiError("The server returned an invalid response.", "", response.status);
                }
            }

            if (!response.ok) {
                const apiError = payload && payload.error;
                const message =
                    (apiError && typeof apiError === "object" && asText(apiError.summary || apiError.message)) ||
                    (typeof apiError === "string" && apiError) ||
                    (payload && asText(payload.message)) ||
                    `Request failed (${response.status})`;
                const detail =
                    (apiError && typeof apiError === "object" && asText(apiError.detail)) ||
                    (payload && asText(payload.detail)) ||
                    "";
                throw new ApiError(message, detail, response.status);
            }

            return payload;
        } catch (error) {
            if (error && error.name === "AbortError") {
                throw new ApiError("The server took too long to respond.");
            }
            if (error instanceof ApiError) {
                throw error;
            }
            throw new ApiError("Could not reach the recorder.", error instanceof Error ? error.message : "");
        } finally {
            window.clearTimeout(timeout);
        }
    }

    function normalizeJob(rawJob) {
        if (!rawJob || typeof rawJob !== "object") {
            return null;
        }

        const id = asText(String(rawJob.id ?? ""));
        if (!id) {
            return null;
        }

        const state = asText(rawJob.state).toLowerCase();
        const file = rawJob.file && typeof rawJob.file === "object" ? rawJob.file : {};
        const progress = rawJob.progress && typeof rawJob.progress === "object" ? rawJob.progress : {};
        const fileState = asText(file.state).toLowerCase();

        return {
            ...rawJob,
            id,
            url: asText(rawJob.url),
            title: asText(rawJob.title),
            source_title: asText(rawJob.source_title),
            thumbnail_url: asText(rawJob.thumbnail_url),
            state: KNOWN_STATES.has(state) ? state : "unknown",
            phase: asText(rawJob.phase),
            detail: asText(rawJob.detail),
            progress,
            file: {
                ...file,
                state: KNOWN_FILE_STATES.has(fileState) ? fileState : "unknown",
                name: asText(file.name),
                download_url: asText(file.download_url),
                playback_url: asText(file.playback_url),
            },
        };
    }

    function extractJob(payload) {
        if (!payload || typeof payload !== "object") {
            return null;
        }
        return normalizeJob(payload.download || payload.job || (payload.id ? payload : null));
    }

    function formatBytes(value) {
        const bytes = finiteNumber(value);
        if (bytes === null || bytes < 0) {
            return "";
        }
        if (bytes === 0) {
            return "0 B";
        }

        const units = ["B", "KiB", "MiB", "GiB", "TiB"];
        const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
        const amount = bytes / (1024 ** unitIndex);
        const digits = amount >= 100 || unitIndex === 0 ? 0 : amount >= 10 ? 1 : 2;
        return `${amount.toFixed(digits)} ${units[unitIndex]}`;
    }

    function formatPercent(value) {
        const percent = finiteNumber(value);
        if (percent === null) {
            return "";
        }
        const bounded = clamp(percent, 0, 100);
        return `${bounded < 10 && bounded % 1 !== 0 ? bounded.toFixed(1) : Math.round(bounded)}%`;
    }

    function formatDuration(value) {
        const rawSeconds = finiteNumber(value);
        if (rawSeconds === null || rawSeconds < 0) {
            return "";
        }

        const totalSeconds = Math.round(rawSeconds);
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;
        if (hours > 0) {
            return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
        }
        return `${minutes}:${String(seconds).padStart(2, "0")}`;
    }

    const dateFormatter = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
    });

    function formatDate(value) {
        const text = asText(value);
        if (!text) {
            return null;
        }
        const date = new Date(text);
        if (Number.isNaN(date.getTime())) {
            return null;
        }
        return {
            date,
            label: dateFormatter.format(date),
        };
    }

    function humanizeIdentifier(value) {
        const text = asText(value).replace(/[_-]+/g, " ").replace(/\s+/g, " ");
        return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
    }

    function shortSourceLabel(value) {
        const href = safeUrl(value);
        if (!href) {
            return "";
        }

        const parsed = new URL(href);
        let label = `${parsed.hostname.replace(/^www\./, "")}${parsed.pathname === "/" ? "" : parsed.pathname}`;
        const videoId = parsed.searchParams.get("v");
        if (videoId) {
            label += `?v=${videoId}`;
        }
        if (label.length > 48) {
            label = `${label.slice(0, 45)}…`;
        }
        return label;
    }

    function displayTitle(job) {
        return job.title || job.source_title || "Untitled stream";
    }

    function defaultStatusDetail(job) {
        switch (job.state) {
            case "queued":
                return "Waiting for a download worker.";
            case "inspecting":
                return "Reading stream details and available formats.";
            case "waiting_live":
                return "The stream has not started yet.";
            case "downloading":
                return "Capturing available stream data.";
            case "post_processing":
                return "Preparing the final media file.";
            case "completed":
                return "Capture finished successfully.";
            case "failed":
                return "The capture could not be completed.";
            case "cancelled":
                return "Capture stopped by a user.";
            case "interrupted":
                return "Capture stopped before it completed.";
            default:
                return "Status unavailable.";
        }
    }

    function normalizeParts(parts) {
        if (Array.isArray(parts)) {
            return parts
                .map((part, index) => {
                    if (!part || typeof part !== "object") {
                        return null;
                    }
                    return { key: asText(part.id) || asText(part.kind) || String(index), ...part };
                })
                .filter(Boolean);
        }

        if (parts && typeof parts === "object") {
            return Object.entries(parts)
                .map(([key, part]) => {
                    if (!part || typeof part !== "object") {
                        return null;
                    }
                    return { key, ...part };
                })
                .filter(Boolean);
        }
        return [];
    }

    function partDescription(part) {
        const state = asText(part.state || part.status).toLowerCase();
        if (state === "completed" || state === "finished") {
            const bytes = formatBytes(part.downloaded_bytes ?? part.total_bytes);
            return bytes ? `Complete · ${bytes}` : "Complete";
        }

        const percentValue = finiteNumber(part.percent);
        if (percentValue !== null) {
            return formatPercent(percentValue);
        }

        const downloaded = formatBytes(part.downloaded_bytes);
        const total = formatBytes(part.total_bytes);
        if (downloaded && total) {
            return `${downloaded} / ${total}`;
        }
        if (downloaded) {
            return `${downloaded} captured`;
        }
        return humanizeIdentifier(state) || "Pending";
    }

    function jobTimestamp(job) {
        const date = new Date(job.created_at || job.updated_at || 0);
        const time = date.getTime();
        return Number.isNaN(time) ? 0 : time;
    }

    function sortedJobs() {
        return [...downloads.values()].sort((left, right) => {
            const timeDifference = jobTimestamp(right) - jobTimestamp(left);
            return timeDifference || left.id.localeCompare(right.id);
        });
    }

    function makeRow(job) {
        const row = elements.rowTemplate.content.firstElementChild.cloneNode(true);
        row.dataset.downloadId = job.id;
        const thumbnail = row.querySelector(".stream-thumbnail");
        thumbnail.addEventListener("error", () => {
            thumbnail.classList.add("is-unavailable");
        });
        return row;
    }

    function updateThumbnail(row, job) {
        const thumbnail = row.querySelector(".stream-thumbnail");
        const thumbnailUrl = safeUrl(job.thumbnail_url);
        if (!thumbnailUrl) {
            thumbnail.removeAttribute("src");
            thumbnail.classList.add("is-unavailable");
            thumbnail.dataset.source = "";
            return;
        }

        if (thumbnail.dataset.source !== thumbnailUrl) {
            thumbnail.dataset.source = thumbnailUrl;
            thumbnail.classList.remove("is-unavailable");
            thumbnail.src = thumbnailUrl;
        }
    }

    function updateStreamCell(row, job) {
        const title = displayTitle(job);
        const titleElement = row.querySelector(".stream-title");
        const sourceTitle = row.querySelector(".source-title");
        const sourceLink = row.querySelector(".source-link");
        const createdTime = row.querySelector(".created-time");

        titleElement.textContent = title;
        titleElement.title = title;

        const hasDifferentSourceTitle = Boolean(job.source_title && job.source_title !== title);
        sourceTitle.hidden = !hasDifferentSourceTitle;
        sourceTitle.textContent = hasDifferentSourceTitle ? `YouTube: ${job.source_title}` : "";
        sourceTitle.title = hasDifferentSourceTitle ? job.source_title : "";

        const sourceUrl = safeUrl(job.url);
        sourceLink.hidden = !sourceUrl;
        if (sourceUrl) {
            sourceLink.href = sourceUrl;
            sourceLink.textContent = shortSourceLabel(sourceUrl) || "View source";
            sourceLink.title = job.url;
        } else {
            sourceLink.removeAttribute("href");
            sourceLink.textContent = "";
            sourceLink.title = "";
        }

        const created = formatDate(job.created_at);
        createdTime.hidden = !created;
        if (created) {
            createdTime.dateTime = created.date.toISOString();
            createdTime.textContent = `Added ${created.label}`;
        } else {
            createdTime.removeAttribute("datetime");
            createdTime.textContent = "";
        }

        updateThumbnail(row, job);
    }

    function updateProgress(row, job) {
        const block = row.querySelector(".progress-block");
        const track = row.querySelector(".progress-track");
        const fill = row.querySelector(".progress-fill");
        const summary = row.querySelector(".progress-summary");
        const metrics = row.querySelector(".progress-metrics");
        const partsList = row.querySelector(".progress-parts");
        const progress = job.progress || {};
        const downloaded = finiteNumber(progress.downloaded_bytes);
        const total = finiteNumber(progress.total_bytes);
        let percent = finiteNumber(progress.percent);
        const speed = finiteNumber(progress.speed_bps);
        const eta = finiteNumber(progress.eta_seconds);
        const parts = normalizeParts(progress.parts);
        const isActive = ACTIVE_STATES.has(job.state);

        if (percent === null && downloaded !== null && total !== null && total > 0) {
            percent = (downloaded / total) * 100;
        }
        if (percent === null && job.state === "completed") {
            percent = 100;
        }
        if (percent !== null) {
            percent = clamp(percent, 0, 100);
        }

        const hasRecordedData = downloaded !== null && downloaded > 0;
        const shouldShow = isActive || hasRecordedData || percent !== null || parts.length > 0;
        block.hidden = !shouldShow;
        if (!shouldShow) {
            return;
        }

        const isIndeterminate = isActive && percent === null;
        const showTrack = percent !== null || isIndeterminate;
        track.hidden = !showTrack;
        track.classList.toggle("is-indeterminate", isIndeterminate);
        track.setAttribute("aria-label", `Progress for ${displayTitle(job)}`);

        let summaryText = "";
        if (downloaded !== null && total !== null && total > 0) {
            summaryText = `${formatBytes(downloaded)} of ${formatBytes(total)}`;
        } else if (hasRecordedData) {
            summaryText = `${formatBytes(downloaded)} captured`;
        } else if (job.state === "waiting_live") {
            summaryText = "Waiting for stream data";
        } else if (job.state === "post_processing") {
            summaryText = "Preparing final file";
        } else if (job.state === "completed") {
            summaryText = "Capture complete";
        } else {
            summaryText = "Waiting for data";
        }

        if (percent !== null) {
            fill.style.width = `${percent}%`;
            track.setAttribute("aria-valuenow", String(Math.round(percent)));
            track.setAttribute("aria-valuetext", `${formatPercent(percent)}; ${summaryText}`);
        } else {
            fill.style.width = "";
            track.removeAttribute("aria-valuenow");
            track.setAttribute("aria-valuetext", summaryText);
        }

        summary.textContent = percent !== null ? `${formatPercent(percent)} · ${summaryText}` : summaryText;
        const metricParts = [];
        if (speed !== null && speed > 0) {
            metricParts.push(`${formatBytes(speed)}/s`);
        }
        if (eta !== null && eta >= 0) {
            metricParts.push(`ETA ${formatDuration(eta)}`);
        }
        metrics.textContent = metricParts.join(" · ");
        metrics.hidden = metricParts.length === 0;

        partsList.replaceChildren();
        for (const part of parts) {
            const item = document.createElement("li");
            const name = document.createElement("span");
            const state = document.createElement("span");
            item.className = "progress-part";
            name.className = "progress-part-name";
            state.className = "progress-part-state";
            name.textContent = humanizeIdentifier(part.label || part.kind || part.key) || "Media";
            state.textContent = partDescription(part);
            item.append(name, state);
            partsList.append(item);
        }
        partsList.hidden = parts.length === 0;
    }

    function normalizedError(job) {
        if (!job.error) {
            return null;
        }
        if (typeof job.error === "string") {
            return { summary: job.error, detail: "" };
        }
        if (typeof job.error === "object") {
            const summary = asText(job.error.summary || job.error.message) || "Download failed";
            return { summary, detail: asText(job.error.detail) };
        }
        return null;
    }

    function updateActivityCell(row, job) {
        const badge = row.querySelector(".status-badge");
        const statusText = row.querySelector(".status-text");
        const phaseLabel = row.querySelector(".phase-label");
        const detail = row.querySelector(".status-detail");
        const errorCard = row.querySelector(".error-card");
        const errorSummary = row.querySelector(".error-summary");
        const errorDetail = row.querySelector(".error-detail");
        const isActive = ACTIVE_STATES.has(job.state);

        badge.className = `status-badge state-${job.state}${isActive ? " is-active" : ""}`;
        statusText.textContent = STATE_LABELS[job.state] || STATE_LABELS.unknown;
        phaseLabel.textContent = humanizeIdentifier(job.phase);
        phaseLabel.hidden = !job.phase;
        detail.textContent = job.detail || defaultStatusDetail(job);

        const error = normalizedError(job);
        errorCard.hidden = !error;
        if (error) {
            errorSummary.textContent = error.summary;
            errorDetail.textContent = error.detail;
            errorDetail.hidden = !error.detail;
        } else {
            errorCard.open = false;
            errorSummary.textContent = "";
            errorDetail.textContent = "";
        }

        updateProgress(row, job);
    }

    function updateFileCell(row, job) {
        const fileState = job.file.state || "unknown";
        const stateElement = row.querySelector(".file-state");
        const nameElement = row.querySelector(".file-name");
        const sizeElement = row.querySelector(".file-size");
        const fileSize = finiteNumber(job.file.size_bytes);

        stateElement.className = `file-state file-${fileState}`;
        stateElement.textContent = FILE_STATE_LABELS[fileState] || FILE_STATE_LABELS.unknown;
        nameElement.textContent = job.file.name;
        nameElement.title = job.file.name;
        nameElement.hidden = !job.file.name;
        sizeElement.textContent = fileSize !== null && fileSize >= 0 ? formatBytes(fileSize) : "";
        sizeElement.hidden = fileSize === null || fileSize < 0;
    }

    function setActionLabel(element, action, title) {
        element.setAttribute("aria-label", `${action} ${title}`);
    }

    function updateActions(row, job) {
        const title = displayTitle(job);
        const isActive = ACTIVE_STATES.has(job.state);
        const isBusy = busyJobs.has(job.id);
        const play = row.querySelector(".action-play");
        const download = row.querySelector(".action-download");
        const rename = row.querySelector('[data-action="rename"]');
        const cancel = row.querySelector(".action-cancel");
        const retry = row.querySelector(".action-retry");
        const deleteFile = row.querySelector(".action-delete-file");
        const removeHistory = row.querySelector(".action-remove-history");
        const canDownload = job.file.state === "available";
        const canPlay = canDownload && Boolean(safeUrl(job.file.playback_url));
        const canRetry = RETRYABLE_STATES.has(job.state);
        const canDeleteFile = !isActive && job.file.state === "available";

        row.setAttribute("aria-busy", String(isBusy));

        play.hidden = !canPlay;
        play.disabled = isBusy;
        download.hidden = !canDownload;
        if (canDownload) {
            const downloadUrl = safeUrl(job.file.download_url) || jobEndpoint(job.id, "/file");
            download.href = downloadUrl;
            download.setAttribute("download", "");
            setActionLabel(download, "Download", title);
        } else {
            download.removeAttribute("href");
            download.removeAttribute("download");
        }

        cancel.hidden = !isActive;
        retry.hidden = !canRetry;
        deleteFile.hidden = !canDeleteFile;
        removeHistory.hidden = isActive;

        for (const control of [rename, cancel, retry, deleteFile, removeHistory]) {
            control.disabled = isBusy;
        }
        if (canDownload) {
            download.setAttribute("aria-disabled", String(isBusy));
            download.tabIndex = isBusy ? -1 : 0;
        }

        setActionLabel(play, "Play", title);
        setActionLabel(rename, "Rename", title);
        setActionLabel(cancel, "Cancel capture for", title);
        setActionLabel(retry, "Retry", title);
        setActionLabel(deleteFile, "Delete file for", title);
        setActionLabel(removeHistory, "Remove history for", title);
    }

    function updateRow(row, job) {
        row.dataset.downloadId = job.id;
        updateStreamCell(row, job);
        updateActivityCell(row, job);
        updateFileCell(row, job);
        updateActions(row, job);
    }

    function renderDownloads() {
        const jobs = sortedJobs();
        const currentIds = new Set(jobs.map((job) => job.id));

        for (const [id, row] of rows) {
            if (!currentIds.has(id)) {
                row.remove();
                rows.delete(id);
            }
        }

        for (const job of jobs) {
            let row = rows.get(job.id);
            if (!row) {
                row = makeRow(job);
                rows.set(job.id, row);
            }
            updateRow(row, job);
            elements.downloadsBody.append(row);
        }

        elements.loadingState.hidden = hasLoaded;
        elements.emptyState.hidden = !hasLoaded || jobs.length > 0;
        elements.tableFrame.hidden = jobs.length === 0;
        elements.downloadCount.textContent = String(jobs.length);
        elements.downloadCount.title = `${jobs.length} ${jobs.length === 1 ? "download" : "downloads"}`;
    }

    function announceStateChanges(previous, next) {
        if (!hasLoaded) {
            return;
        }

        for (const [id, job] of next) {
            const oldJob = previous.get(id);
            if (!oldJob || oldJob.state === job.state) {
                continue;
            }
            if (job.state === "completed") {
                showToast(`${displayTitle(job)} is ready.`, "success");
            } else if (job.state === "failed") {
                const error = normalizedError(job);
                showToast(error ? `${displayTitle(job)} failed: ${error.summary}` : `${displayTitle(job)} failed.`, "error");
            } else if (job.state === "interrupted") {
                showToast(`${displayTitle(job)} was interrupted.`, "error");
            }
        }
    }

    function replaceDownloads(rawJobs) {
        const next = new Map();
        for (const rawJob of rawJobs) {
            const job = normalizeJob(rawJob);
            if (job) {
                next.set(job.id, job);
            }
        }
        announceStateChanges(downloads, next);
        downloads = next;
        renderDownloads();
    }

    function upsertJob(job) {
        if (!job) {
            return;
        }
        downloads.set(job.id, job);
        renderDownloads();
    }

    function setConnectionState(isConnected, retryDelay = 0) {
        elements.serviceStatus.classList.toggle("is-online", isConnected);
        elements.serviceStatus.classList.toggle("is-offline", !isConnected);

        if (isConnected) {
            elements.serviceStatusText.textContent = "Connected";
            elements.connectionBanner.hidden = true;
            return;
        }

        const isOffline = navigator.onLine === false;
        elements.serviceStatusText.textContent = isOffline ? "Offline" : "Reconnecting";
        elements.connectionMessage.textContent = isOffline
            ? "You are offline. Updates will resume when the connection returns."
            : `Updates are temporarily unavailable. Retrying in ${Math.max(1, Math.round(retryDelay / 1000))} seconds.`;
        elements.connectionBanner.hidden = false;
    }

    function nextPollDelay() {
        if (pollFailures > 0) {
            return Math.min(POLL_INTERVAL_ACTIVE * (2 ** (pollFailures - 1)), POLL_INTERVAL_MAX);
        }
        if (document.hidden) {
            return POLL_INTERVAL_HIDDEN;
        }
        return [...downloads.values()].some((job) => ACTIVE_STATES.has(job.state))
            ? POLL_INTERVAL_ACTIVE
            : POLL_INTERVAL_IDLE;
    }

    function schedulePoll(delay = nextPollDelay()) {
        window.clearTimeout(pollTimer);
        pollTimer = window.setTimeout(() => {
            void pollDownloads();
        }, delay);
    }

    async function pollDownloads({ manual = false } = {}) {
        if (pollInFlight) {
            return;
        }
        if (navigator.onLine === false) {
            pollFailures = Math.max(1, pollFailures);
            const delay = nextPollDelay();
            setConnectionState(false, delay);
            schedulePoll(delay);
            return;
        }

        window.clearTimeout(pollTimer);
        pollInFlight = true;
        elements.refreshButton.classList.add("is-refreshing");
        elements.refreshButton.setAttribute("aria-busy", "true");
        const versionAtStart = mutationVersion;

        try {
            const payload = await requestJson("/api/downloads");
            if (!payload || !Array.isArray(payload.downloads)) {
                throw new ApiError("The server returned an invalid downloads list.");
            }
            pollFailures = 0;
            setConnectionState(true);
            if (versionAtStart === mutationVersion) {
                replaceDownloads(payload.downloads);
            }
            hasLoaded = true;
            renderDownloads();
        } catch (error) {
            pollFailures += 1;
            const delay = nextPollDelay();
            setConnectionState(false, delay);
            if (manual) {
                showToast(error.message || "Could not refresh downloads.", "error");
            }
        } finally {
            pollInFlight = false;
            elements.refreshButton.classList.remove("is-refreshing");
            elements.refreshButton.removeAttribute("aria-busy");
            schedulePoll();
        }
    }

    function pollNow(options = {}) {
        window.clearTimeout(pollTimer);
        if (!pollInFlight) {
            void pollDownloads(options);
        }
    }

    function setCaptureError(message = "") {
        elements.captureError.textContent = message;
        elements.captureError.hidden = !message;
        elements.captureInput.setAttribute("aria-invalid", String(Boolean(message)));
    }

    function setCaptureBusy(isBusy) {
        elements.captureSubmit.disabled = isBusy;
        elements.captureInput.disabled = isBusy;
        elements.captureSubmit.setAttribute("aria-busy", String(isBusy));
        elements.captureSubmitLabel.textContent = isBusy ? "Starting…" : "Start capture";
    }

    function validateCaptureUrl(value) {
        if (!value) {
            return "Enter a YouTube URL.";
        }
        try {
            const url = new URL(value);
            if (url.protocol !== "http:" && url.protocol !== "https:") {
                return "Enter a valid web address beginning with http:// or https://.";
            }
        } catch (_error) {
            return "Enter a valid YouTube URL.";
        }
        return "";
    }

    async function submitCapture(event) {
        event.preventDefault();
        const url = elements.captureInput.value.trim();
        const validationError = validateCaptureUrl(url);
        setCaptureError(validationError);
        if (validationError) {
            elements.captureInput.focus();
            return;
        }

        setCaptureBusy(true);
        mutationVersion += 1;
        try {
            const payload = await requestJson("/api/downloads", {
                method: "POST",
                body: JSON.stringify({ url }),
            });
            const job = extractJob(payload);
            if (job) {
                upsertJob(job);
            }
            elements.captureForm.reset();
            setCaptureError();
            showToast(job ? `${displayTitle(job)} was added.` : "Capture was added.", "success");
            pollNow();
        } catch (error) {
            const message = error.message || "Could not start this capture.";
            setCaptureError(message);
            elements.captureInput.focus();
        } finally {
            setCaptureBusy(false);
        }
    }

    function showDialog(dialog) {
        if (typeof dialog.showModal === "function") {
            dialog.showModal();
        } else {
            dialog.setAttribute("open", "");
        }
    }

    function closeDialog(dialog) {
        if (typeof dialog.close === "function") {
            dialog.close();
        } else {
            dialog.removeAttribute("open");
        }
    }

    function setFormError(element, message = "") {
        element.textContent = message;
        element.hidden = !message;
    }

    function setDialogBusy(form, isBusy) {
        for (const control of form.querySelectorAll("button, input")) {
            control.disabled = isBusy;
        }
        form.setAttribute("aria-busy", String(isBusy));
    }

    function openPlaybackDialog(job, trigger) {
        const playbackUrl = safeUrl(job.file.playback_url);
        if (!playbackUrl) {
            return;
        }
        const downloadUrl = safeUrl(job.file.download_url) || jobEndpoint(job.id, "/file");
        playbackContext = { id: job.id, trigger };
        elements.playbackHeading.textContent = displayTitle(job);
        elements.playbackFileName.textContent = job.file.name;
        elements.playbackFileName.title = job.file.name;
        elements.playbackError.hidden = true;
        elements.playbackDownload.href = downloadUrl;
        const posterUrl = safeUrl(job.thumbnail_url);
        if (posterUrl) {
            elements.playbackVideo.poster = posterUrl;
        } else {
            elements.playbackVideo.removeAttribute("poster");
        }
        elements.playbackVideo.src = playbackUrl;
        showDialog(elements.playbackDialog);
        elements.playbackVideo.load();
        void elements.playbackVideo.play().catch(() => {});
    }

    function resetPlayback() {
        elements.playbackVideo.pause();
        elements.playbackVideo.removeAttribute("src");
        elements.playbackVideo.removeAttribute("poster");
        elements.playbackVideo.load();
        elements.playbackDownload.removeAttribute("href");
        elements.playbackError.hidden = true;
    }

    function openRenameDialog(job, trigger) {
        renameContext = { id: job.id, trigger };
        elements.renameInput.value = displayTitle(job);
        elements.renameDescription.textContent = job.file.state === "available"
            ? "Saving will also rename the completed file on disk."
            : "The display title changes now. The final file will use this title when the capture completes.";
        setFormError(elements.renameError);
        elements.renameInput.removeAttribute("aria-invalid");
        showDialog(elements.renameDialog);
        window.setTimeout(() => {
            elements.renameInput.focus();
            elements.renameInput.select();
        }, 0);
    }

    async function submitRename(event) {
        event.preventDefault();
        if (!renameContext) {
            return;
        }

        const context = { ...renameContext };
        const job = downloads.get(context.id);
        const title = elements.renameInput.value.trim();
        if (!title) {
            setFormError(elements.renameError, "Enter a title.");
            elements.renameInput.setAttribute("aria-invalid", "true");
            elements.renameInput.focus();
            return;
        }
        if (job && title === displayTitle(job)) {
            closeDialog(elements.renameDialog);
            return;
        }

        setDialogBusy(elements.renameForm, true);
        setJobBusy(context.id, true);
        setFormError(elements.renameError);
        mutationVersion += 1;
        try {
            const payload = await requestJson(jobEndpoint(context.id), {
                method: "PATCH",
                body: JSON.stringify({ title }),
            });
            const updatedJob = extractJob(payload);
            if (updatedJob) {
                upsertJob(updatedJob);
            } else if (job) {
                upsertJob({ ...job, title });
            }
            closeDialog(elements.renameDialog);
            showToast("Download title updated.", "success");
            pollNow();
        } catch (error) {
            setFormError(elements.renameError, error.message || "Could not rename this download.");
            elements.renameInput.setAttribute("aria-invalid", "true");
            elements.renameInput.focus();
        } finally {
            setDialogBusy(elements.renameForm, false);
            setJobBusy(context.id, false);
        }
    }

    function confirmationDetails(action, job) {
        const title = displayTitle(job);
        switch (action) {
            case "cancel":
                return {
                    heading: "Cancel this capture?",
                    description: "Recording will stop. Any partial data may be removed by the recorder, but this history entry will remain.",
                    target: title,
                    button: "Cancel capture",
                };
            case "delete-file":
                return {
                    heading: "Delete this file?",
                    description: "This permanently deletes the media file from disk. The download history and its details will remain here.",
                    target: job.file.name || title,
                    button: "Delete file",
                };
            case "remove-history":
                return {
                    heading: "Remove from history?",
                    description: job.file.state === "available"
                        ? "This removes only the history entry. The media file will remain on disk and will no longer be managed from this page."
                        : "This removes the history entry from the UI. No media file will be deleted.",
                    target: title,
                    button: "Remove history",
                };
            default:
                return null;
        }
    }

    function openConfirmationDialog(action, job, trigger) {
        const details = confirmationDetails(action, job);
        if (!details) {
            return;
        }
        confirmationContext = { action, id: job.id, trigger };
        elements.confirmationHeading.textContent = details.heading;
        elements.confirmationDescription.textContent = details.description;
        elements.confirmationTarget.textContent = details.target;
        elements.confirmationSubmit.textContent = details.button;
        setFormError(elements.confirmationError);
        showDialog(elements.confirmationDialog);
        window.setTimeout(() => elements.confirmationSubmit.focus(), 0);
    }

    function mutationRequest(action, id) {
        switch (action) {
            case "cancel":
                return requestJson(jobEndpoint(id, "/cancel"), { method: "POST" });
            case "delete-file":
                return requestJson(jobEndpoint(id, "/file"), { method: "DELETE" });
            case "remove-history":
                return requestJson(jobEndpoint(id), { method: "DELETE" });
            case "retry":
                return requestJson(jobEndpoint(id, "/retry"), { method: "POST" });
            default:
                return Promise.reject(new ApiError("Unsupported action."));
        }
    }

    function fallbackMutation(action, job) {
        switch (action) {
            case "cancel":
                return { ...job, state: "cancelled", detail: "Capture stopped by a user." };
            case "delete-file":
                return { ...job, file: { ...job.file, state: "deleted", size_bytes: null, download_url: "" } };
            case "retry":
                return {
                    ...job,
                    state: "queued",
                    detail: "Waiting for a download worker.",
                    error: null,
                    attempt: (finiteNumber(job.attempt) || 0) + 1,
                };
            default:
                return job;
        }
    }

    function successMessage(action, job) {
        switch (action) {
            case "cancel":
                return `${displayTitle(job)} was cancelled.`;
            case "delete-file":
                return "File deleted. Its history entry was kept.";
            case "remove-history":
                return "Download removed from history.";
            case "retry":
                return `${displayTitle(job)} was queued again.`;
            default:
                return "Download updated.";
        }
    }

    function applyMutationResult(action, originalJob, payload) {
        if (action === "remove-history") {
            downloads.delete(originalJob.id);
            renderDownloads();
            return;
        }
        upsertJob(extractJob(payload) || fallbackMutation(action, originalJob));
    }

    function setJobBusy(id, isBusy) {
        if (!id) {
            return;
        }
        if (isBusy) {
            busyJobs.add(id);
        } else {
            busyJobs.delete(id);
        }
        const row = rows.get(id);
        const job = downloads.get(id);
        if (row && job) {
            updateActions(row, job);
        }
    }

    async function submitConfirmation(event) {
        event.preventDefault();
        if (!confirmationContext) {
            return;
        }

        const context = { ...confirmationContext };
        const job = downloads.get(context.id);
        if (!job) {
            closeDialog(elements.confirmationDialog);
            return;
        }

        setDialogBusy(elements.confirmationForm, true);
        setJobBusy(job.id, true);
        setFormError(elements.confirmationError);
        mutationVersion += 1;
        try {
            const payload = await mutationRequest(context.action, job.id);
            applyMutationResult(context.action, job, payload);
            closeDialog(elements.confirmationDialog);
            showToast(successMessage(context.action, job), "success");
            pollNow();
        } catch (error) {
            setFormError(elements.confirmationError, error.message || "The action could not be completed.");
        } finally {
            setDialogBusy(elements.confirmationForm, false);
            setJobBusy(job.id, false);
        }
    }

    async function retryJob(job) {
        setJobBusy(job.id, true);
        mutationVersion += 1;
        try {
            const payload = await mutationRequest("retry", job.id);
            applyMutationResult("retry", job, payload);
            showToast(successMessage("retry", job), "success");
            pollNow();
        } catch (error) {
            showToast(error.message || "Could not retry this download.", "error");
        } finally {
            setJobBusy(job.id, false);
        }
    }

    function handleRowAction(event) {
        const control = event.target.closest("[data-action]");
        if (!control || !elements.downloadsBody.contains(control)) {
            return;
        }

        const action = control.dataset.action;
        if (action === "download") {
            if (control.getAttribute("aria-disabled") === "true") {
                event.preventDefault();
            }
            return;
        }

        event.preventDefault();
        const row = control.closest(".download-row");
        const job = row ? downloads.get(row.dataset.downloadId) : null;
        if (!job || busyJobs.has(job.id)) {
            return;
        }

        if (action === "play") {
            openPlaybackDialog(job, control);
        } else if (action === "rename") {
            openRenameDialog(job, control);
        } else if (action === "retry") {
            void retryJob(job);
        } else if (["cancel", "delete-file", "remove-history"].includes(action)) {
            openConfirmationDialog(action, job, control);
        }
    }

    function showToast(message, type = "info") {
        if (!message) {
            return;
        }

        const toast = document.createElement("div");
        const text = document.createElement("span");
        const close = document.createElement("button");
        toast.className = `toast toast-${type}`;
        text.className = "toast-message";
        close.className = "toast-close";
        text.textContent = message;
        close.type = "button";
        close.setAttribute("aria-label", "Dismiss notification");
        close.textContent = "×";
        toast.append(text, close);

        let removalTimer = window.setTimeout(() => toast.remove(), 6_000);
        close.addEventListener("click", () => {
            window.clearTimeout(removalTimer);
            toast.remove();
        });
        toast.addEventListener("mouseenter", () => window.clearTimeout(removalTimer));
        toast.addEventListener("mouseleave", () => {
            removalTimer = window.setTimeout(() => toast.remove(), 3_000);
        });

        elements.toastRegion.append(toast);
        while (elements.toastRegion.children.length > 4) {
            elements.toastRegion.firstElementChild.remove();
        }
    }

    elements.captureForm.addEventListener("submit", submitCapture);
    elements.captureInput.addEventListener("input", () => setCaptureError());
    elements.downloadsBody.addEventListener("click", handleRowAction);
    elements.renameForm.addEventListener("submit", submitRename);
    elements.confirmationForm.addEventListener("submit", submitConfirmation);
    elements.playbackVideo.addEventListener("error", () => {
        if (playbackContext) {
            elements.playbackError.hidden = false;
        }
    });

    for (const closeButton of document.querySelectorAll("[data-close-dialog]")) {
        closeButton.addEventListener("click", () => {
            const dialog = closeButton.closest("dialog");
            if (dialog) {
                closeDialog(dialog);
            }
        });
    }

    elements.renameDialog.addEventListener("close", () => {
        setFormError(elements.renameError);
        elements.renameInput.removeAttribute("aria-invalid");
        const trigger = renameContext && renameContext.trigger;
        renameContext = null;
        if (trigger && trigger.isConnected) {
            trigger.focus();
        }
    });

    elements.playbackDialog.addEventListener("close", () => {
        const trigger = playbackContext && playbackContext.trigger;
        playbackContext = null;
        resetPlayback();
        if (trigger && trigger.isConnected) {
            trigger.focus();
        }
    });

    elements.confirmationDialog.addEventListener("close", () => {
        setFormError(elements.confirmationError);
        const trigger = confirmationContext && confirmationContext.trigger;
        confirmationContext = null;
        if (trigger && trigger.isConnected) {
            trigger.focus();
        }
    });

    elements.refreshButton.addEventListener("click", () => pollNow({ manual: true }));
    elements.retryConnection.addEventListener("click", () => pollNow({ manual: true }));

    window.addEventListener("online", () => {
        pollFailures = 0;
        pollNow({ manual: true });
    });
    window.addEventListener("offline", () => {
        window.clearTimeout(pollTimer);
        pollFailures = Math.max(1, pollFailures);
        setConnectionState(false, nextPollDelay());
    });
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            pollNow();
        } else {
            schedulePoll(POLL_INTERVAL_HIDDEN);
        }
    });

    renderDownloads();
    void pollDownloads();
})();
