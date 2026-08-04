#!/usr/bin/env node

import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  createAccessTokenVerifier,
  createGatewayServer,
  validateGatewayConfig,
  verifyAccessJwt,
} from "../codex-skill/external-advisor/scripts/cloudflare_access_gateway.mjs";

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: "jwk" }), kid: "test-key", alg: "RS256", use: "sig" };
const now = Math.floor(Date.now() / 1000);
const config = validateGatewayConfig({
  accessIssuer: "https://advisor-test.cloudflareaccess.com",
  accessAudience: "audience_test_value",
  allowedEmails: ["owner@example.com"],
  publicHostname: "mcp.example.com",
  gatewaySocket: "/tmp/test-gateway.sock",
  originSocket: "/tmp/test-origin.sock",
  upstreamSecretFile: "/tmp/test-origin.secret",
  maxBodyBytes: 1024,
  maxConcurrent: 2,
});

function token(overrides = {}, headerOverrides = {}) {
  const header = Buffer.from(
    JSON.stringify({ alg: "RS256", typ: "JWT", kid: jwk.kid, ...headerOverrides }),
  ).toString("base64url");
  const payload = Buffer.from(
    JSON.stringify({
      iss: config.accessIssuer,
      aud: [config.accessAudience],
      email: "owner@example.com",
      sub: "owner",
      iat: now - 5,
      nbf: now - 5,
      exp: now + 300,
      ...overrides,
    }),
  ).toString("base64url");
  const signature = sign(
    "RSA-SHA256",
    Buffer.from(`${header}.${payload}`, "ascii"),
    privateKey,
  ).toString("base64url");
  return `${header}.${payload}.${signature}`;
}

assert.equal(verifyAccessJwt(token(), config, jwk, now).email, "owner@example.com");
assert.throws(() => verifyAccessJwt(token({ aud: ["wrong"] }), config, jwk, now), /audience/);
assert.throws(() => verifyAccessJwt(token({ email: "other@example.com" }), config, jwk, now), /identity/);
assert.throws(() => verifyAccessJwt(token({ exp: now - 1000 }), config, jwk, now), /expired/);
assert.throws(() => verifyAccessJwt(token({ iat: undefined }), config, jwk, now), /issue time/);
assert.throws(() => verifyAccessJwt(token({ sub: "" }), config, jwk, now), /subject/);
assert.equal(
  verifyAccessJwt(
    token({ iat: now - 3600, nbf: now - 3600, exp: now + 24 * 3600 }),
    config,
    jwk,
    now,
  ).email,
  "owner@example.com",
);
assert.throws(
  () => verifyAccessJwt(token({ iat: now - 5, nbf: now - 5, exp: now - 5 }), config, jwk, now),
  /temporal/,
);
assert.throws(
  () => verifyAccessJwt(token({ iat: now, nbf: now, exp: now - 1 }), config, jwk, now),
  /expired|temporal/,
);
assert.throws(
  () => verifyAccessJwt(token({ iat: now - 5, nbf: now + 600, exp: now + 300 }), config, jwk, now),
  /not active|temporal/,
);
assert.throws(
  () =>
    validateGatewayConfig({
      ...config,
      allowedEmails: ["owner@example.com", "second@example.com"],
  }),
  /Exactly one/,
);
assert.equal(validateGatewayConfig({ ...config, maxConcurrent: undefined }).maxConcurrent, 8);
assert.throws(
  () => validateGatewayConfig({ ...config, maxConcurrent: 65 }),
  /maxConcurrent/,
);

let jwksFetches = 0;
const verifier = createAccessTokenVerifier(config, {
  fetchJwks: async () => {
    jwksFetches += 1;
    return { keys: [jwk] };
  },
});
await verifier(token());
await verifier(token());
assert.equal(jwksFetches, 1);
await assert.rejects(() => verifier(token({}, { kid: "unknown-key" })), /signing key/);
await assert.rejects(() => verifier(token({}, { kid: "unknown-key" })), /signing key/);
assert.equal(jwksFetches, 1);

