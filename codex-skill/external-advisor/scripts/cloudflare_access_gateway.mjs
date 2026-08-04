#!/usr/bin/env node

import { createPublicKey, timingSafeEqual, verify as verifySignature } from "node:crypto";
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, unlinkSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";

const GATEWAY_HEADER = "x-advisor-gateway-secret";
const HEALTH_HEADER = "x-advisor-health-secret";
const ACCESS_HEADER = "cf-access-jwt-assertion";
const MAX_TOKEN_BYTES = 16 * 1024;
const MAX_JWKS_BYTES = 256 * 1024;
const DEFAULT_BODY_LIMIT = 16 * 1024 * 1024;
const DEFAULT_CONCURRENCY = 8;
const DEFAULT_CLOCK_SKEW_SECONDS = 60;
const REQUEST_BODY_TIMEOUT_MS = 60 * 1000;
const JWKS_CACHE_MS = 5 * 60 * 1000;
const MIN_JWKS_REFRESH_INTERVAL_MS = 30 * 1000;
const NEGATIVE_KID_CACHE_MS = 60 * 1000;
const MAX_NEGATIVE_KIDS = 256;
const LOG_EVENT = "advisor_domain_mcp_gateway_request";
const HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function fail(message) {
  throw new Error(message);
}

function defaultGatewayLog(event, fields) {
  console.log(
    JSON.stringify({
      ts: new Date().toISOString(),
      level: "info",
      event,
      ...fields,
    }),
  );
}

function requestRoute(pathname) {
  if (pathname === "/mcp") return "mcp";
  if (pathname.startsWith("/mcp-app-assets/")) return "asset";
  if (pathname === "/__advisor_mcp_health") return "health";
  return "other";
}

function requestMethod(method) {
  const normalized = String(method ?? "").toUpperCase();
  return new Set(["GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"]).has(
    normalized,
  )
    ? normalized
    : "OTHER";
}

function accessAssertionRejection(error) {
  const message = error instanceof Error ? error.message : "";
  const categories = new Map([
    ["Invalid Cloudflare Access token.", "access_assertion_malformed"],
    ["Invalid JWT header.", "access_assertion_malformed"],
    ["Invalid JWT payload.", "access_assertion_malformed"],
    ["Invalid JWT signature.", "access_assertion_malformed"],
    ["Unsupported Cloudflare Access token.", "access_assertion_unsupported"],
    ["Cloudflare Access signing key was not found.", "access_assertion_signing_key"],
    ["Cloudflare Access signing key is invalid.", "access_assertion_signing_key"],
    ["Cloudflare Access token signature is invalid.", "access_assertion_signature"],
    ["Cloudflare Access issuer mismatch.", "access_assertion_issuer"],
    ["Cloudflare Access audience mismatch.", "access_assertion_audience"],
    ["Cloudflare Access token expired.", "access_assertion_expired"],
    ["Cloudflare Access token is not active.", "access_assertion_not_active"],
    ["Cloudflare Access token issue time is invalid.", "access_assertion_issue_time"],
    ["Cloudflare Access token temporal claims are invalid.", "access_assertion_temporal"],
    ["Cloudflare Access identity is not allowed.", "access_assertion_identity"],
    ["Cloudflare Access subject is missing or malformed.", "access_assertion_subject"],
    ["Cloudflare Access JWKS payload is invalid.", "access_assertion_jwks"],
    ["JWKS request timed out.", "access_assertion_jwks"],
    ["JWKS response exceeded its size limit.", "access_assertion_jwks"],
    ["JWKS response was not valid JSON.", "access_assertion_jwks"],
  ]);
  if (categories.has(message)) return categories.get(message);
  if (message.startsWith("JWKS request returned HTTP ")) return "access_assertion_jwks";
  return "access_assertion_rejected";
}

function readPrivateSecret(path) {
  const metadata = lstatSync(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    fail("Gateway secret must be a regular file.");
  }
  if ((metadata.mode & 0o077) !== 0) {
    fail("Gateway secret must not be group- or world-accessible.");
  }
  if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
    fail("Gateway secret must be owned by the gateway service user.");
  }
  const value = readFileSync(path, "utf8").trim();
  if (!/^[A-Za-z0-9_-]{43,128}$/.test(value)) {
    fail("Gateway secret must be a 32-byte-or-longer base64url value.");
  }
  return value;
}

