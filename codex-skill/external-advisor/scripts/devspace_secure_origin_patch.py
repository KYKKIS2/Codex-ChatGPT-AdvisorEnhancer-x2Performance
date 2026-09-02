#!/usr/bin/env python3
"""Add a pinned-root, trusted-local-gateway mode to a patched DevSpace install."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path


CONFIG_NEEDLE = """        publicBaseUrl,
        toolMode: parseToolMode(env),"""
CONFIG_PATCHED = """        publicBaseUrl,
        trustedProxyAuthFile: env.DEVSPACE_TRUSTED_PROXY_AUTH_FILE
            ? resolve(expandHomePath(env.DEVSPACE_TRUSTED_PROXY_AUTH_FILE))
            : undefined,
        pinnedExactRootFile: env.DEVSPACE_PINNED_EXACT_ROOT_FILE
            ? resolve(expandHomePath(env.DEVSPACE_PINNED_EXACT_ROOT_FILE))
            : undefined,
        toolMode: parseToolMode(env),"""

CRYPTO_IMPORT_NEEDLE = 'import { randomUUID } from "node:crypto";'
CRYPTO_IMPORT_LEGACY_PATCHED = (
    'import { randomUUID, timingSafeEqual } from "node:crypto";'
)
CRYPTO_IMPORT_PATCHED = (
    'import { createHash, randomUUID, timingSafeEqual } from "node:crypto";'
)
FS_IMPORT_NEEDLE = 'import { readFileSync, realpathSync } from "node:fs";'
FS_IMPORT_PATCHED = 'import { lstatSync, readFileSync, realpathSync } from "node:fs";'

HELPER_MARKER = "function loadAdvisorTrustedProxySecret(config) {"
HELPER_INSERT_NEEDLE = "export function createServer(config = loadConfig()) {"
HELPERS = r'''function loadAdvisorTrustedProxySecret(config) {
    const path = config.trustedProxyAuthFile;
    if (!path)
        return undefined;
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink())
        throw new Error("Trusted proxy secret must be a regular file.");
    if ((metadata.mode & 0o077) !== 0)
        throw new Error("Trusted proxy secret must not be group- or world-accessible.");
    if (typeof process.getuid === "function" && metadata.uid !== process.getuid())
        throw new Error("Trusted proxy secret must be owned by the DevSpace service user.");
    const secret = readFileSync(path, "utf8").trim();
    if (!/^[A-Za-z0-9_-]{43,128}$/.test(secret))
        throw new Error("Trusted proxy secret must be a 32-byte-or-longer base64url value.");
    return Buffer.from(secret, "utf8");
}
function advisorTrustedProxyRequest(req, expectedSecret) {
    const remoteAddress = req.socket.remoteAddress;
    const localPeer = remoteAddress === undefined
        || remoteAddress === "127.0.0.1"
        || remoteAddress === "::1"
        || remoteAddress === "::ffff:127.0.0.1";
    if (!localPeer)
        return false;
    const presented = req.header("x-advisor-gateway-secret") ?? "";
    const actual = Buffer.from(presented, "utf8");
    return actual.length === expectedSecret.length && timingSafeEqual(actual, expectedSecret);
}
function advisorPinnedExactRoot(config) {
    const pointer = config.pinnedExactRootFile;
    if (!pointer)
        return undefined;
    const metadata = lstatSync(pointer);
    if (!metadata.isFile() || metadata.isSymbolicLink())
        throw new Error("Pinned DevSpace root pointer must be a regular file.");
    if ((metadata.mode & 0o077) !== 0)
        throw new Error("Pinned DevSpace root pointer must not be group- or world-accessible.");
    if (typeof process.getuid === "function" && metadata.uid !== process.getuid())
        throw new Error("Pinned DevSpace root pointer must be owned by the DevSpace service user.");
    const configured = readFileSync(pointer, "utf8").trim();
    if (!configured)
        throw new Error("Pinned DevSpace root pointer is empty.");
    return realpathSync(configured);
}
function assertAdvisorPinnedOpen(config, path, mode, baseRef) {
    const pinned = advisorPinnedExactRoot(config);
    if (!pinned)
        return;
    if ((mode ?? "checkout") !== "checkout" || baseRef !== undefined)
        throw new Error("Pinned DevSpace permits checkout mode only.");
    if (realpathSync(path) !== pinned)
        throw new Error("Pinned DevSpace denied a workspace outside its exact root.");
}
function assertAdvisorPinnedWorkspace(config, workspace) {
    const pinned = advisorPinnedExactRoot(config);
    if (pinned && realpathSync(workspace.root) !== pinned)
        throw new Error("DevSpace workspace is no longer the pinned exact root.");
}
'''

PINNED_ROOT_NEEDLE = '''    if (!metadata.isFile() || metadata.isSymbolicLink())
        throw new Error("Pinned DevSpace root pointer must be a regular file.");
    const configured = readFileSync(pointer, "utf8").trim();'''
PINNED_ROOT_PATCHED = '''    if (!metadata.isFile() || metadata.isSymbolicLink())
        throw new Error("Pinned DevSpace root pointer must be a regular file.");
    if ((metadata.mode & 0o077) !== 0)
        throw new Error("Pinned DevSpace root pointer must not be group- or world-accessible.");
    if (typeof process.getuid === "function" && metadata.uid !== process.getuid())
        throw new Error("Pinned DevSpace root pointer must be owned by the DevSpace service user.");
    const configured = readFileSync(pointer, "utf8").trim();'''

OAUTH_NEEDLE = """    const oauthProvider = new SingleUserOAuthProvider(config.oauth, mcpUrl, config.stateDir);
    const bearerAuth = requireBearerAuth({
        verifier: oauthProvider,
        requiredScopes: [config.oauth.scopes[0] ?? "devspace"],
        resourceMetadataUrl: getOAuthProtectedResourceMetadataUrl(resourceServerUrl),
    });"""
OAUTH_PATCHED = """    const trustedProxySecret = loadAdvisorTrustedProxySecret(config);
    const oauthProvider = trustedProxySecret
        ? undefined
        : new SingleUserOAuthProvider(config.oauth, mcpUrl, config.stateDir);
    const bearerAuth = oauthProvider
        ? requireBearerAuth({
            verifier: oauthProvider,
            requiredScopes: [config.oauth.scopes[0] ?? "devspace"],
            resourceMetadataUrl: getOAuthProtectedResourceMetadataUrl(resourceServerUrl),
        })
        : undefined;"""

AUTH_ROUTER_NEEDLE = """    app.use(mcpAuthRouter({
        provider: oauthProvider,
        issuerUrl: new URL(config.publicBaseUrl),
        baseUrl: new URL(config.publicBaseUrl),
        resourceServerUrl,
        scopesSupported: config.oauth.scopes,
        resourceName: "DevSpace",
    }));"""
AUTH_ROUTER_PATCHED = """    if (oauthProvider) {
        app.use(mcpAuthRouter({
            provider: oauthProvider,
            issuerUrl: new URL(config.publicBaseUrl),
            baseUrl: new URL(config.publicBaseUrl),
            resourceServerUrl,
            scopesSupported: config.oauth.scopes,
            resourceName: "DevSpace",
        }));
    }"""

REQUEST_AUTH_NEEDLE = """        await new Promise((resolve, reject) => {
            bearerAuth(req, res, (error) => {
                if (error)
                    reject(error);
                else
                    resolve();
            });
        });
        if (res.headersSent)
            return;
        if (!req.auth?.resource || !checkResourceAllowed({ requestedResource: req.auth.resource, configuredResource: resourceServerUrl })) {
            logEvent(config.logging, "warn", "auth_denied", {
                requestId,
                method: req.method,
                path: requestPath(req),
                reason: "invalid_oauth_resource",
                ...requestLogFields(req, config),
            });
            sendJsonRpcError(res, 401, -32001, "Unauthorized");
            return;
        }"""
REQUEST_AUTH_PATCHED = """        if (trustedProxySecret) {
            if (!advisorTrustedProxyRequest(req, trustedProxySecret)) {
                logEvent(config.logging, "warn", "auth_denied", {
                    requestId,
                    method: req.method,
                    path: requestPath(req),
                    reason: "invalid_trusted_proxy",
                });
                sendJsonRpcError(res, 401, -32001, "Unauthorized");
                return;
            }
        }
        else {
            await new Promise((resolve, reject) => {
                bearerAuth(req, res, (error) => {
                    if (error)
                        reject(error);
                    else
                        resolve();
                });
            });
            if (res.headersSent)
                return;
            if (!req.auth?.resource || !checkResourceAllowed({ requestedResource: req.auth.resource, configuredResource: resourceServerUrl })) {
                logEvent(config.logging, "warn", "auth_denied", {
                    requestId,
                    method: req.method,
                    path: requestPath(req),
                    reason: "invalid_oauth_resource",
                    ...requestLogFields(req, config),
                });
                sendJsonRpcError(res, 401, -32001, "Unauthorized");
                return;
            }
        }"""

CLOSE_NEEDLE = "            oauthProvider.close();"
CLOSE_PATCHED = "            oauthProvider?.close();"

TRANSPORT_MAP_NEEDLE = """    const transports = new Map();"""
TRANSPORT_RETENTION_PATCHED_LEGACY = r'''    const transports = new Map();
    const transportIdleTimers = new Map();
    const transportIdleTtlMs = Number(
        process.env.DEVSPACE_MCP_SESSION_IDLE_TTL_MS ?? 300000
    );
    if (!Number.isInteger(transportIdleTtlMs)
        || transportIdleTtlMs < 1000
        || transportIdleTtlMs > 3600000) {
        throw new Error("DevSpace MCP session idle TTL must be an integer from 1000 through 3600000 milliseconds.");
    }
    const cancelTransportIdleClose = (sessionId) => {
        const timer = transportIdleTimers.get(sessionId);
        if (timer)
            clearTimeout(timer);
        transportIdleTimers.delete(sessionId);
    };
    const closeTransport = (transport, event, fields = {}) => {
        Promise.resolve()
            .then(() => transport.close())
            .catch((error) => {
            logEvent(config.logging, "error", "mcp_session_close_error", {
                event,
                error: error instanceof Error ? error.message : String(error),
                ...fields,
            });
        });
    };
    const scheduleTransportIdleClose = (transport) => {
        const sessionId = transport.sessionId;
        if (!sessionId)
            return;
        cancelTransportIdleClose(sessionId);
        const timer = setTimeout(() => {
            transportIdleTimers.delete(sessionId);
            if (transports.get(sessionId) !== transport)
                return;
            transports.delete(sessionId);
            logEvent(config.logging, "info", "mcp_session_expired", {
                sessionIdPrefix: sessionIdPrefix(sessionId),
                idleTtlMs: transportIdleTtlMs,
            });
            closeTransport(transport, "idle_expiry", {
                sessionIdPrefix: sessionIdPrefix(sessionId),
            });
        }, transportIdleTtlMs);
        timer.unref?.();
        transportIdleTimers.set(sessionId, timer);
    };'''
TRANSPORT_RETENTION_PATCHED = r'''    const transports = new Map();
    const transportIdleTimers = new Map();
    const transportActiveRequests = new Map();
    const transportIdleTtlMs = Number(
        process.env.DEVSPACE_MCP_SESSION_IDLE_TTL_MS ?? 300000
    );
    if (!Number.isInteger(transportIdleTtlMs)
        || transportIdleTtlMs < 1000
        || transportIdleTtlMs > 3600000) {
        throw new Error("DevSpace MCP session idle TTL must be an integer from 1000 through 3600000 milliseconds.");
    }
    const cancelTransportIdleClose = (sessionId) => {
        const timer = transportIdleTimers.get(sessionId);
        if (timer)
            clearTimeout(timer);
        transportIdleTimers.delete(sessionId);
    };
    const closeTransport = (transport, event, fields = {}) => {
        Promise.resolve()
            .then(() => transport.close())
            .catch((error) => {
            logEvent(config.logging, "error", "mcp_session_close_error", {
                event,
                error: error instanceof Error ? error.message : String(error),
                ...fields,
            });
        });
    };
    const beginTransportRequest = (transport) => {
        const sessionId = transport.sessionId;
        if (!sessionId)
            return;
        cancelTransportIdleClose(sessionId);
        transportActiveRequests.set(
            sessionId,
            (transportActiveRequests.get(sessionId) ?? 0) + 1,
        );
    };
    const scheduleTransportIdleClose = (transport) => {
        const sessionId = transport.sessionId;
        if (!sessionId
            || transports.get(sessionId) !== transport
            || (transportActiveRequests.get(sessionId) ?? 0) !== 0) {
            return;
        }
        cancelTransportIdleClose(sessionId);
        const timer = setTimeout(() => {
            transportIdleTimers.delete(sessionId);
            if (transports.get(sessionId) !== transport
                || (transportActiveRequests.get(sessionId) ?? 0) !== 0) {
                return;
            }
            transports.delete(sessionId);
            transportActiveRequests.delete(sessionId);
            logEvent(config.logging, "info", "mcp_session_expired", {
                sessionIdPrefix: sessionIdPrefix(sessionId),
                idleTtlMs: transportIdleTtlMs,
            });
            closeTransport(transport, "idle_expiry", {
                sessionIdPrefix: sessionIdPrefix(sessionId),
            });
        }, transportIdleTtlMs);
        timer.unref?.();
        transportIdleTimers.set(sessionId, timer);
    };
    const finishTransportRequest = (transport) => {
        const sessionId = transport.sessionId;
        if (!sessionId)
            return;
        const activeRequests = transportActiveRequests.get(sessionId) ?? 0;
        if (activeRequests > 1) {
            transportActiveRequests.set(sessionId, activeRequests - 1);
            return;
        }
        transportActiveRequests.delete(sessionId);
        scheduleTransportIdleClose(transport);
    };'''

TRANSPORT_LOOKUP_NEEDLE = '''                transport = transports.get(sessionId);
                if (!transport) {
                    sendJsonRpcError(res, 404, -32000, "Unknown MCP session");
                    return;
                }'''
TRANSPORT_LOOKUP_PATCHED_LEGACY = TRANSPORT_LOOKUP_NEEDLE + '''
                cancelTransportIdleClose(sessionId);'''
TRANSPORT_LOOKUP_PATCHED = '''                transport = transports.get(sessionId);
                if (!transport) {
                    sendJsonRpcError(res, 404, -32000, "Unknown MCP session");
                    return;
                }
                beginTransportRequest(transport);'''

TRANSPORT_ONCLOSE_NEEDLE = '''                transport.onclose = () => {
                    const closedSessionId = transport?.sessionId;
                    if (closedSessionId) {
                        transports.delete(closedSessionId);
                        logEvent(config.logging, "info", "mcp_session_closed", {
                            sessionIdPrefix: sessionIdPrefix(closedSessionId),
                        });
                    }
                };'''
TRANSPORT_ONCLOSE_PATCHED_LEGACY = '''                transport.onclose = () => {
                    const closedSessionId = transport?.sessionId;
                    if (closedSessionId) {
                        cancelTransportIdleClose(closedSessionId);
                        transports.delete(closedSessionId);
                        logEvent(config.logging, "info", "mcp_session_closed", {
                            sessionIdPrefix: sessionIdPrefix(closedSessionId),
                        });
                    }
                };'''
TRANSPORT_ONCLOSE_PATCHED = '''                transport.onclose = () => {
                    const closedSessionId = transport?.sessionId;
                    if (closedSessionId
                        && transports.get(closedSessionId) === transport) {
                        cancelTransportIdleClose(closedSessionId);
                        transports.delete(closedSessionId);
                        transportActiveRequests.delete(closedSessionId);
                        logEvent(config.logging, "info", "mcp_session_closed", {
                            sessionIdPrefix: sessionIdPrefix(closedSessionId),
                        });
                    }
                };'''

TRANSPORT_HANDLE_NEEDLE = '''            await transport.handleRequest(req, res, req.body);'''
TRANSPORT_HANDLE_PATCHED_LEGACY = '''            try {
                await transport.handleRequest(req, res, req.body);
            }
            finally {
                scheduleTransportIdleClose(transport);
            }'''
TRANSPORT_HANDLE_PATCHED = '''            try {
                await transport.handleRequest(req, res, req.body);
            }
            finally {
                finishTransportRequest(transport);
            }'''

TRANSPORT_SHUTDOWN_NEEDLE = '''            processSessions.shutdown();
            oauthProvider?.close();'''
TRANSPORT_SHUTDOWN_PATCHED_LEGACY = '''            processSessions.shutdown();
            for (const timer of transportIdleTimers.values())
                clearTimeout(timer);
            transportIdleTimers.clear();
            for (const transport of transports.values())
                closeTransport(transport, "server_shutdown");
            transports.clear();
            oauthProvider?.close();'''
TRANSPORT_SHUTDOWN_PATCHED = '''            processSessions.shutdown();
            for (const timer of transportIdleTimers.values())
                clearTimeout(timer);
            transportIdleTimers.clear();
            for (const transport of transports.values())
                closeTransport(transport, "server_shutdown");
            transports.clear();
            transportActiveRequests.clear();
            oauthProvider?.close();'''

OPEN_NEEDLE = """        assertAdvisorReadonlyOpen(config, path, mode, baseRef);
        const { workspace, agentsFiles, availableAgentsFiles } = await workspaces.openWorkspace({ path, mode, baseRef });
        assertAdvisorReadonlyWorkspace(config, workspace);"""
OPEN_PATCHED = """        assertAdvisorReadonlyOpen(config, path, mode, baseRef);
        assertAdvisorPinnedOpen(config, path, mode, baseRef);
        const { workspace, agentsFiles, availableAgentsFiles } = await workspaces.openWorkspace({ path, mode, baseRef });
        assertAdvisorReadonlyWorkspace(config, workspace);
        assertAdvisorPinnedWorkspace(config, workspace);"""

WORKSPACE_NEEDLE = """        const workspace = workspaces.getWorkspace(workspaceId);
        assertAdvisorReadonlyWorkspace(config, workspace);"""
WORKSPACE_PATCHED = """        const workspace = workspaces.getWorkspace(workspaceId);
        assertAdvisorReadonlyWorkspace(config, workspace);
        assertAdvisorPinnedWorkspace(config, workspace);"""
WORKSPACE_DUPLICATE_PATCHED = WORKSPACE_PATCHED + """
        assertAdvisorPinnedWorkspace(config, workspace);"""

PROCESS_HELPER_MARKER = (
    "const ADVISOR_PROCESS_SHELL_SANDBOX = process.env.DEVSPACE_SHELL_SANDBOX;"
)
PROCESS_HELPER_INSERT_NEEDLE = (
    'const WORKSPACE_APP_URI = "ui://devspace/workspace-app.html";'
)
PROCESS_HELPERS_LEGACY = r'''const ADVISOR_PROCESS_SHELL_SANDBOX = process.env.DEVSPACE_SHELL_SANDBOX;
function advisorSandboxedProcessCommand(command, cwd, root) {
    if (!ADVISOR_PROCESS_SHELL_SANDBOX)
        return command;
    if (ADVISOR_PROCESS_SHELL_SANDBOX !== "/opt/advisor/devspace_shell_sandbox.py")
        throw new Error("Secure DevSpace process sandbox path is not pinned.");
    const payload = Buffer.from(JSON.stringify({
        command,
        cwd,
        root,
    }), "utf8").toString("base64url");
    return `${ADVISOR_PROCESS_SHELL_SANDBOX} ${payload}`;
}
'''
PROCESS_IDENTITY_MARKER = "function advisorProcessExecutionIdentity("
PROCESS_HELPERS = PROCESS_HELPERS_LEGACY.rstrip() + r'''
function advisorProcessDigest(value) {
    return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
function advisorProcessExecutionIdentity({ root, cwd, command, tty, columns, rows, executionKey }) {
    const requestFingerprint = advisorProcessDigest({
        version: 1,
        root: realpathSync(root),
        cwd,
        command,
        tty: Boolean(tty),
        columns: columns ?? 80,
        rows: rows ?? 24,
    });
    const replayKey = executionKey
        ? advisorProcessDigest({
            version: 1,
            root: realpathSync(root),
            executionKey,
        })
        : requestFingerprint;
    return { replayKey, requestFingerprint };
}
function advisorProcessMaxActive(config) {
    if (config.toolMode !== "full")
        return undefined;
    const value = Number(
        config.processMaxActive
        ?? process.env.DEVSPACE_PROCESS_MAX_ACTIVE
        ?? 8
    );
    if (!Number.isInteger(value) || value < 1 || value > 64)
        throw new Error("Secure DevSpace active-process limit must be an integer from 1 through 64.");
    return value;
}
'''
PROCESS_MAX_ACTIVE_MARKER = "function advisorProcessMaxActive("
PROCESS_MAX_ACTIVE_HELPER_LEGACY = r'''function advisorProcessMaxActive(config) {
    if (config.toolMode !== "full")
        return undefined;
    const value = Number(process.env.DEVSPACE_PROCESS_MAX_ACTIVE);
    if (!Number.isInteger(value) || value < 1 || value > 64)
        throw new Error("Secure DevSpace active-process limit must be an integer from 1 through 64.");
    return value;
}
'''
PROCESS_MAX_ACTIVE_HELPER_CONFIG_ONLY = r'''function advisorProcessMaxActive(config) {
    if (config.toolMode !== "full")
        return undefined;
    const value = config.processMaxActive;
    if (!Number.isInteger(value) || value < 1 || value > 64)
        throw new Error("Secure DevSpace active-process limit must be an integer from 1 through 64.");
    return value;
}
'''
PROCESS_MAX_ACTIVE_HELPER = r'''function advisorProcessMaxActive(config) {
    if (config.toolMode !== "full")
        return undefined;
    const value = Number(
        config.processMaxActive
        ?? process.env.DEVSPACE_PROCESS_MAX_ACTIVE
        ?? 8
    );
    if (!Number.isInteger(value) || value < 1 || value > 64)
        throw new Error("Secure DevSpace active-process limit must be an integer from 1 through 64.");
    return value;
}
'''

PROCESS_START_NEEDLE = '''        const snapshot = await processSessions.start({
            workspaceId,
            command: cmd,
            cwd,
            workspaceRoot: workspace.root,'''
PROCESS_START_LEGACY_PATCHED = '''        const command = advisorSandboxedProcessCommand(
            cmd,
            cwd,
            workspace.root,
        );
        const snapshot = await processSessions.start({
            workspaceId,
            command,
            cwd,
            workspaceRoot: workspace.root,'''
PROCESS_START_PATCHED = '''        const identity = advisorProcessExecutionIdentity({
            root: workspace.root,
            cwd,
            command: cmd,
            tty,
            columns,
            rows,
            executionKey,
        });
        const command = advisorSandboxedProcessCommand(
            cmd,
            cwd,
            workspace.root,
        );
        const snapshot = await processSessions.start({
            workspaceId,
            command,
            cwd,
            workspaceRoot: workspace.root,
            replayKey: identity.replayKey,
            requestFingerprint: identity.requestFingerprint,
            allowConcurrentDuplicate,'''

PROCESS_RESULT_NEEDLE = '''function processResult(snapshot) {
    const status = snapshot.running
        ? `Process running with session ID ${snapshot.sessionId}.`
        : snapshot.signal
            ? `Process exited after signal ${snapshot.signal}.`
            : `Process exited with code ${snapshot.exitCode ?? "unknown"}.`;
    return snapshot.output ? `${snapshot.output.replace(/\\n$/, "")}\\n${status}` : status;
}
function processOutputSchema() {
    return resultOutputSchema({
        sessionId: z.number().optional(),
        running: z.boolean(),
        exitCode: z.number().int().optional(),
        signal: z.string().optional(),
        wallTimeMs: z.number().nonnegative(),
        outputTruncated: z.boolean(),
    });
}
function processToolResponse(tool, workspaceId, snapshot, summary) {
    const result = processResult(snapshot);
    const content = [textBlock(result)];
    const outputSummary = textSummary(snapshot.output ? [textBlock(snapshot.output)] : []);
    return {
        content,
        _meta: {
            tool,
            card: {
                workspaceId,
                summary: { ...summary, ...outputSummary },
                payload: { content },
            },
        },
        structuredContent: {
            result,
            sessionId: snapshot.sessionId,
            running: snapshot.running,
            exitCode: snapshot.exitCode,
            signal: snapshot.signal,
            wallTimeMs: snapshot.wallTimeMs,
            outputTruncated: snapshot.outputTruncated,
        },
    };
}'''
PROCESS_RESULT_PATCHED = '''function processResult(snapshot) {
    const status = snapshot.running
        ? `Process running with session ID ${snapshot.sessionId}.`
        : snapshot.signal
            ? `Process exited after signal ${snapshot.signal}.`
            : `Process exited with code ${snapshot.exitCode ?? "unknown"}.`;
    const replay = snapshot.reused
        ? "Reused the matching execution; no duplicate process was started.\\n"
        : "";
    return snapshot.output
        ? `${replay}${snapshot.output.replace(/\\n$/, "")}\\n${status}`
        : `${replay}${status}`;
}
function processOutputSchema() {
    return resultOutputSchema({
        workspaceId: z.string(),
        sessionId: z.number(),
        reused: z.boolean(),
        running: z.boolean(),
        exitCode: z.number().int().optional(),
        signal: z.string().optional(),
        wallTimeMs: z.number().nonnegative(),
        outputTruncated: z.boolean(),
    });
}
function processToolResponse(tool, workspaceId, snapshot, summary) {
    const ownerWorkspaceId = snapshot.workspaceId ?? workspaceId;
    const result = processResult(snapshot);
    const content = [textBlock(result)];
    const outputSummary = textSummary(snapshot.output ? [textBlock(snapshot.output)] : []);
    return {
        content,
        _meta: {
            tool,
            card: {
                workspaceId: ownerWorkspaceId,
                summary: { ...summary, reused: snapshot.reused, ...outputSummary },
                payload: { content },
            },
        },
        structuredContent: {
            result,
            workspaceId: ownerWorkspaceId,
            sessionId: snapshot.sessionId,
            reused: snapshot.reused,
            running: snapshot.running,
            exitCode: snapshot.exitCode,
            signal: snapshot.signal,
            wallTimeMs: snapshot.wallTimeMs,
            outputTruncated: snapshot.outputTruncated,
        },
    };
}'''

PROCESS_DESCRIPTION_NEEDLE = '''        description: "Run a command inside an open workspace. Returns its result when it exits during the yield window, otherwise returns a sessionId for write_stdin. Use this for file inspection, tests, builds, package scripts, and long-running processes. Call open_workspace first and pass workspaceId.",'''
PROCESS_DESCRIPTION_PATCHED = '''        description: "Run a command asynchronously inside an open workspace. It returns within 30 seconds with a durable session handle when still running; use write_stdin to poll or interact. Exact matching retries reuse the active or recently completed execution. Reuse the same executionKey across retries, use a distinct executionKey for each intentionally parallel identical command, and reserve allowConcurrentDuplicate for deliberate unprotected duplicates. Call open_workspace first and pass workspaceId.",'''

PROCESS_EXEC_SCHEMA_NEEDLE = '''            yieldTimeMs: z
                .number()
                .int()
                .min(0)
                .max(30_000)
                .optional()
                .describe("Milliseconds to wait before returning a running session. Defaults to 10000."),
            maxOutputTokens: z
                .number()
                .int()
                .positive()
                .max(100_000)
                .optional()
                .describe("Approximate output token budget. Defaults to 10000."),
        },
        outputSchema: processOutputSchema(),'''
PROCESS_EXEC_SCHEMA_PATCHED = '''            yieldTimeMs: z
                .number()
                .int()
                .min(0)
                .max(30_000)
                .optional()
                .describe("Milliseconds to wait before returning a running session. Defaults to 10000."),
            maxOutputTokens: z
                .number()
                .int()
                .positive()
                .max(100_000)
                .optional()
                .describe("Approximate output token budget. Defaults to 10000."),
            executionKey: z
                .string()
                .min(1)
                .max(128)
                .regex(/^[A-Za-z0-9._:-]+$/)
                .optional()
                .describe("Stable idempotency key. Reuse it for retries; use a distinct key for an intentionally separate identical run."),
            allowConcurrentDuplicate: z
                .boolean()
                .optional()
                .describe("Bypass replay protection and always start another matching command. Defaults to false; prefer a distinct executionKey."),
        },
        outputSchema: processOutputSchema(),'''

PROCESS_HANDLER_NEEDLE = '''    }, async ({ workspaceId, cmd, tty, columns, rows, workingDirectory, yieldTimeMs, maxOutputTokens }) => {'''
PROCESS_HANDLER_PATCHED = '''    }, async ({ workspaceId, cmd, tty, columns, rows, workingDirectory, yieldTimeMs, maxOutputTokens, executionKey, allowConcurrentDuplicate }) => {'''

PROCESS_WRITE_NEEDLE = '''        workspaces.getWorkspace(workspaceId);
        const snapshot = await processSessions.write({
            workspaceId,
            sessionId,'''
PROCESS_WRITE_PATCHED = '''        const workspace = workspaces.getWorkspace(workspaceId);
        assertAdvisorReadonlyWorkspace(config, workspace);
        assertAdvisorPinnedWorkspace(config, workspace);
        const snapshot = await processSessions.write({
            workspaceId,
            workspaceRoot: workspace.root,
            sessionId,'''

PROCESS_REGISTER_NEEDLE = '''    if (config.toolMode === "codex") {
        registerCodexProcessTools(server, config, workspaces, processSessions);
    }'''
PROCESS_REGISTER_PATCHED = '''    if (config.toolMode === "codex" || config.toolMode === "full") {
        registerCodexProcessTools(server, config, workspaces, processSessions);
    }'''

PROCESS_MANAGER_FACTORY_NEEDLE = '''    const processSessions = new ProcessSessionManager();'''
PROCESS_MANAGER_FACTORY_PATCHED = '''    const processSessions = new ProcessSessionManager({
        maxActiveSessions: advisorProcessMaxActive(config),
    });'''

PROCESS_BUFFER_NEEDLE = '''    drain(maxCharacters) {
        if (!Number.isInteger(maxCharacters) || maxCharacters < 1) {
            throw new Error("Output limit must be a positive integer.");
        }
        const omittedByBuffer = Math.max(0, this.totalCharacters - codePointLength(this.head) - codePointLength(this.tail));
        const retained = formatHeadTail(this.head, this.tail, omittedByBuffer);
        const output = truncateOutput(retained, maxCharacters);
        const truncated = omittedByBuffer > 0 || output.truncated;
        this.head = "";
        this.tail = "";
        this.totalCharacters = 0;
        return { output: output.output, truncated };
    }'''
PROCESS_BUFFER_PATCHED = '''    snapshot(maxCharacters) {
        if (!Number.isInteger(maxCharacters) || maxCharacters < 1) {
            throw new Error("Output limit must be a positive integer.");
        }
        const omittedByBuffer = Math.max(0, this.totalCharacters - codePointLength(this.head) - codePointLength(this.tail));
        const retained = formatHeadTail(this.head, this.tail, omittedByBuffer);
        const output = truncateOutput(retained, maxCharacters);
        return {
            output: output.output,
            truncated: omittedByBuffer > 0 || output.truncated,
        };
    }
    drain(maxCharacters) {
        const snapshot = this.snapshot(maxCharacters);
        this.head = "";
        this.tail = "";
        this.totalCharacters = 0;
        return snapshot;
    }'''

PROCESS_FIELDS_NEEDLE = '''export class ProcessSessionManager {
    sessions = new Map();
    maxBufferCharacters;'''
PROCESS_FIELDS_PATCHED = '''export class ProcessSessionManager {
    sessions = new Map();
    replaySessions = new Map();
    maxBufferCharacters;'''

PROCESS_LIMIT_FIELDS_NEEDLE = '''    completedSessionTtlMs;
    nextSessionId = 1;'''
PROCESS_LIMIT_FIELDS_PATCHED = '''    completedSessionTtlMs;
    maxActiveSessions;
    nextSessionId = 1;'''

PROCESS_LIMIT_CONSTRUCTOR_NEEDLE = '''    constructor(options = {}) {
        this.maxBufferCharacters = options.maxBufferCharacters ?? DEFAULT_BUFFER_CHARACTERS;
        this.completedSessionTtlMs = options.completedSessionTtlMs ?? COMPLETED_SESSION_TTL_MS;
    }'''
PROCESS_LIMIT_CONSTRUCTOR_PATCHED = '''    constructor(options = {}) {
        this.maxBufferCharacters = options.maxBufferCharacters ?? DEFAULT_BUFFER_CHARACTERS;
        this.completedSessionTtlMs = options.completedSessionTtlMs ?? COMPLETED_SESSION_TTL_MS;
        if (options.maxActiveSessions !== undefined
            && (!Number.isInteger(options.maxActiveSessions)
                || options.maxActiveSessions < 1
                || options.maxActiveSessions > 64)) {
            throw new Error("Active process-session limit must be an integer from 1 through 64.");
        }
        this.maxActiveSessions = options.maxActiveSessions ?? Number.POSITIVE_INFINITY;
    }'''

PROCESS_MANAGER_START_NEEDLE = '''    async start(input) {
        const session = this.createSession(input);
        this.sessions.set(session.id, session);
        try {
            if (input.tty && process.platform !== "win32")
                await this.startPty(session, input);
            else
                this.startPipe(session, input);
        }
        catch (error) {
            this.sessions.delete(session.id);
            throw error;
        }
        const yieldTimeMs = boundedInteger(input.yieldTimeMs, DEFAULT_EXEC_YIELD_MS, MAX_COMMAND_YIELD_MS);
        await this.waitForExit(session, yieldTimeMs);
        const snapshot = this.consume(session, input.maxOutputTokens);
        if (!session.running)
            this.removeSession(session.id);
        return snapshot;
    }'''
PROCESS_MANAGER_START_REPLAY_PATCHED = '''    async start(input) {
        const replay = this.findReplay(input);
        if (replay)
            return this.replaySnapshot(replay, input.maxOutputTokens);
        const session = this.createSession(input);
        this.sessions.set(session.id, session);
        if (session.replayKey)
            this.replaySessions.set(session.replayKey, session.id);
        try {
            if (input.tty && process.platform !== "win32")
                await this.startPty(session, input);
            else
                this.startPipe(session, input);
        }
        catch (error) {
            this.removeSession(session.id);
            throw error;
        }
        const yieldTimeMs = boundedInteger(input.yieldTimeMs, DEFAULT_EXEC_YIELD_MS, MAX_COMMAND_YIELD_MS);
        await this.waitForExit(session, yieldTimeMs);
        return this.consume(session, input.maxOutputTokens);
    }'''
PROCESS_MANAGER_START_PATCHED = '''    async start(input) {
        const replay = this.findReplay(input);
        if (replay)
            return this.replaySnapshot(replay, input.maxOutputTokens);
        const activeSessions = Array.from(this.sessions.values())
            .filter((session) => session.running)
            .length;
        if (activeSessions >= this.maxActiveSessions)
            throw new Error(`Active process-session limit reached (${this.maxActiveSessions}). Poll or stop an existing session before starting another.`);
        const session = this.createSession(input);
        this.sessions.set(session.id, session);
        if (session.replayKey)
            this.replaySessions.set(session.replayKey, session.id);
        try {
            if (input.tty && process.platform !== "win32")
                await this.startPty(session, input);
            else
                this.startPipe(session, input);
        }
        catch (error) {
            this.removeSession(session.id);
            throw error;
        }
        const yieldTimeMs = boundedInteger(input.yieldTimeMs, DEFAULT_EXEC_YIELD_MS, MAX_COMMAND_YIELD_MS);
        await this.waitForExit(session, yieldTimeMs);
        return this.consume(session, input.maxOutputTokens);
    }'''

PROCESS_MANAGER_WRITE_NEEDLE = '''    async write(input) {
        const session = this.getOwnedSession(input.workspaceId, input.sessionId);
        const chars = input.chars ?? "";
        const interactionRequested = chars.length > 0 || input.columns !== undefined || input.rows !== undefined;
        if (input.columns !== undefined || input.rows !== undefined) {
            session.columns = terminalSize(input.columns, session.columns);
            session.rows = terminalSize(input.rows, session.rows);
            if (!session.process?.resize) {
                throw new Error(`Process session ${session.id} is not a PTY and cannot be resized.`);
            }
            session.process.resize(session.columns, session.rows);
        }
        const interruptRequested = chars.includes("\\u0003") && session.running;
        if (interruptRequested) {
            session.process?.kill("SIGINT");
        }
        const writableChars = chars.replaceAll("\\u0003", "");
        if (writableChars && session.running)
            session.process?.write(writableChars);
        if ((interactionRequested || !session.buffer.hasOutput()) && session.running) {
            const fallback = interactionRequested ? DEFAULT_INTERACTIVE_YIELD_MS : DEFAULT_POLL_YIELD_MS;
            const maximum = interactionRequested ? MAX_COMMAND_YIELD_MS : MAX_POLL_YIELD_MS;
            const yieldTimeMs = boundedInteger(input.yieldTimeMs, fallback, maximum);
            await this.waitForExit(session, yieldTimeMs);
        }
        const snapshot = this.consume(session, input.maxOutputTokens);
        if (!session.running)
            this.removeSession(session.id);
        return snapshot;
    }'''
PROCESS_MANAGER_WRITE_PATCHED = '''    async write(input) {
        const session = this.getOwnedSession(input.workspaceId, input.sessionId, input.workspaceRoot);
        const chars = input.chars ?? "";
        const interactionRequested = chars.length > 0 || input.columns !== undefined || input.rows !== undefined;
        if (input.columns !== undefined || input.rows !== undefined) {
            session.columns = terminalSize(input.columns, session.columns);
            session.rows = terminalSize(input.rows, session.rows);
            if (!session.process?.resize) {
                throw new Error(`Process session ${session.id} is not a PTY and cannot be resized.`);
            }
            session.process.resize(session.columns, session.rows);
        }
        const interruptRequested = chars.includes("\\u0003") && session.running;
        if (interruptRequested) {
            session.process?.kill("SIGINT");
        }
        const writableChars = chars.replaceAll("\\u0003", "");
        if (writableChars && session.running)
            session.process?.write(writableChars);
        if ((interactionRequested || !session.buffer.hasOutput()) && session.running) {
            const fallback = interactionRequested ? DEFAULT_INTERACTIVE_YIELD_MS : DEFAULT_POLL_YIELD_MS;
            const maximum = interactionRequested ? MAX_COMMAND_YIELD_MS : MAX_POLL_YIELD_MS;
            const yieldTimeMs = boundedInteger(input.yieldTimeMs, fallback, maximum);
            await this.waitForExit(session, yieldTimeMs);
        }
        return this.consume(session, input.maxOutputTokens);
    }'''

PROCESS_MANAGER_SHUTDOWN_NEEDLE = '''    shutdown() {
        for (const session of this.sessions.values()) {
            if (session.cleanupTimer)
                clearTimeout(session.cleanupTimer);
            if (session.running)
                session.process?.kill("SIGTERM");
        }
        this.sessions.clear();
    }'''
PROCESS_MANAGER_SHUTDOWN_PATCHED = '''    shutdown() {
        for (const session of this.sessions.values()) {
            if (session.cleanupTimer)
                clearTimeout(session.cleanupTimer);
            if (session.running)
                session.process?.kill("SIGTERM");
        }
        this.sessions.clear();
        this.replaySessions.clear();
    }'''

PROCESS_MANAGER_CREATE_NEEDLE = '''    createSession(input) {
        let resolveExit = () => undefined;
        const exitPromise = new Promise((resolve) => {
            resolveExit = resolve;
        });
        return {
            id: this.nextSessionId++,
            workspaceId: input.workspaceId,
            startedAt: Date.now(),
            columns: terminalSize(input.columns, DEFAULT_COLUMNS),
            rows: terminalSize(input.rows, DEFAULT_ROWS),
            buffer: new HeadTailBuffer(this.maxBufferCharacters),
            running: true,
            exitPromise,
            resolveExit,
        };
    }'''
PROCESS_MANAGER_CREATE_PATCHED = '''    createSession(input) {
        let resolveExit = () => undefined;
        const exitPromise = new Promise((resolve) => {
            resolveExit = resolve;
        });
        return {
            id: this.nextSessionId++,
            workspaceId: input.workspaceId,
            workspaceRoot: input.workspaceRoot,
            replayKey: input.allowConcurrentDuplicate ? undefined : input.replayKey,
            requestFingerprint: input.requestFingerprint,
            startedAt: Date.now(),
            columns: terminalSize(input.columns, DEFAULT_COLUMNS),
            rows: terminalSize(input.rows, DEFAULT_ROWS),
            buffer: new HeadTailBuffer(this.maxBufferCharacters),
            history: new HeadTailBuffer(this.maxBufferCharacters),
            running: true,
            exitPromise,
            resolveExit,
        };
    }'''

PROCESS_MANAGER_FINISH_NEEDLE = '''    finish(session, exitCode, signal) {
        if (!session.running)
            return;
        session.running = false;
        session.exitCode = exitCode;
        session.signal = signal;
        session.resolveExit();
        session.cleanupTimer = setTimeout(() => this.sessions.delete(session.id), this.completedSessionTtlMs);
        session.cleanupTimer.unref();
    }
    append(session, output) {
        session.buffer.append(output);
    }'''
PROCESS_MANAGER_FINISH_PATCHED = '''    finish(session, exitCode, signal) {
        if (!session.running)
            return;
        session.running = false;
        session.exitCode = exitCode;
        session.signal = signal;
        session.resolveExit();
        session.cleanupTimer = setTimeout(() => this.removeSession(session.id), this.completedSessionTtlMs);
        session.cleanupTimer.unref();
    }
    append(session, output) {
        session.buffer.append(output);
        session.history.append(output);
    }'''

PROCESS_MANAGER_CONSUME_NEEDLE = '''    consume(session, maxOutputTokens) {
        const limit = boundedInteger(maxOutputTokens, DEFAULT_MAX_OUTPUT_TOKENS, 100_000);
        const maxCharacters = Math.max(256, limit * 4);
        const buffered = session.buffer.drain(maxCharacters);
        return {
            sessionId: session.running ? session.id : undefined,
            output: buffered.output,
            outputTruncated: buffered.truncated,
            running: session.running,
            exitCode: session.exitCode,
            signal: session.signal,
            wallTimeMs: Date.now() - session.startedAt,
        };
    }'''
PROCESS_MANAGER_CONSUME_PATCHED = '''    consume(session, maxOutputTokens) {
        const limit = boundedInteger(maxOutputTokens, DEFAULT_MAX_OUTPUT_TOKENS, 100_000);
        const maxCharacters = Math.max(256, limit * 4);
        const buffered = session.buffer.drain(maxCharacters);
        return this.sessionSnapshot(session, buffered, false);
    }
    replaySnapshot(session, maxOutputTokens) {
        const limit = boundedInteger(maxOutputTokens, DEFAULT_MAX_OUTPUT_TOKENS, 100_000);
        const maxCharacters = Math.max(256, limit * 4);
        return this.sessionSnapshot(session, session.history.snapshot(maxCharacters), true);
    }
    sessionSnapshot(session, buffered, reused) {
        return {
            workspaceId: session.workspaceId,
            sessionId: session.id,
            reused,
            output: buffered.output,
            outputTruncated: buffered.truncated,
            running: session.running,
            exitCode: session.exitCode,
            signal: session.signal,
            wallTimeMs: Date.now() - session.startedAt,
        };
    }
    findReplay(input) {
        if (!input.replayKey || input.allowConcurrentDuplicate)
            return undefined;
        const sessionId = this.replaySessions.get(input.replayKey);
        if (sessionId === undefined)
            return undefined;
        const session = this.sessions.get(sessionId);
        if (!session) {
            this.replaySessions.delete(input.replayKey);
            return undefined;
        }
        if (session.requestFingerprint !== input.requestFingerprint) {
            throw new Error("The executionKey is already bound to a different command request.");
        }
        return session;
    }'''

PROCESS_MANAGER_OWNERSHIP_NEEDLE = '''    getOwnedSession(workspaceId, sessionId) {
        const session = this.sessions.get(sessionId);
        if (!session)
            throw new Error(`Unknown process session: ${sessionId}`);
        if (session.workspaceId !== workspaceId) {
            throw new Error(`Process session ${sessionId} does not belong to workspace ${workspaceId}.`);
        }
        return session;
    }
    removeSession(sessionId) {
        const session = this.sessions.get(sessionId);
        if (session?.cleanupTimer)
            clearTimeout(session.cleanupTimer);
        this.sessions.delete(sessionId);
    }'''
PROCESS_MANAGER_OWNERSHIP_PATCHED = '''    getOwnedSession(workspaceId, sessionId, workspaceRoot) {
        const session = this.sessions.get(sessionId);
        if (!session)
            throw new Error(`Unknown process session: ${sessionId}`);
        const sameWorkspace = session.workspaceId === workspaceId;
        const sameRoot = workspaceRoot !== undefined
            && session.workspaceRoot !== undefined
            && session.workspaceRoot === workspaceRoot;
        if (!sameWorkspace && !sameRoot) {
            throw new Error(`Process session ${sessionId} does not belong to this workspace root.`);
        }
        return session;
    }
    removeSession(sessionId) {
        const session = this.sessions.get(sessionId);
        if (session?.cleanupTimer)
            clearTimeout(session.cleanupTimer);
        if (session?.replayKey && this.replaySessions.get(session.replayKey) === sessionId)
            this.replaySessions.delete(session.replayKey);
        this.sessions.delete(sessionId);
    }'''

PI_HELPER_MARKER = "const ADVISOR_SHELL_SANDBOX = process.env.DEVSPACE_SHELL_SANDBOX;"
PI_HELPER_INSERT_NEEDLE = 'import { resolveAllowedPath } from "./roots.js";'
PI_HELPERS = r'''import { resolveAllowedPath } from "./roots.js";
const ADVISOR_SHELL_SANDBOX = process.env.DEVSPACE_SHELL_SANDBOX;
function advisorSandboxedShellCommand(command, context) {
    if (!ADVISOR_SHELL_SANDBOX)
        return command;
    if (ADVISOR_SHELL_SANDBOX !== "/opt/advisor/devspace_shell_sandbox.py")
        throw new Error("Secure DevSpace shell sandbox path is not pinned.");
    const payload = Buffer.from(JSON.stringify({
        command,
        cwd: context.cwd,
        root: context.root,
    }), "utf8").toString("base64url");
    return `${ADVISOR_SHELL_SANDBOX} ${payload}`;
}'''

PI_SHELL_NEEDLE = '''export async function runShellTool(input, context) {
    const tool = createBashTool(context.cwd);
    const timeout = input.timeout === undefined ? 30 : Math.min(input.timeout, 300);
    return runTool((params) => tool.execute("run_shell", params), {
        command: input.command,
        timeout,
    }, context);
}'''
PI_SHELL_LEGACY_PATCHED = '''export async function runShellTool(input, context) {
    const tool = createBashTool(context.cwd);
    const timeout = input.timeout === undefined ? 30 : Math.min(input.timeout, 300);
    const command = advisorSandboxedShellCommand(input.command, context);
    return runTool((params) => tool.execute("run_shell", params), {
        command,
        timeout,
    }, context);
}'''
PI_SHELL_WINDOW_PATCHED = '''export async function runShellTool(input, context) {
    const tool = createBashTool(context.cwd);
    const maximum = context.shellMaxSeconds;
    if (!Number.isInteger(maximum) || maximum < 300 || maximum > 28800)
        throw new Error("Secure DevSpace shell timeout boundary is invalid.");
    const timeout = input.timeout === undefined ? maximum : Math.min(input.timeout, maximum);
    const command = advisorSandboxedShellCommand(input.command, context);
    return runTool((params) => tool.execute("run_shell", params), {
        command,
        timeout,
    }, context);
}'''
PI_SHELL_COMPAT_PATCHED = '''export async function runShellTool(input, context) {
    const tool = createBashTool(context.cwd);
    const maximum = context.shellMaxSeconds ?? 300;
    if (!Number.isInteger(maximum) || maximum < 300 || maximum > 28800)
        throw new Error("Secure DevSpace shell timeout boundary is invalid.");
    const timeout = input.timeout === undefined ? maximum : Math.min(input.timeout, maximum);
    const command = advisorSandboxedShellCommand(input.command, context);
    return runTool((params) => tool.execute("run_shell", params), {
        command,
        timeout,
    }, context);
}'''
PI_SHELL_LONG_SYNC_PATCHED = '''export async function runShellTool(input, context) {
    const tool = createBashTool(context.cwd);
    const configuredMaximum = context.shellMaxSeconds;
    const maximum = configuredMaximum ?? 300;
    if (!Number.isInteger(maximum) || maximum < 300 || maximum > 28800)
        throw new Error("Secure DevSpace shell timeout boundary is invalid.");
    const timeout = input.timeout === undefined
        ? (configuredMaximum ?? 30)
        : Math.min(input.timeout, maximum);
    const command = advisorSandboxedShellCommand(input.command, context);
    return runTool((params) => tool.execute("run_shell", params), {
        command,
        timeout,
    }, context);
}'''
PI_SHELL_PATCHED = '''export async function runShellTool(input, context) {
    const tool = createBashTool(context.cwd);
    const configuredMaximum = context.shellMaxSeconds;
    if (configuredMaximum !== undefined
        && (!Number.isInteger(configuredMaximum) || configuredMaximum < 300 || configuredMaximum > 28800))
        throw new Error("Secure DevSpace shell timeout boundary is invalid.");
    const maximum = Math.min(configuredMaximum ?? 90, 90);
    const timeout = input.timeout === undefined ? 30 : Math.min(input.timeout, maximum);
    const command = advisorSandboxedShellCommand(input.command, context);
    return runTool((params) => tool.execute("run_shell", params), {
        command,
        timeout,
    }, context);
}'''

SHELL_SCHEMA_NEEDLE = '''                timeout: z
                    .number()
                    .positive()
                    .max(300)
                    .optional()
                    .describe("Timeout in seconds. Defaults to 30, max 300."),'''
SHELL_SCHEMA_WINDOW_PATCHED = '''                timeout: z
                    .number()
                    .positive()
                    .max(config.shellMaxSeconds)
                    .optional()
                    .describe(`Timeout in seconds. Defaults to the connector exposure window; max ${config.shellMaxSeconds}. The expiry timer can stop it earlier.`),'''
SHELL_SCHEMA_LONG_SYNC_PATCHED = '''                timeout: z
                    .number()
                    .positive()
                    .max(config.shellMaxSeconds ?? 300)
                    .optional()
                    .describe(config.shellMaxSeconds
                        ? `Timeout in seconds. Defaults to the connector exposure window; max ${config.shellMaxSeconds}. The expiry timer can stop it earlier.`
                        : "Timeout in seconds. Defaults to 30, max 300."),'''
SHELL_SCHEMA_PATCHED = '''                timeout: z
                    .number()
                    .positive()
                    .max(Math.min(config.shellMaxSeconds ?? 90, 90))
                    .optional()
                    .describe("Timeout in seconds for short synchronous commands. Defaults to 30, max 90. Use exec_command and write_stdin for longer jobs."),'''
SYNC_SHELL_REGISTRATION_NEEDLE = '''    if (config.toolMode !== "codex" && config.toolMode !== "readonly") {
        registerAdvisorAppTool(server, toolNames.shell, {'''
SYNC_SHELL_REGISTRATION_ENV_PATCHED = '''    if (config.toolMode !== "codex"
        && config.toolMode !== "readonly"
        && process.env.DEVSPACE_DISABLE_SYNC_SHELL !== "true") {
        registerAdvisorAppTool(server, toolNames.shell, {'''
SYNC_SHELL_REGISTRATION_PATCHED = '''    if (config.toolMode !== "codex"
        && config.toolMode !== "readonly"
        && config.disableSyncShell !== true) {
        registerAdvisorAppTool(server, toolNames.shell, {'''
SHELL_CONTEXT_NEEDLE = '''            const response = await runShellTool(input, {
                cwd,
                root: workspace.root,
            });'''
SHELL_CONTEXT_PATCHED = '''            const response = await runShellTool(input, {
                cwd,
                root: workspace.root,
                shellMaxSeconds: config.shellMaxSeconds,
            });'''

PI_GREP_NEEDLE = '''export async function grepFilesTool(input, context) {
    if (input.path)
        resolveAllowedPath(input.path, context.cwd, [context.root]);
    const tool = createGrepTool(context.cwd);
    return runTool((params) => tool.execute("grep_files", params), input, context);
}'''
PI_GREP_PATCHED = '''export async function grepFilesTool(input, context) {
    const path = input.path
        ? resolveAllowedPath(input.path, context.cwd, [context.root])
        : undefined;
    const tool = createGrepTool(context.cwd);
    return runTool((params) => tool.execute("grep_files", params), {
        ...input,
        path,
    }, context);
}'''

PI_FIND_NEEDLE = '''export async function findFilesTool(input, context) {
    if (input.path)
        resolveAllowedPath(input.path, context.cwd, [context.root]);
    const tool = createFindTool(context.cwd);
    return runTool((params) => tool.execute("find_files", params), input, context);
}'''
PI_FIND_PATCHED = '''export async function findFilesTool(input, context) {
    const path = input.path
        ? resolveAllowedPath(input.path, context.cwd, [context.root])
        : undefined;
    const tool = createFindTool(context.cwd);
    return runTool((params) => tool.execute("find_files", params), {
        ...input,
        path,
    }, context);
}'''

PI_LIST_NEEDLE = '''export async function listDirectoryTool(input, context) {
    if (input.path)
        resolveAllowedPath(input.path, context.cwd, [context.root]);
    const tool = createLsTool(context.cwd);
    return runTool((params) => tool.execute("list_directory", params), input, context);
}'''
PI_LIST_PATCHED = '''export async function listDirectoryTool(input, context) {
    const path = input.path
        ? resolveAllowedPath(input.path, context.cwd, [context.root])
        : undefined;
    const tool = createLsTool(context.cwd);
    return runTool((params) => tool.execute("list_directory", params), {
        ...input,
        path,
    }, context);
}'''

ROOTS_FS_IMPORT_NEEDLE = 'import { homedir } from "node:os";'
ROOTS_FS_IMPORT_PATCHED = '''import { existsSync, lstatSync, realpathSync } from "node:fs";
import { homedir } from "node:os";'''
ROOTS_PATH_IMPORT_NEEDLE = 'import { isAbsolute, relative, resolve, sep } from "node:path";'
ROOTS_PATH_IMPORT_PATCHED = 'import { dirname, isAbsolute, relative, resolve, sep } from "node:path";'
ROOTS_ASSERT_NEEDLE = '''export function assertAllowedPath(path, allowedRoots) {
    const resolvedPath = resolve(expandHomePath(path));
    if (allowedRoots.some((root) => isPathInsideRoot(resolvedPath, root))) {
        return resolvedPath;
    }
    throw new AccessDeniedError(`Path is outside allowed roots: ${path}`);
}'''
ROOTS_ASSERT_PATCHED = '''function advisorSecurePathInsideRoot(path, root) {
    const resolvedPath = resolve(expandHomePath(path));
    const resolvedRoot = resolve(expandHomePath(root));
    if (!isPathInsideRoot(resolvedPath, resolvedRoot) || !existsSync(resolvedRoot))
        return false;
    let ancestor = resolvedPath;
    while (!existsSync(ancestor)) {
        const parent = dirname(ancestor);
        if (parent === ancestor)
            return false;
        ancestor = parent;
    }
    let segment = ancestor;
    while (true) {
        const metadata = lstatSync(segment);
        if (metadata.isSymbolicLink())
            return false;
        if (segment === resolvedRoot)
            break;
        const parent = dirname(segment);
        if (parent === segment || !isPathInsideRoot(parent, resolvedRoot))
            return false;
        segment = parent;
    }
    const canonicalRoot = realpathSync(resolvedRoot);
    const canonicalAncestor = realpathSync(ancestor);
    return isPathInsideRoot(canonicalAncestor, canonicalRoot);
}
export function assertAllowedPath(path, allowedRoots) {
    const resolvedPath = resolve(expandHomePath(path));
    if (allowedRoots.some((root) => advisorSecurePathInsideRoot(resolvedPath, root))) {
        return resolvedPath;
    }
    throw new AccessDeniedError(`Path is outside allowed roots: ${path}`);
}'''


def atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)


def resolve_dist(executable: str) -> Path:
    resolved = shutil.which(executable) or executable
    path = Path(resolved).expanduser().resolve()
    candidates = [path.parent] if path.name == "cli.js" else []
    candidates.extend(parent / "dist" for parent in path.parents)
    for candidate in candidates:
        if (
            (candidate / "cli.js").is_file()
            and (candidate / "config.js").is_file()
            and (candidate / "server.js").is_file()
        ):
            return candidate
    raise RuntimeError(f"Could not locate the DevSpace dist directory from {executable!r}.")


def replace_once(text: str, needle: str, replacement: str, label: str) -> tuple[str, bool]:
    if replacement in text:
        return text, False
    count = text.count(needle)
    if count != 1:
        raise RuntimeError(
            f"DevSpace secure-origin {label} patch expected one compatible insertion point, "
            f"found {count}."
        )
    return text.replace(needle, replacement, 1), True


def verify(
    config_text: str,
    server_text: str,
    pi_tools_text: str,
    process_sessions_text: str,
    roots_text: str,
) -> None:
    required = (
        (CONFIG_PATCHED, config_text),
        (CRYPTO_IMPORT_PATCHED, server_text),
        (FS_IMPORT_PATCHED, server_text),
        (HELPER_MARKER, server_text),
        (PINNED_ROOT_PATCHED, server_text),
        (OAUTH_PATCHED, server_text),
        (AUTH_ROUTER_PATCHED, server_text),
        (REQUEST_AUTH_PATCHED, server_text),
        (CLOSE_PATCHED, server_text),
        (TRANSPORT_RETENTION_PATCHED, server_text),
        (TRANSPORT_LOOKUP_PATCHED, server_text),
        (TRANSPORT_ONCLOSE_PATCHED, server_text),
        (TRANSPORT_HANDLE_PATCHED, server_text),
        (TRANSPORT_SHUTDOWN_PATCHED, server_text),
        (OPEN_PATCHED, server_text),
        (WORKSPACE_PATCHED, server_text),
        (PROCESS_HELPER_MARKER, server_text),
        (PROCESS_MAX_ACTIVE_HELPER, server_text),
        (PROCESS_START_PATCHED, server_text),
        (PROCESS_RESULT_PATCHED, server_text),
        (PROCESS_DESCRIPTION_PATCHED, server_text),
        (PROCESS_EXEC_SCHEMA_PATCHED, server_text),
        (PROCESS_HANDLER_PATCHED, server_text),
        (PROCESS_WRITE_PATCHED, server_text),
        (PROCESS_REGISTER_PATCHED, server_text),
        (PROCESS_MANAGER_FACTORY_PATCHED, server_text),
        (SYNC_SHELL_REGISTRATION_PATCHED, server_text),
        (SHELL_SCHEMA_PATCHED, server_text),
        (SHELL_CONTEXT_PATCHED, server_text),
        (PI_HELPER_MARKER, pi_tools_text),
        (PI_SHELL_PATCHED, pi_tools_text),
        (PI_GREP_PATCHED, pi_tools_text),
        (PI_FIND_PATCHED, pi_tools_text),
        (PI_LIST_PATCHED, pi_tools_text),
        (PROCESS_BUFFER_PATCHED, process_sessions_text),
        (PROCESS_FIELDS_PATCHED, process_sessions_text),
        (PROCESS_LIMIT_FIELDS_PATCHED, process_sessions_text),
        (PROCESS_LIMIT_CONSTRUCTOR_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_START_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_WRITE_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_SHUTDOWN_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_CREATE_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_FINISH_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_CONSUME_PATCHED, process_sessions_text),
        (PROCESS_MANAGER_OWNERSHIP_PATCHED, process_sessions_text),
        (ROOTS_FS_IMPORT_PATCHED, roots_text),
        (ROOTS_PATH_IMPORT_PATCHED, roots_text),
        (ROOTS_ASSERT_PATCHED, roots_text),
    )
    missing = [marker.splitlines()[0] for marker, text in required if marker not in text]
    if missing:
        raise RuntimeError("DevSpace secure-origin patch is incomplete: " + ", ".join(missing))
    forbidden = (
        ("unforwarded grep path", PI_GREP_NEEDLE, pi_tools_text),
        ("unforwarded find path", PI_FIND_NEEDLE, pi_tools_text),
        ("unforwarded list path", PI_LIST_NEEDLE, pi_tools_text),
        ("fixed five-minute shell timeout", PI_SHELL_LEGACY_PATCHED, pi_tools_text),
        ("window timeout without compatibility fallback", PI_SHELL_WINDOW_PATCHED, pi_tools_text),
        ("compatibility timeout with changed default", PI_SHELL_COMPAT_PATCHED, pi_tools_text),
        ("long synchronous shell timeout", PI_SHELL_LONG_SYNC_PATCHED, pi_tools_text),
        ("fixed five-minute shell schema", SHELL_SCHEMA_NEEDLE, server_text),
        ("window schema without compatibility fallback", SHELL_SCHEMA_WINDOW_PATCHED, server_text),
        ("long synchronous shell schema", SHELL_SCHEMA_LONG_SYNC_PATCHED, server_text),
        ("unsandboxed process command", PROCESS_START_NEEDLE, server_text),
        ("process tools restricted to Codex mode", PROCESS_REGISTER_NEEDLE, server_text),
        ("environment-backed active process limit", PROCESS_MAX_ACTIVE_HELPER_LEGACY, server_text),
        ("config-only active process limit", PROCESS_MAX_ACTIVE_HELPER_CONFIG_ONLY, server_text),
        ("unbounded process manager", PROCESS_MANAGER_FACTORY_NEEDLE, server_text),
        ("process output without replay identity", PROCESS_RESULT_NEEDLE, server_text),
        ("process manager without replay tracking", PROCESS_MANAGER_START_NEEDLE, process_sessions_text),
        ("replay manager without active limit", PROCESS_MANAGER_START_REPLAY_PATCHED, process_sessions_text),
        ("process session removed before replay recovery", PROCESS_MANAGER_WRITE_NEEDLE, process_sessions_text),
        ("duplicate pinned-workspace guard", WORKSPACE_DUPLICATE_PATCHED, server_text),
        ("lexical-only path guard", ROOTS_ASSERT_NEEDLE, roots_text),
        ("transport close without idle timer cleanup", TRANSPORT_ONCLOSE_NEEDLE, server_text),
        ("transport retention without active request tracking", TRANSPORT_RETENTION_PATCHED_LEGACY, server_text),
        ("transport lookup without active request tracking", TRANSPORT_LOOKUP_PATCHED_LEGACY, server_text),
        ("transport close without identity guard", TRANSPORT_ONCLOSE_PATCHED_LEGACY, server_text),
        ("transport finalizer without active request tracking", TRANSPORT_HANDLE_PATCHED_LEGACY, server_text),
        ("transport shutdown without active request cleanup", TRANSPORT_SHUTDOWN_PATCHED_LEGACY, server_text),
    )
    present = [label for label, marker, text in forbidden if marker in text]
    if present:
        raise RuntimeError(
            "DevSpace secure-origin patch retained unsafe upstream code: "
            + ", ".join(present)
        )


def patch_devspace(dist: Path, *, check_only: bool) -> bool:
    config_path = dist / "config.js"
    server_path = dist / "server.js"
    pi_tools_path = dist / "pi-tools.js"
    process_sessions_path = dist / "process-sessions.js"
    roots_path = dist / "roots.js"
    config_text = config_path.read_text(encoding="utf-8")
    server_text = server_path.read_text(encoding="utf-8")
    pi_tools_text = pi_tools_path.read_text(encoding="utf-8")
    process_sessions_text = process_sessions_path.read_text(encoding="utf-8")
    roots_text = roots_path.read_text(encoding="utf-8")

    if check_only:
        verify(
            config_text,
            server_text,
            pi_tools_text,
            process_sessions_text,
            roots_text,
        )
        return False

    changed = False
    if CRYPTO_IMPORT_PATCHED not in server_text:
        crypto_candidates = (
            CRYPTO_IMPORT_LEGACY_PATCHED,
            CRYPTO_IMPORT_NEEDLE,
        )
        matches = [
            candidate
            for candidate in crypto_candidates
            if server_text.count(candidate) == 1
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "DevSpace secure-origin crypto import patch expected exactly one "
                f"compatible insertion point, found {len(matches)}."
            )
        server_text = server_text.replace(matches[0], CRYPTO_IMPORT_PATCHED, 1)
        changed = True
    for label, replacement, candidates in (
        (
            "MCP transport retention",
            TRANSPORT_RETENTION_PATCHED,
            (TRANSPORT_RETENTION_PATCHED_LEGACY, TRANSPORT_MAP_NEEDLE),
        ),
        (
            "MCP transport lookup",
            TRANSPORT_LOOKUP_PATCHED,
            (TRANSPORT_LOOKUP_PATCHED_LEGACY, TRANSPORT_LOOKUP_NEEDLE),
        ),
        (
            "MCP transport close",
            TRANSPORT_ONCLOSE_PATCHED,
            (TRANSPORT_ONCLOSE_PATCHED_LEGACY, TRANSPORT_ONCLOSE_NEEDLE),
        ),
        (
            "MCP transport request lifecycle",
            TRANSPORT_HANDLE_PATCHED,
            (TRANSPORT_HANDLE_PATCHED_LEGACY, TRANSPORT_HANDLE_NEEDLE),
        ),
        (
            "MCP transport shutdown",
            TRANSPORT_SHUTDOWN_PATCHED,
            (TRANSPORT_SHUTDOWN_PATCHED_LEGACY, TRANSPORT_SHUTDOWN_NEEDLE),
        ),
    ):
        if replacement in server_text:
            continue
        selected = None
        for candidate in candidates:
            count = server_text.count(candidate)
            if count > 1:
                raise RuntimeError(
                    f"DevSpace secure-origin {label} patch found {count} "
                    "compatible insertion points."
                )
            if count == 1:
                selected = candidate
                break
        if selected is None:
            raise RuntimeError(
                f"DevSpace secure-origin {label} patch expected exactly one "
                "compatible insertion point, found 0."
            )
        server_text = server_text.replace(selected, replacement, 1)
        changed = True
    for path_label, needle, replacement in (
        ("config", CONFIG_NEEDLE, CONFIG_PATCHED),
        ("filesystem import", FS_IMPORT_NEEDLE, FS_IMPORT_PATCHED),
        ("OAuth provider", OAUTH_NEEDLE, OAUTH_PATCHED),
        ("OAuth router", AUTH_ROUTER_NEEDLE, AUTH_ROUTER_PATCHED),
        ("request authentication", REQUEST_AUTH_NEEDLE, REQUEST_AUTH_PATCHED),
        ("OAuth shutdown", CLOSE_NEEDLE, CLOSE_PATCHED),
        ("pinned workspace open", OPEN_NEEDLE, OPEN_PATCHED),
    ):
        target = config_text if path_label == "config" else server_text
        target, did_change = replace_once(target, needle, replacement, path_label)
        if path_label == "config":
            config_text = target
        else:
            server_text = target
        changed |= did_change

    if HELPER_MARKER not in server_text:
        server_text, did_change = replace_once(
            server_text,
            HELPER_INSERT_NEEDLE,
            HELPERS + HELPER_INSERT_NEEDLE,
            "helper functions",
        )
        changed |= did_change

    if PROCESS_IDENTITY_MARKER not in server_text:
        if PROCESS_HELPERS_LEGACY in server_text:
            server_text = server_text.replace(
                PROCESS_HELPERS_LEGACY,
                PROCESS_HELPERS,
                1,
            )
            changed = True
        else:
            server_text, did_change = replace_once(
                server_text,
                PROCESS_HELPER_INSERT_NEEDLE,
                PROCESS_HELPERS + PROCESS_HELPER_INSERT_NEEDLE,
                "process sandbox helper",
            )
            changed |= did_change
    if PROCESS_MAX_ACTIVE_HELPER not in server_text:
        legacy_helpers = (
            PROCESS_MAX_ACTIVE_HELPER_CONFIG_ONLY,
            PROCESS_MAX_ACTIVE_HELPER_LEGACY,
        )
        matches = [helper for helper in legacy_helpers if server_text.count(helper) == 1]
        if len(matches) == 1:
            server_text = server_text.replace(matches[0], PROCESS_MAX_ACTIVE_HELPER, 1)
            changed = True
        elif matches:
            raise RuntimeError(
                "DevSpace secure-origin active process limit patch found multiple "
                "legacy helpers."
            )
        else:
            server_text, did_change = replace_once(
                server_text,
                PROCESS_HELPER_INSERT_NEEDLE,
                PROCESS_MAX_ACTIVE_HELPER + PROCESS_HELPER_INSERT_NEEDLE,
                "active process limit helper",
            )
            changed |= did_change
    if SYNC_SHELL_REGISTRATION_ENV_PATCHED in server_text:
        server_text = server_text.replace(
            SYNC_SHELL_REGISTRATION_ENV_PATCHED,
            SYNC_SHELL_REGISTRATION_PATCHED,
            1,
        )
        changed = True
    if PROCESS_START_PATCHED not in server_text:
        process_start_candidates = (
            PROCESS_START_LEGACY_PATCHED,
            PROCESS_START_NEEDLE,
        )
        matches = [
            candidate
            for candidate in process_start_candidates
            if server_text.count(candidate) == 1
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "DevSpace secure-origin process command patch expected exactly one "
                f"compatible insertion point, found {len(matches)}."
            )
        server_text = server_text.replace(matches[0], PROCESS_START_PATCHED, 1)
        changed = True

    for needle, replacement, label in (
        (PROCESS_RESULT_NEEDLE, PROCESS_RESULT_PATCHED, "process replay response"),
        (
            PROCESS_DESCRIPTION_NEEDLE,
            PROCESS_DESCRIPTION_PATCHED,
            "process tool description",
        ),
        (
            PROCESS_EXEC_SCHEMA_NEEDLE,
            PROCESS_EXEC_SCHEMA_PATCHED,
            "process replay input schema",
        ),
        (PROCESS_HANDLER_NEEDLE, PROCESS_HANDLER_PATCHED, "process handler inputs"),
        (
            PROCESS_REGISTER_NEEDLE,
            PROCESS_REGISTER_PATCHED,
            "full-mode process registration",
        ),
        (
            PROCESS_MANAGER_FACTORY_NEEDLE,
            PROCESS_MANAGER_FACTORY_PATCHED,
            "bounded process manager",
        ),
        (
            SYNC_SHELL_REGISTRATION_NEEDLE,
            SYNC_SHELL_REGISTRATION_PATCHED,
            "optional synchronous shell registration",
        ),
    ):
        server_text, did_change = replace_once(
            server_text,
            needle,
            replacement,
            label,
        )
        changed |= did_change

    server_text, did_change = replace_once(
        server_text,
        PINNED_ROOT_NEEDLE,
        PINNED_ROOT_PATCHED,
        "pinned-root metadata",
    )
    changed |= did_change

    while WORKSPACE_DUPLICATE_PATCHED in server_text:
        server_text = server_text.replace(
            WORKSPACE_DUPLICATE_PATCHED,
            WORKSPACE_PATCHED,
        )
        changed = True
    workspace_guard_placeholder = f"__ADVISOR_WORKSPACE_GUARD_{uuid.uuid4().hex}__"
    guarded_count = server_text.count(WORKSPACE_PATCHED)
    server_text = server_text.replace(
        WORKSPACE_PATCHED,
        workspace_guard_placeholder,
    )
    count = server_text.count(WORKSPACE_NEEDLE)
    if count:
        server_text = server_text.replace(WORKSPACE_NEEDLE, WORKSPACE_PATCHED)
        changed = True
    server_text = server_text.replace(
        workspace_guard_placeholder,
        WORKSPACE_PATCHED,
    )
    if server_text.count(WORKSPACE_PATCHED) < guarded_count:
        raise RuntimeError("DevSpace secure-origin patch lost a guarded workspace lookup.")
    if WORKSPACE_PATCHED not in server_text:
        raise RuntimeError("DevSpace secure-origin patch found no guarded workspace lookups.")
    server_text, did_change = replace_once(
        server_text,
        PROCESS_WRITE_NEEDLE,
        PROCESS_WRITE_PATCHED,
        "process workspace ownership",
    )
    changed |= did_change

    if SHELL_SCHEMA_PATCHED not in server_text:
        schema_candidates = (
            SHELL_SCHEMA_LONG_SYNC_PATCHED,
            SHELL_SCHEMA_WINDOW_PATCHED,
            SHELL_SCHEMA_NEEDLE,
        )
        matches = [
            candidate
            for candidate in schema_candidates
            if server_text.count(candidate) == 1
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "DevSpace secure-origin shell timeout schema patch expected exactly one "
                f"compatible insertion point, found {len(matches)}."
            )
        server_text = server_text.replace(matches[0], SHELL_SCHEMA_PATCHED, 1)
        changed = True
    server_text, did_change = replace_once(
        server_text,
        SHELL_CONTEXT_NEEDLE,
        SHELL_CONTEXT_PATCHED,
        "shell timeout context",
    )
    changed |= did_change

    if PI_HELPER_MARKER not in pi_tools_text:
        pi_tools_text, did_change = replace_once(
            pi_tools_text,
            PI_HELPER_INSERT_NEEDLE,
            PI_HELPERS,
            "shell helper",
        )
        changed |= did_change
    if PI_SHELL_PATCHED not in pi_tools_text:
        shell_candidates = (
            PI_SHELL_LONG_SYNC_PATCHED,
            PI_SHELL_COMPAT_PATCHED,
            PI_SHELL_WINDOW_PATCHED,
            PI_SHELL_LEGACY_PATCHED,
            PI_SHELL_NEEDLE,
        )
        matches = [
            candidate
            for candidate in shell_candidates
            if pi_tools_text.count(candidate) == 1
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "DevSpace secure-origin shell sandbox patch expected exactly one "
                f"compatible insertion point, found {len(matches)}."
            )
        pi_tools_text = pi_tools_text.replace(matches[0], PI_SHELL_PATCHED, 1)
        changed = True

    if PROCESS_MANAGER_START_PATCHED not in process_sessions_text:
        process_start_candidates = (
            PROCESS_MANAGER_START_REPLAY_PATCHED,
            PROCESS_MANAGER_START_NEEDLE,
        )
        matches = [
            candidate
            for candidate in process_start_candidates
            if process_sessions_text.count(candidate) == 1
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "DevSpace secure-origin process replay admission patch expected "
                f"exactly one compatible insertion point, found {len(matches)}."
            )
        process_sessions_text = process_sessions_text.replace(
            matches[0],
            PROCESS_MANAGER_START_PATCHED,
            1,
        )
        changed = True

    for needle, replacement, label in (
        (PROCESS_BUFFER_NEEDLE, PROCESS_BUFFER_PATCHED, "process output snapshot"),
        (PROCESS_FIELDS_NEEDLE, PROCESS_FIELDS_PATCHED, "process replay registry"),
        (
            PROCESS_LIMIT_FIELDS_NEEDLE,
            PROCESS_LIMIT_FIELDS_PATCHED,
            "active process limit field",
        ),
        (
            PROCESS_LIMIT_CONSTRUCTOR_NEEDLE,
            PROCESS_LIMIT_CONSTRUCTOR_PATCHED,
            "active process limit constructor",
        ),
        (
            PROCESS_MANAGER_WRITE_NEEDLE,
            PROCESS_MANAGER_WRITE_PATCHED,
            "process durable polling",
        ),
        (
            PROCESS_MANAGER_SHUTDOWN_NEEDLE,
            PROCESS_MANAGER_SHUTDOWN_PATCHED,
            "process replay shutdown",
        ),
        (
            PROCESS_MANAGER_CREATE_NEEDLE,
            PROCESS_MANAGER_CREATE_PATCHED,
            "process replay metadata",
        ),
        (
            PROCESS_MANAGER_FINISH_NEEDLE,
            PROCESS_MANAGER_FINISH_PATCHED,
            "process completion retention",
        ),
        (
            PROCESS_MANAGER_CONSUME_NEEDLE,
            PROCESS_MANAGER_CONSUME_PATCHED,
            "process replay snapshots",
        ),
        (
            PROCESS_MANAGER_OWNERSHIP_NEEDLE,
            PROCESS_MANAGER_OWNERSHIP_PATCHED,
            "process root ownership",
        ),
    ):
        process_sessions_text, did_change = replace_once(
            process_sessions_text,
            needle,
            replacement,
            label,
        )
        changed |= did_change
    for needle, replacement, label in (
        (PI_GREP_NEEDLE, PI_GREP_PATCHED, "grep path forwarding"),
        (PI_FIND_NEEDLE, PI_FIND_PATCHED, "find path forwarding"),
        (PI_LIST_NEEDLE, PI_LIST_PATCHED, "list path forwarding"),
    ):
        pi_tools_text, did_change = replace_once(
            pi_tools_text,
            needle,
            replacement,
            label,
        )
        changed |= did_change
    for needle, replacement, label in (
        (ROOTS_FS_IMPORT_NEEDLE, ROOTS_FS_IMPORT_PATCHED, "roots filesystem import"),
        (ROOTS_PATH_IMPORT_NEEDLE, ROOTS_PATH_IMPORT_PATCHED, "roots path import"),
        (ROOTS_ASSERT_NEEDLE, ROOTS_ASSERT_PATCHED, "symlink-safe path guard"),
    ):
        roots_text, did_change = replace_once(
            roots_text,
            needle,
            replacement,
            label,
        )
        changed |= did_change

    verify(
        config_text,
        server_text,
        pi_tools_text,
        process_sessions_text,
        roots_text,
    )
    if changed:
        atomic_write(config_path, config_text)
        atomic_write(server_path, server_text)
        atomic_write(pi_tools_path, pi_tools_text)
        atomic_write(process_sessions_path, process_sessions_text)
        atomic_write(roots_path, roots_text)
    verify(
        config_path.read_text(encoding="utf-8"),
        server_path.read_text(encoding="utf-8"),
        pi_tools_path.read_text(encoding="utf-8"),
        process_sessions_path.read_text(encoding="utf-8"),
        roots_path.read_text(encoding="utf-8"),
    )
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="devspace")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dist = resolve_dist(args.executable)
        changed = patch_devspace(dist, check_only=args.check)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.check:
        print("DevSpace secure origin verified.")
    elif changed:
        print("DevSpace secure origin applied and verified.")
    else:
        print("DevSpace secure origin already applied and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
