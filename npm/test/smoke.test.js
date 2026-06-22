import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, chmod, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SHIM = join(__dirname, "..", "bin", "pytheum-mcp.js");

function runShim({ env }) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [SHIM], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("exit", (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
}

test("with uvx stub on PATH, shim spawns it with `--from pytheum pytheum-mcp`", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pytheum-mcp-test-uvx-"));
  // Stub uvx: writes its argv to a marker file, then exits 0.
  const marker = join(dir, "marker.txt");
  const uvxStub = join(dir, "uvx");
  await writeFile(
    uvxStub,
    `#!/bin/sh\nprintf '%s\\n' "$@" > "${marker}"\nexit 0\n`,
  );
  await chmod(uvxStub, 0o755);

  // Empty HOME so the fallback dir checks can't accidentally find a real uv.
  const emptyHome = await mkdtemp(join(tmpdir(), "pytheum-mcp-test-home-"));

  const result = await runShim({
    env: { PATH: dir, HOME: emptyHome },
  });

  assert.equal(result.code, 0, `expected exit 0, got ${result.code}; stderr: ${result.stderr}`);

  // Confirm the stub was invoked with `--from pytheum pytheum-mcp` (re-homed:
  // install the `pytheum` distribution, run its `pytheum-mcp` console script).
  const { readFile } = await import("node:fs/promises");
  const recordedArgs = (await readFile(marker, "utf8")).trim().split("\n");
  assert.deepEqual(recordedArgs.slice(0, 3), ["--from", "pytheum", "pytheum-mcp"]);
});

test("with no uv/uvx anywhere, shim prints install hint and exits 1", async () => {
  // PATH points at an empty dir; HOME points at an empty dir (so no
  // ~/.local/bin/uv or ~/.cargo/bin/uv either).
  const emptyPath = await mkdtemp(join(tmpdir(), "pytheum-mcp-test-emptypath-"));
  const emptyHome = await mkdtemp(join(tmpdir(), "pytheum-mcp-test-emptyhome-"));
  // Make sure the synthetic ~/.local/bin and ~/.cargo/bin exist but are empty
  // (covers the case where the directory exists but no uv binary is in it).
  await mkdir(join(emptyHome, ".local", "bin"), { recursive: true });
  await mkdir(join(emptyHome, ".cargo", "bin"), { recursive: true });

  const result = await runShim({
    env: { PATH: emptyPath, HOME: emptyHome },
  });

  assert.equal(result.code, 1, `expected exit 1, got ${result.code}`);
  assert.match(result.stderr, /could not find `uv` or `uvx`/);
  assert.match(result.stderr, /astral\.sh\/uv/);
});