function normalizeIssuer(value) {
  const parsed = new URL(value);
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    parsed.search ||
    parsed.hash ||
    parsed.pathname.replaceAll("/", "")
  ) {
    fail("Cloudflare Access issuer must be an HTTPS team-domain origin.");
  }
  const hostname = parsed.hostname.toLowerCase();
  if (!hostname.endsWith(".cloudflareaccess.com")) {
    fail("Cloudflare Access issuer must use a cloudflareaccess.com team domain.");
  }
  return `https://${hostname}`;
}

function positiveInteger(value, fallback, name, maximum = Number.MAX_SAFE_INTEGER) {
  if (value === undefined) return fallback;
  if (!Number.isInteger(value) || value < 1 || value > maximum) {
    fail(`${name} must be an integer between 1 and ${maximum}.`);
  }
  return value;
}

export function validateGatewayConfig(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) fail("Gateway config must be an object.");
  const issuer = normalizeIssuer(String(raw.accessIssuer ?? ""));
  const audience = String(raw.accessAudience ?? "").trim();
  if (!/^[A-Za-z0-9_-]{8,256}$/.test(audience)) {
    fail("Cloudflare Access audience is missing or malformed.");
  }
  const publicHostname = String(raw.publicHostname ?? "").trim().toLowerCase();
  if (
    !/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(
      publicHostname,
    )
  ) {
    fail("Public hostname is missing or malformed.");
  }
  const emails = Array.isArray(raw.allowedEmails)
    ? raw.allowedEmails.map((value) => String(value).trim().toLowerCase()).filter(Boolean)
    : [];
  if (
    emails.length !== 1 ||
    emails.some((email) => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email))
  ) {
    fail("Exactly one valid allowed email is required.");
  }
  const originSocket = String(raw.originSocket ?? "").trim();
  if (!originSocket.startsWith("/") || originSocket.includes("\0")) {
    fail("Origin socket must be an absolute Unix-socket path.");
  }
  const upstreamSecretFile = String(raw.upstreamSecretFile ?? "").trim();
  if (!upstreamSecretFile.startsWith("/") || upstreamSecretFile.includes("\0")) {
    fail("Upstream secret file must be an absolute path.");
  }
  const gatewaySocket = String(raw.gatewaySocket ?? "").trim();
  if (!gatewaySocket.startsWith("/") || gatewaySocket.includes("\0")) {
    fail("Gateway socket must be an absolute Unix-socket path.");
  }
  return {
    accessIssuer: issuer,
    accessAudience: audience,
    allowedEmails: [...new Set(emails)],
    publicHostname,
    originSocket,
    upstreamSecretFile,
    gatewaySocket,
    maxBodyBytes: positiveInteger(raw.maxBodyBytes, DEFAULT_BODY_LIMIT, "maxBodyBytes"),
    maxConcurrent: positiveInteger(raw.maxConcurrent, DEFAULT_CONCURRENCY, "maxConcurrent", 64),
    clockSkewSeconds: positiveInteger(
      raw.clockSkewSeconds,
      DEFAULT_CLOCK_SKEW_SECONDS,
      "clockSkewSeconds",
      300,
    ),
  };
}

function decodeBase64Url(value, label) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    fail(`Invalid JWT ${label}.`);
  }
  return Buffer.from(value, "base64url");
}

function decodeJsonPart(value, label) {
  let parsed;
  try {
    parsed = JSON.parse(decodeBase64Url(value, label).toString("utf8"));
  } catch {
    fail(`Invalid JWT ${label}.`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    fail(`Invalid JWT ${label}.`);
  }
  return parsed;
}

function normalizedAudience(value) {
  if (typeof value === "string") return [value];
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) return value;
  return [];
}

function exactString(value, expected) {
  if (typeof value !== "string") return false;
  const actualBytes = Buffer.from(value, "utf8");
  const expectedBytes = Buffer.from(expected, "utf8");
  return (
    actualBytes.length === expectedBytes.length && timingSafeEqual(actualBytes, expectedBytes)
  );
}