const realDateNow = Date.now;
let fakeNow = realDateNow();
let staleFetches = 0;
Date.now = () => fakeNow;
try {
  const failClosedVerifier = createAccessTokenVerifier(config, {
    fetchJwks: async () => {
      staleFetches += 1;
      if (staleFetches > 1) throw new Error("simulated JWKS outage");
      return { keys: [jwk] };
    },
  });
  const longToken = token({ exp: now + 600 });
  await failClosedVerifier(longToken);
  fakeNow += 5 * 60 * 1000 + 1;
  await assert.rejects(() => failClosedVerifier(longToken), /simulated JWKS outage/);
} finally {
  Date.now = realDateNow;
}

const temporary = mkdtempSync(join(tmpdir(), "advisor-gateway-test-"));
const socketPath = join(temporary, "origin.sock");
const gatewaySocketPath = join(temporary, "gateway.sock");
const serialGatewaySocketPath = join(temporary, "serial-gateway.sock");
const seen = [];
const gatewayLogs = [];
const parallelResponses = [];
let parallelActive = 0;
let parallelPeak = 0;
let resolveParallelReady;
const parallelReady = new Promise((resolve) => {
  resolveParallelReady = resolve;
});
let abandonedResponse;
let resolveAbandonedReady;
const abandonedReady = new Promise((resolve) => {
  resolveAbandonedReady = resolve;
});
let streamCloseCount = 0;
const origin = http.createServer((request, response) => {
  const chunks = [];
  request.on("data", (chunk) => chunks.push(chunk));
  request.on("end", () => {
    seen.push({
      gatewaySecret: request.headers["x-advisor-gateway-secret"],
      authorization: request.headers.authorization,
      cookie: request.headers.cookie,
      accessJwt: request.headers["cf-access-jwt-assertion"],
      body: Buffer.concat(chunks).toString("utf8"),
    });
    if (request.url === "/mcp?parallel") {
      parallelActive += 1;
      parallelPeak = Math.max(parallelPeak, parallelActive);
      parallelResponses.push(response);
      response.once("close", () => {
        parallelActive -= 1;
      });
      if (parallelResponses.length === 2) resolveParallelReady();
      return;
    }
    if (request.url === "/mcp?abandon") {
      abandonedResponse = response;
      resolveAbandonedReady();
      return;
    }
    if (request.url === "/mcp?stream") {
      response.once("close", () => {
        streamCloseCount += 1;
      });
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
      });
      response.flushHeaders();
      response.write(": connected\n\n");
      return;
    }
    response.writeHead(200, {
      "content-type": "application/json",
      "set-cookie": "must-not-leave-origin=1",
      "www-authenticate": "Bearer private-origin",
    });
    response.end(JSON.stringify({ ok: true }));
  });
});
await new Promise((resolve, reject) => {
  origin.once("error", reject);
  origin.listen(socketPath, resolve);
});

const testConfig = { ...config, originSocket: socketPath, gatewaySocket: gatewaySocketPath };
const gateway = createGatewayServer(testConfig, {
  verifyToken: async (value) => {
    if (value === "rejected-token") {
      throw new Error("Cloudflare Access token temporal claims are invalid.");
    }
    assert.equal(value, token());
    return { email: "owner@example.com" };
  },
  upstreamSecret: "s".repeat(43),
  logEvent: (event, fields) => gatewayLogs.push({ event, ...fields }),
});
await new Promise((resolve, reject) => {
  gateway.once("error", reject);
  gateway.listen(gatewaySocketPath, resolve);
});
const address = gateway.address();
assert.equal(address, gatewaySocketPath);

function request(
  path,
  headers = {},
  body = "",
  method = body ? "POST" : "GET",
  socket = gatewaySocketPath,
) {
  return new Promise((resolve, reject) => {
    const outgoing = http.request(
      {
        socketPath: socket,
        method,
        path,
        headers,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () =>
          resolve({
            status: response.statusCode,
            headers: response.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          }),
        );
      },
    );
    outgoing.once("error", reject);
    outgoing.end(body);
  });
}

