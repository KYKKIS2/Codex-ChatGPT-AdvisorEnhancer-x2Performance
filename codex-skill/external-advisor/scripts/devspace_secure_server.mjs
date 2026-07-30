#!/usr/bin/env node

import { chmodSync, existsSync, lstatSync, mkdirSync, unlinkSync } from "node:fs";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";

function fail(message) {
  throw new Error(message);
}

function safeSocketPath(path) {
  if (!path.startsWith("/") || path.includes("\0")) fail("DEVSPACE_UNIX_SOCKET must be absolute.");
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (!existsSync(path)) return;
  const metadata = lstatSync(path);
  if (!metadata.isSocket() || (typeof process.getuid === "function" && metadata.uid !== process.getuid())) {
    fail("Refusing to replace a non-socket or foreign Unix-socket path.");
  }
  unlinkSync(path);
}

async function main() {
  const dist = process.env.DEVSPACE_DIST_DIR;
  const socketPath = process.env.DEVSPACE_UNIX_SOCKET;
  if (!dist?.startsWith("/") || !socketPath) {
    fail("DEVSPACE_DIST_DIR and DEVSPACE_UNIX_SOCKET are required.");
  }
  if (socketPath !== "/run/advisor-origin/devspace.sock") {
    fail("Secure DevSpace Unix socket is not the pinned origin path.");
  }
  const shellSandbox = process.env.DEVSPACE_SHELL_SANDBOX;
  const shellMaxSeconds = Number(process.env.DEVSPACE_SHELL_MAX_SECONDS);
  const processMaxActive = Number(process.env.DEVSPACE_PROCESS_MAX_ACTIVE);
  const disableSyncShell = process.env.DEVSPACE_DISABLE_SYNC_SHELL;
  if (
    !Number.isInteger(shellMaxSeconds) ||
    shellMaxSeconds < 300 ||
    shellMaxSeconds > 28_800
  ) {
    fail("Secure DevSpace shell timeout must match a 5-minute to 8-hour exposure window.");
  }
  if (!Number.isInteger(processMaxActive) || processMaxActive < 1 || processMaxActive > 64) {
    fail("Secure DevSpace active-process limit must be an integer from 1 through 64.");
  }
  if (disableSyncShell !== "true") {
    fail("Secure DevSpace must disable the synchronous shell tool.");
  }
  safeSocketPath(socketPath);
  const [{ loadConfig }, { createServer }] = await Promise.all([
    import(pathToFileURL(`${dist}/config.js`).href),
    import(pathToFileURL(`${dist}/server.js`).href),
  ]);
  const config = loadConfig();
  config.shellMaxSeconds = shellMaxSeconds;
  config.processMaxActive = processMaxActive;
  config.disableSyncShell = true;
  for (const name of Object.keys(process.env)) {
    if (name.startsWith("DEVSPACE_")) delete process.env[name];
  }
  if (!config.trustedProxyAuthFile || !config.pinnedExactRootFile) {
    fail("Secure DevSpace requires trusted-proxy authentication and a pinned exact root.");
  }
  if (config.trustedProxyAuthFile !== "/run/advisor-origin/origin-secret") {
    fail("Secure DevSpace trusted-proxy credential path is not the pinned runtime path.");
  }
  if (config.pinnedExactRootFile !== "/run/advisor-pinned-root") {
    fail("Secure DevSpace root pointer path is not pinned.");
  }
  if (shellSandbox !== "/opt/advisor/devspace_shell_sandbox.py") {
    fail("Secure DevSpace shell sandbox is not pinned.");
  }
  if (config.host !== "127.0.0.1") fail("Secure DevSpace HOST must remain loopback.");
  const instance = createServer(config);
  try {
    unlinkSync(config.trustedProxyAuthFile);
  } catch {
    instance.close();
    fail("Secure DevSpace could not remove its staged trusted-proxy credential.");
  }
  const server = instance.app.listen(socketPath, () => {
    chmodSync(socketPath, 0o600);
    console.log("secure DevSpace origin listening on its private Unix socket");
    console.log(`allowed roots: ${config.allowedRoots.join(", ")}`);
    console.log(`tool mode: ${config.toolMode}`);
    console.log("network: isolated by launcher");
  });
  const shutdown = () => {
    server.close(() => {
      instance.close();
      try {
        unlinkSync(socketPath);
      } catch {
        // The runtime directory may already be gone during shutdown.
      }
      process.exit(0);
    });
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

main().catch((error) => {
  console.error(`secure DevSpace origin failed: ${error.message}`);
  process.exit(1);
});