export function verifyAccessJwt(token, config, jwk, nowSeconds = Math.floor(Date.now() / 1000)) {
  if (typeof token !== "string" || token.length < 32 || token.length > MAX_TOKEN_BYTES) {
    fail("Invalid Cloudflare Access token.");
  }
  const parts = token.split(".");
  if (parts.length !== 3) fail("Invalid Cloudflare Access token.");
  const [encodedHeader, encodedPayload, encodedSignature] = parts;
  const header = decodeJsonPart(encodedHeader, "header");
  const payload = decodeJsonPart(encodedPayload, "payload");
  if (header.alg !== "RS256" || typeof header.kid !== "string" || header.kid.length > 256) {
    fail("Unsupported Cloudflare Access token.");
  }
  if (!jwk || jwk.kid !== header.kid || jwk.kty !== "RSA") {
    fail("Cloudflare Access signing key was not found.");
  }
  let key;
  try {
    key = createPublicKey({ key: jwk, format: "jwk" });
  } catch {
    fail("Cloudflare Access signing key is invalid.");
  }
  const signature = decodeBase64Url(encodedSignature, "signature");
  const signed = Buffer.from(`${encodedHeader}.${encodedPayload}`, "ascii");
  if (!verifySignature("RSA-SHA256", signed, key, signature)) {
    fail("Cloudflare Access token signature is invalid.");
  }
  const skew = config.clockSkewSeconds;
  if (!exactString(payload.iss, config.accessIssuer)) fail("Cloudflare Access issuer mismatch.");
  if (!normalizedAudience(payload.aud).some((item) => exactString(item, config.accessAudience))) {
    fail("Cloudflare Access audience mismatch.");
  }
  if (!Number.isFinite(payload.exp) || payload.exp < nowSeconds - skew) {
    fail("Cloudflare Access token expired.");
  }
  if (payload.nbf !== undefined && (!Number.isFinite(payload.nbf) || payload.nbf > nowSeconds + skew)) {
    fail("Cloudflare Access token is not active.");
  }
  if (!Number.isFinite(payload.iat) || payload.iat > nowSeconds + skew) {
    fail("Cloudflare Access token issue time is invalid.");
  }
  if (
    payload.exp <= payload.iat ||
    (payload.nbf !== undefined && payload.nbf > payload.exp)
  ) {
    fail("Cloudflare Access token temporal claims are invalid.");
  }
  const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
  if (!config.allowedEmails.some((allowed) => exactString(email, allowed))) {
    fail("Cloudflare Access identity is not allowed.");
  }
  const subject = typeof payload.sub === "string" ? payload.sub.trim() : "";
  if (!subject || subject.length > 512) {
    fail("Cloudflare Access subject is missing or malformed.");
  }
  return { email, subject };
}

function requestJson(url, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, { timeout: timeoutMs, headers: { accept: "application/json" } });
    request.once("timeout", () => request.destroy(new Error("JWKS request timed out.")));
    request.once("error", reject);
    request.once("response", (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`JWKS request returned HTTP ${response.statusCode}.`));
        return;
      }
      let bytes = 0;
      const chunks = [];
      response.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes > MAX_JWKS_BYTES) {
          request.destroy(new Error("JWKS response exceeded its size limit."));
          return;
        }
        chunks.push(chunk);
      });
      response.once("end", () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
        } catch {
          reject(new Error("JWKS response was not valid JSON."));
        }
      });
    });
  });
}