function streamingRequest(path, headers, socket = gatewaySocketPath) {
  return new Promise((resolve, reject) => {
    const outgoing = http.request(
      {
        socketPath: socket,
        method: "GET",
        path,
        headers,
      },
      (response) => resolve({ outgoing, response }),
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
}

const validToken = token();
const proxied = await request(
  "/mcp",
  {
    host: config.publicHostname,
    "cf-access-jwt-assertion": validToken,
    authorization: "Bearer must-not-reach-origin",
    cookie: "must-not-reach-origin=1",
    "content-type": "application/json",
  },
  '{"jsonrpc":"2.0"}',
);
assert.equal(proxied.status, 200);
assert.deepEqual(JSON.parse(proxied.body), { ok: true });
assert.equal(proxied.headers["set-cookie"], undefined);
assert.equal(proxied.headers["www-authenticate"], undefined);
assert.equal(seen.length, 1);
assert.equal(seen[0].gatewaySecret, "s".repeat(43));
assert.equal(seen[0].authorization, undefined);
assert.equal(seen[0].cookie, undefined);
assert.equal(seen[0].accessJwt, undefined);
assert.equal(gatewayLogs.at(-1)?.outcome, "origin_response");

assert.equal((await request("/mcp", { host: config.publicHostname })).status, 401);
assert.equal(
  (
    await request("/mcp", {
      host: config.publicHostname,
      "cf-access-jwt-assertion": "rejected-token",
    })
  ).status,
  401,
);
assert.equal(gatewayLogs.at(-1)?.outcome, "access_assertion_temporal");
assert.equal(
  (
    await request(
      "/mcp",
      {
        host: config.publicHostname,
        "cf-access-jwt-assertion": validToken,
      },
      "",
      "PUT",
    )
  ).status,
  405,
);
assert.equal(
  (
    await request("/mcp", {
      host: "wrong.example.com",
      "cf-access-jwt-assertion": validToken,
    })
  ).status,
  404,
);
assert.equal(
  (
    await request("/mcp", {
      host: `${config.publicHostname}:8443`,
      "cf-access-jwt-assertion": validToken,
    })
  ).status,
  404,
);
assert.equal(
  (
    await request(`http://${config.publicHostname}/mcp`, {
      host: config.publicHostname,
      "cf-access-jwt-assertion": validToken,
    })
  ).status,
  404,
);
assert.equal(
  (
    await request("/mcp-app-assets/%2e%2e/private", {
      host: config.publicHostname,
      "cf-access-jwt-assertion": validToken,
    })
  ).status,
  404,
);
assert.equal(
  (
    await request(
      "/mcp",
      {
        host: config.publicHostname,
        "cf-access-jwt-assertion": validToken,
        "content-length": String(config.maxBodyBytes + 1),
      },
      "x".repeat(config.maxBodyBytes + 1),
      "POST",
    )
  ).status,
  413,
);
assert.equal((await request("/__advisor_mcp_health", { host: config.publicHostname })).status, 404);
assert.equal(
  (
    await request("/__advisor_mcp_health", {
      host: "localhost",
      "x-advisor-health-secret": "s".repeat(43),
    })
  ).status,
  200,
);
assert.equal(
  (await request("/__advisor_mcp_health", { host: "localhost" }, "", "GET")).status,
  404,
);
assert.equal(
  (
    await request(
      "/__advisor_mcp_health",
      { host: "localhost", "x-advisor-health-secret": "s".repeat(43) },
      "",
      "POST",
    )
  ).status,
  404,
);
assert.equal(gateway.maxConnections, 128);
assert.equal(gateway.requestTimeout, 60_000);
assert.equal(seen.length, 1);
const serializedGatewayLogs = JSON.stringify(gatewayLogs);
for (const forbidden of [
  validToken,
  "owner@example.com",
  config.publicHostname,
  "must-not-reach-origin",
  '{"jsonrpc":"2.0"}',
  "?parallel",
]) {
  assert.equal(serializedGatewayLogs.includes(forbidden), false);
}
for (const entry of gatewayLogs) {
  assert.deepEqual(
    Object.keys(entry).sort(),
    [
      "durationMs",
      "event",
      "method",
      "ordinal",
      "outcome",
      "route",
      "sessionHeaderPresent",
      "status",
    ],
  );
}

const authenticatedHeaders = {
  host: config.publicHostname,
  "cf-access-jwt-assertion": validToken,
};
const parallelOne = request("/mcp?parallel", authenticatedHeaders, "{}", "POST");
const parallelTwo = request("/mcp?parallel", authenticatedHeaders, "{}", "POST");
await parallelReady;
assert.equal(parallelPeak, 2);
assert.equal((await request("/mcp", authenticatedHeaders, "{}", "POST")).status, 503);
for (const pendingResponse of parallelResponses) {
  pendingResponse.writeHead(200, { "content-type": "application/json" });
  pendingResponse.end(JSON.stringify({ parallel: true }));
}
for (const result of await Promise.all([parallelOne, parallelTwo])) {
  assert.equal(result.status, 200);
  assert.deepEqual(JSON.parse(result.body), { parallel: true });
}

const serialGateway = createGatewayServer(
  { ...testConfig, gatewaySocket: serialGatewaySocketPath, maxConcurrent: 1 },
  {
    verifyToken: async () => ({ email: "owner@example.com" }),
    upstreamSecret: "s".repeat(43),
    logEvent: () => {},
  },
);
await new Promise((resolve, reject) => {
  serialGateway.once("error", reject);
  serialGateway.listen(serialGatewaySocketPath, resolve);
});
for (let reconnect = 0; reconnect < 100; reconnect += 1) {
  const liveStream = await streamingRequest(
    "/mcp?stream",
    authenticatedHeaders,
    serialGatewaySocketPath,
  );
  assert.equal(liveStream.response.statusCode, 200);
  if (reconnect === 0) {
    assert.equal(
      (
        await request(
          "/mcp?stream-overflow",
          authenticatedHeaders,
          "",
          "GET",
          serialGatewaySocketPath,
        )
      ).status,
      503,
    );
  }
  assert.equal(
    (await request("/mcp", authenticatedHeaders, "{}", "POST", serialGatewaySocketPath)).status,
    200,
  );
  liveStream.response.destroy();
  liveStream.outgoing.destroy();
  for (let attempt = 0; attempt < 20 && streamCloseCount <= reconnect; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(streamCloseCount, reconnect + 1);
}

const abandonedClient = http.request({
  socketPath: serialGatewaySocketPath,
  method: "POST",
  path: "/mcp?abandon",
  headers: { ...authenticatedHeaders, "content-length": "2" },
});
abandonedClient.once("error", () => {});
abandonedClient.end("{}");
await abandonedReady;
const abandonedClosed = new Promise((resolve) => abandonedClient.once("close", resolve));
abandonedClient.destroy();
await abandonedClosed;
assert.equal(
  (await request("/mcp", authenticatedHeaders, "{}", "POST", serialGatewaySocketPath)).status,
  503,
);
abandonedResponse.writeHead(200, { "content-type": "application/json" });
abandonedResponse.end(JSON.stringify({ abandoned: true }));
let afterAbandon;
for (let attempt = 0; attempt < 20; attempt += 1) {
  afterAbandon = await request(
    "/mcp",
    authenticatedHeaders,
    "{}",
    "POST",
    serialGatewaySocketPath,
  );
  if (afterAbandon.status !== 503) break;
  await new Promise((resolve) => setTimeout(resolve, 5));
}
assert.equal(afterAbandon.status, 200);

await new Promise((resolve) => serialGateway.close(resolve));
await new Promise((resolve) => gateway.close(resolve));
await new Promise((resolve) => origin.close(resolve));
rmSync(temporary, { recursive: true, force: true });
console.log("Cloudflare Access gateway tests passed.");
