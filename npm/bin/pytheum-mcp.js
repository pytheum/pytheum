#!/usr/bin/env node
// Thin Node shim that locates `uv`/`uvx` and execs the `pytheum-mcp` console
// script from the `pytheum` PyPI package via uv tooling.
// Exists because Claude Desktop bundles Node but NOT Python — so we spawn the
// real (Python) MCP server through uv, which the user installs separately.
//
// Re-homed: the MCP server now ships inside the `pytheum` package (its
// `pytheum-mcp` entry point), so we run `uvx --from pytheum pytheum-mcp`
// rather than installing a standalone `pytheum-mcp` distribution.

import { spawn } from "node:child_process";
import { access, constants } from "node:fs/promises";
import { delimiter, join } from "node:path";
import { homedir } from "node:os";

const PATH_DIRS = (process.env.PATH || "").split(delimiter).filter(Boolean);
const EXE_SUFFIXES = process.platform === "win32" ? [".exe", ".cmd", ".bat", ""] : [""];

async function findOnPath(name) {
  for (const dir of PATH_DIRS) {
    for (const ext of EXE_SUFFIXES) {
      const candidate = join(dir, name + ext);
      try {
        await access(candidate, constants.X_OK);
        return candidate;
      } catch {
        // not here, keep looking
      }
    }
  }
  return null;
}

async function fileExecutable(path) {
  try {
    await access(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

async function resolveUvCommand() {
  // Prefer `uvx` (single-binary entry to `uv tool run`) if it's on PATH.
  const uvxOnPath = await findOnPath("uvx");
  if (uvxOnPath) return { cmd: uvxOnPath, prefixArgs: [] };

  // Some installs only expose `uv`; `uv tool run` is equivalent to `uvx`.
  const uvOnPath = await findOnPath("uv");
  if (uvOnPath) return { cmd: uvOnPath, prefixArgs: ["tool", "run"] };

  // `uv`'s default installers drop the binary into `~/.local/bin` (astral.sh
  // standalone installer) or `~/.cargo/bin` (when installed via `cargo install`).
  // Neither is on PATH by default for GUI-launched apps like Claude Desktop,
  // so check them explicitly before giving up.
  const home = homedir();
  for (const dir of [join(home, ".local", "bin"), join(home, ".cargo", "bin")]) {
    const uvxPath = join(dir, "uvx");
    if (await fileExecutable(uvxPath)) return { cmd: uvxPath, prefixArgs: [] };
    const uvPath = join(dir, "uv");
    if (await fileExecutable(uvPath)) return { cmd: uvPath, prefixArgs: ["tool", "run"] };
  }

  return null;
}

const resolved = await resolveUvCommand();
if (!resolved) {
  process.stderr.write(
    "pytheum-mcp: could not find `uv` or `uvx` on PATH.\n" +
      "Install uv (Python package runner) — it's required to run the underlying pytheum MCP server.\n" +
      "macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n" +
      "Docs: https://docs.astral.sh/uv/\n",
  );
  process.exit(1);
}

const userArgs = process.argv.slice(2);
// `--from pytheum`: install the `pytheum` distribution, then run its
// `pytheum-mcp` console script (entry point pytheum.mcp.server:main).
const child = spawn(
  resolved.cmd,
  [...resolved.prefixArgs, "--from", "pytheum", "pytheum-mcp", ...userArgs],
  { stdio: "inherit", env: process.env },
);

for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(sig, () => child.kill(sig));
}

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