export function createAccessTokenVerifier(config, options = {}) {
  const fetchJwks =
    options.fetchJwks ??
    (() => requestJson(new URL("/cdn-cgi/access/certs", config.accessIssuer), 5000));
  let cache = { keys: [], fetchedAt: 0 };
  let pending;
  let lastRefreshAttempt = 0;
  const negativeKids = new Map();

  async function refresh(force = false) {
    const now = Date.now();
    if (force && cache.fetchedAt > 0 && now - lastRefreshAttempt < MIN_JWKS_REFRESH_INTERVAL_MS) {
      return cache;
    }
    if (!pending) {
      lastRefreshAttempt = now;
      pending = Promise.resolve(fetchJwks())
        .then((payload) => {
          if (
            !payload ||
            !Array.isArray(payload.keys) ||
            payload.keys.length < 1 ||
            payload.keys.some((key) => !key || typeof key !== "object")
          ) {
            fail("Cloudflare Access JWKS payload is invalid.");
          }
          cache = { keys: payload.keys, fetchedAt: Date.now() };
          return cache;
        })
        .finally(() => {
          pending = undefined;
        });
    }
    return pending;
  }

  return async (token) => {
    if (typeof token !== "string" || token.length < 32 || token.length > MAX_TOKEN_BYTES) {
      fail("Invalid Cloudflare Access token.");
    }
    const [encodedHeader] = String(token).split(".");
    const header = decodeJsonPart(encodedHeader, "header");
    if (header.alg !== "RS256" || typeof header.kid !== "string" || header.kid.length > 256) {
      fail("Unsupported Cloudflare Access token.");
    }
    const now = Date.now();
    const negativeUntil = negativeKids.get(header.kid) ?? 0;
    if (negativeUntil > now) {
      fail("Cloudflare Access signing key was not found.");
    }
    negativeKids.delete(header.kid);
    const fresh = Date.now() - cache.fetchedAt < JWKS_CACHE_MS;
    if (!fresh) {
      await refresh();
    }
    let key = cache.keys.find((candidate) => candidate.kid === header.kid);
    if (!key) {
      await refresh(true);
      key = cache.keys.find((candidate) => candidate.kid === header.kid);
    }
    if (!key) {
      if (negativeKids.size >= MAX_NEGATIVE_KIDS) {
        negativeKids.delete(negativeKids.keys().next().value);
      }
      negativeKids.set(header.kid, Date.now() + NEGATIVE_KID_CACHE_MS);
      fail("Cloudflare Access signing key was not found.");
    }
    return verifyAccessJwt(token, config, key);
  };
}

function allowedPath(pathname) {
  if (pathname === "/mcp") return true;
  if (!pathname.startsWith("/mcp-app-assets/") || pathname.length > 2048) return false;
  const relative = pathname.slice("/mcp-app-assets/".length);
  return Boolean(
    relative &&
      !relative.includes("%") &&
      !relative.includes("\\") &&
      relative
        .split("/")
        .every(
          (segment) =>
            segment &&
            segment !== "." &&
            segment !== ".." &&
            /^[A-Za-z0-9._-]+$/.test(segment),
        ),
  );
}

function allowedMethod(pathname, method) {
  if (pathname === "/mcp") return new Set(["GET", "POST", "DELETE"]).has(method);
  return new Set(["GET", "HEAD", "OPTIONS"]).has(method);
}

function filteredRequestHeaders(headers, secret) {
  const output = {};
  for (const [rawName, value] of Object.entries(headers)) {
    const name = rawName.toLowerCase();
    if (
      value === undefined ||
      HOP_HEADERS.has(name) ||
      name === "host" ||
      name === "authorization" ||
      name === "cookie" ||
      name === GATEWAY_HEADER ||
      name.startsWith("cf-") ||
      name.startsWith("x-forwarded-")
    ) {
      continue;
    }
    output[name] = value;
  }
  output.host = "localhost";
  output[GATEWAY_HEADER] = secret;
  return output;
}

function filteredResponseHeaders(headers) {
  const output = {};
  for (const [rawName, value] of Object.entries(headers)) {
    const name = rawName.toLowerCase();
    if (
      value === undefined ||
      HOP_HEADERS.has(name) ||
      name === "set-cookie" ||
      name === "www-authenticate" ||
      name === "location"
    ) {
      continue;
    }
    output[name] = value;
  }
  output["cache-control"] = output["cache-control"] ?? "no-store";
  return output;
}

function sendJson(response, status, body) {
  const payload = Buffer.from(JSON.stringify(body));
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": payload.length,
    "cache-control": "no-store",
  });
  response.end(payload);
}

function exactSecret(value, expected) {
  if (typeof value !== "string") return false;
  const actual = Buffer.from(value, "utf8");
  const wanted = Buffer.from(expected, "utf8");
  return actual.length === wanted.length && timingSafeEqual(actual, wanted);
}

function localHealthRequest(request, secret) {
  const host = String(request.headers.host ?? "").toLowerCase();
  return (
    request.method === "GET" &&
    request.url === "/__advisor_mcp_health" &&
    host === "localhost" &&
    request.socket.remoteAddress === undefined &&
    exactSecret(request.headers[HEALTH_HEADER], secret)
  );
}

function prepareUnixSocket(path) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (!existsSync(path)) return;
  const metadata = lstatSync(path);
  if (
    !metadata.isSocket() ||
    (typeof process.getuid === "function" && metadata.uid !== process.getuid())
  ) {
    fail("Refusing to replace a non-socket or foreign gateway path.");
  }
  unlinkSync(path);
}

export function createGatewayServer(config, options = {}) {
  const verifyToken = options.verifyToken ?? createAccessTokenVerifier(config);
  const upstreamSecret = options.upstreamSecret ?? readPrivateSecret(config.upstreamSecretFile);
  const logEvent = options.logEvent ?? defaultGatewayLog;
  let active = 0;
  let activeEventStreams = 0;
  let authenticating = 0;
  let requestOrdinal = 0;

  const server = http.createServer(async (request, response) => {
    const startedAt = performance.now();
    const ordinal = (requestOrdinal += 1);
    let logged = false;
    const record = (status, outcome, route = "other") => {
      if (logged) return;
      logged = true;
      logEvent(LOG_EVENT, {
        ordinal,
        method: requestMethod(request.method),
        route,
        sessionHeaderPresent: typeof request.headers["mcp-session-id"] === "string",
        status,
        outcome,
        durationMs: Math.round(performance.now() - startedAt),
      });
    };
    if (localHealthRequest(request, upstreamSecret)) {
      sendJson(response, 200, { ok: true, service: "advisor-domain-mcp-gateway" });
      record(200, "local_health", "health");
      return;
    }
    let parsed;
    try {
      parsed = new URL(request.url ?? "/", `https://${config.publicHostname}`);
    } catch {
      sendJson(response, 400, { error: "bad_request" });
      record(400, "bad_request");
      return;
    }
    const route = requestRoute(parsed.pathname);
    const requestHost = String(request.headers.host ?? "").toLowerCase();
    const expectedHost =
      requestHost === config.publicHostname || requestHost === `${config.publicHostname}:443`;
    const expectedOrigin =
      parsed.protocol === "https:" &&
      parsed.hostname === config.publicHostname &&
      (parsed.port === "" || parsed.port === "443") &&
      parsed.username === "" &&
      parsed.password === "";
    if (
      !expectedHost ||
      !expectedOrigin ||
      !allowedPath(parsed.pathname)
    ) {
      sendJson(response, 404, { error: "not_found" });
      record(404, "route_rejected", route);
      return;
    }
    if (!allowedMethod(parsed.pathname, request.method ?? "")) {
      response.setHeader(
        "allow",
        parsed.pathname === "/mcp" ? "GET, POST, DELETE" : "GET, HEAD, OPTIONS",
      );
      sendJson(response, 405, { error: "method_not_allowed" });
      record(405, "method_rejected", route);
      return;
    }
    const declaredLength = Number(request.headers["content-length"] ?? 0);
    if (!Number.isFinite(declaredLength) || declaredLength < 0 || declaredLength > config.maxBodyBytes) {
      sendJson(response, 413, { error: "request_too_large" });
      record(413, "declared_body_too_large", route);
      return;
    }
    const token = request.headers[ACCESS_HEADER];
    if (typeof token !== "string") {
      sendJson(response, 401, { error: "unauthorized" });
      record(401, "access_assertion_missing", route);
      return;
    }
    if (authenticating >= config.maxConcurrent) {
      sendJson(response, 503, { error: "gateway_busy" });
      record(503, "authentication_capacity", route);
      return;
    }
    authenticating += 1;
    let authenticated = false;
    let authenticationOutcome = "access_assertion_rejected";
    try {
      await verifyToken(token);
      authenticated = true;
    } catch (error) {
      authenticationOutcome = accessAssertionRejection(error);
    } finally {
      authenticating = Math.max(0, authenticating - 1);
    }
    if (!authenticated) {
      sendJson(response, 401, { error: "unauthorized" });
      record(401, authenticationOutcome, route);
      return;
    }
    const eventStream = parsed.pathname === "/mcp" && request.method === "GET";
    const capacityUsed = eventStream ? activeEventStreams : active;
    if (capacityUsed >= config.maxConcurrent) {
      sendJson(response, 503, { error: "gateway_busy" });
      record(503, eventStream ? "event_stream_capacity" : "origin_capacity", route);
      return;
    }
    if (eventStream) activeEventStreams += 1;
    else active += 1;
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      if (eventStream) activeEventStreams = Math.max(0, activeEventStreams - 1);
      else active = Math.max(0, active - 1);
    };

    let upstreamResponse = null;
    const upstream = http.request({
      socketPath: config.originSocket,
      path: `${parsed.pathname}${parsed.search}`,
      method: request.method,
      headers: filteredRequestHeaders(request.headers, upstreamSecret),
    });
    response.once("close", () => {
      if (response.writableFinished) return;
      if (eventStream) {
        if (upstreamResponse === null) upstream.destroy();
        else {
          upstreamResponse.unpipe(response);
          upstreamResponse.destroy();
          upstream.destroy();
        }
        return;
      }
      if (upstreamResponse === null) return;
      upstreamResponse.unpipe(response);
      upstreamResponse.resume();
    });
    let received = 0;
    request.on("data", (chunk) => {
      received += chunk.length;
      if (received > config.maxBodyBytes) {
        upstream.destroy(new Error("request too large"));
        if (!response.headersSent) sendJson(response, 413, { error: "request_too_large" });
        record(413, "streamed_body_too_large", route);
        request.destroy();
      }
    });
    request.once("aborted", () => upstream.destroy());
    upstream.once("error", () => {
      release();
      if (response.destroyed) return;
      if (!response.headersSent) sendJson(response, 502, { error: "origin_unavailable" });
      else response.destroy();
      record(502, "origin_unavailable", route);
    });
    upstream.once("close", () => {
      if (upstreamResponse === null) release();
    });
    upstream.once("response", (originResponse) => {
      upstreamResponse = originResponse;
      originResponse.once("end", release);
      originResponse.once("close", release);
      originResponse.once("error", () => {
        release();
        if (!response.destroyed) response.destroy();
      });
      if (response.destroyed) {
        if (eventStream) {
          originResponse.destroy();
          upstream.destroy();
        }
        else originResponse.resume();
        record(originResponse.statusCode ?? 502, "client_disconnected", route);
        return;
      }
      record(originResponse.statusCode ?? 502, "origin_response", route);
      response.writeHead(
        originResponse.statusCode ?? 502,
        filteredResponseHeaders(originResponse.headers),
      );
      originResponse.pipe(response);
    });
    request.pipe(upstream);
  });

  server.requestTimeout = REQUEST_BODY_TIMEOUT_MS;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 5_000;
  server.maxRequestsPerSocket = 1000;
  server.maxConnections = 128;
  return server;
}

export function loadGatewayConfig(path) {
  const metadata = lstatSync(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) fail("Gateway config must be a regular file.");
  if ((metadata.mode & 0o077) !== 0) fail("Gateway config must not be group- or world-accessible.");
  if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
    fail("Gateway config must be owned by the gateway service user.");
  }
  return validateGatewayConfig(JSON.parse(readFileSync(path, "utf8")));
}

async function main() {
  const configIndex = process.argv.indexOf("--config");
  if (configIndex < 0 || !process.argv[configIndex + 1]) {
    fail("Usage: cloudflare_access_gateway.mjs --config /absolute/path/config.json");
  }
  const config = loadGatewayConfig(process.argv[configIndex + 1]);
  prepareUnixSocket(config.gatewaySocket);
  const server = createGatewayServer(config);
  server.listen(config.gatewaySocket, () => {
    chmodSync(config.gatewaySocket, 0o600);
    console.log("advisor domain MCP gateway listening on its private Cloudflare Unix socket");
  });
  const shutdown = () =>
    server.close(() => {
      try {
        unlinkSync(config.gatewaySocket);
      } catch {
        // The runtime directory may already be gone during shutdown.
      }
      process.exit(0);
    });
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main().catch((error) => {
    console.error(`advisor domain MCP gateway failed: ${error.message}`);
    process.exit(1);
  });
}
