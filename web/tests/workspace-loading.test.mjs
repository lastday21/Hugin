import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const source = process.env.HUGIN_TEST_API_FILE
  ? pathToFileURL(process.env.HUGIN_TEST_API_FILE)
  : new URL("../node_modules/.tmp/workspace-tests/api.js", import.meta.url);
const compiled = await readFile(source, "utf8");
const { loadWorkspace, reconcileForms } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
globalThis.window = { setTimeout, clearTimeout };

function heldRequests() {
  const pending = [];
  const paths = [];
  let active = 0;
  let peak = 0;
  globalThis.fetch = (path, { signal }) => new Promise((resolve, reject) => {
    paths.push(path);
    active += 1;
    peak = Math.max(peak, active);
    let finished = false;
    const finish = (error = false) => {
      if (finished) return;
      finished = true;
      active -= 1;
      if (error) resolve(new Response(JSON.stringify({ detail: "failed section" }), { status: 503 }));
      else resolve(new Response("{}", { status: 200 }));
    };
    signal.addEventListener("abort", () => {
      if (finished) return;
      finished = true;
      active -= 1;
      reject(new DOMException("aborted", "AbortError"));
    }, { once: true });
    pending.push(finish);
  });
  return { pending, paths, peak: () => peak };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("secondary sections wait until every main section finishes", async () => {
  const requests = heldRequests();
  const controller = new AbortController();
  const loading = loadWorkspace(() => {}, controller.signal);
  try {
    await settle();
    requests.pending.shift()();
    requests.pending.shift()();
    await settle();
    requests.pending.shift()();
    requests.pending.shift()();
    await settle();
    assert.equal(requests.paths.length, 5, "heavy secondary reads must not compete with pending outcomes");
    requests.pending.shift()();
    await settle();
    assert.equal(requests.paths.length, 7, "secondary reads resume after the main view completes");
  } finally {
    controller.abort();
    await loading;
  }
});

test("workspace keeps requests bounded, loads the main view first and continues after an error", async () => {
  const requests = heldRequests();
  const controller = new AbortController();
  const callbacks = [];
  const loading = loadWorkspace((section, update, error) => callbacks.push({ section, update, error }), controller.signal);
  try {
    await settle();
    assert.equal(requests.paths.length, 2, "only two independent database reads should start together");
    let failed = false;
    while (callbacks.length < 12) {
      const finish = requests.pending.shift();
      assert.ok(finish, "remaining workspace sections must continue loading");
      finish(!failed);
      failed = true;
      await settle();
    }
    await loading;
    assert.equal(requests.peak(), 2);
    assert.deepEqual(requests.paths.slice(0, 5).map((path) => path.split("?")[0]), [
      "/api/processes", "/api/dashboard", "/api/queue", "/api/forms", "/api/outcomes",
    ]);
    assert.equal(callbacks.filter((item) => item.error).length, 1);
    assert.equal(callbacks.filter((item) => !item.error).length, 11);
  } finally {
    controller.abort();
    await loading;
  }
});

test("cancelling a workspace load does not start queued reads or publish stale sections", async () => {
  const requests = heldRequests();
  const controller = new AbortController();
  const callbacks = [];
  const loading = loadWorkspace((...args) => callbacks.push(args), controller.signal);
  await settle();
  controller.abort();
  await loading;
  assert.equal(requests.paths.length, 2);
  assert.equal(callbacks.length, 0);
});

test("workspace waits for background snapshots, reports real errors and cancels pending polling", async () => {
  const paths = [];
  const delays = [];
  const callbacks = [];
  let mode = "complete";
  globalThis.window = {
    setTimeout(callback, delay) {
      if (delay === 1_000) {
        delays.push(callback);
        return 1;
      }
      return setTimeout(callback, delay);
    },
    clearTimeout,
  };
  globalThis.fetch = async (path) => {
    paths.push(path);
    if (path.startsWith("/api/processes?")) {
      assert.ok(path.endsWith("&background=true"));
      return new Response(JSON.stringify({ kind: "workspace-read", job_id: "process-job", state: "running" }), { status: 202 });
    }
    if (path.includes("/workspace-reads/process-job?")) {
      if (mode === "fail") return new Response(JSON.stringify({ detail: "snapshot failed" }), { status: 500 });
      return new Response(JSON.stringify({ marker: "fresh processes" }));
    }
    return new Response("{}");
  };
  try {
    for (mode of ["complete", "fail", "abort"]) {
      const controller = new AbortController();
      callbacks.length = 0;
      const loading = loadWorkspace((section, update, error) => callbacks.push({ section, update, error }), controller.signal);
      await settle();
      assert.equal(callbacks.some(({ section }) => section === "processes"), false, "pending snapshots must not appear as fresh data");
      const before = paths.length;
      if (mode === "abort") controller.abort();
      else delays.shift()();
      await loading;
      if (mode === "complete") {
        assert.deepEqual(callbacks.find(({ section }) => section === "processes").update, { processes: { marker: "fresh processes" } });
        assert.equal(callbacks.length, 12);
      } else if (mode === "fail") {
        assert.equal(callbacks.find(({ section }) => section === "processes").error, "snapshot failed");
        assert.equal(callbacks.length, 12, "other sections still load after a failed snapshot");
      } else {
        assert.equal(paths.length, before, "cancelled cycles must not send further status requests");
        assert.equal(callbacks.some(({ section }) => section === "processes"), false);
      }
    }
  } finally {
    globalThis.window = { setTimeout, clearTimeout };
  }
});

test("form reconciliation waits for completion, reports failure and cancels only its polling", async () => {
  const paths = [];
  const delays = [];
  let nextState = "running";
  globalThis.window = {
    setTimeout(callback, delay) {
      if (delay === 1_000) {
        delays.push(callback);
        return 1;
      }
      return setTimeout(callback, delay);
    },
    clearTimeout,
  };
  globalThis.fetch = async (path) => {
    paths.push(path);
    if (path === "/api/session") return new Response(JSON.stringify({ key: "local" }));
    return new Response(JSON.stringify({ job_id: "job", state: nextState, error: "check failed" }));
  };
  try {
    let completed = false;
    const first = reconcileForms().then(() => { completed = true; });
    await settle();
    assert.equal(completed, false);
    assert.equal(paths.length, 2);
    assert.ok(paths[1].endsWith("&background=true"));
    nextState = "completed";
    delays.shift()();
    await first;
    assert.equal(completed, true);
    assert.ok(paths[2].includes("/reconcile/job?"));

    nextState = "failed";
    await assert.rejects(reconcileForms(), /check failed/);

    nextState = "running";
    const controller = new AbortController();
    const pending = reconcileForms(controller.signal);
    const rejected = assert.rejects(pending, { name: "AbortError" });
    await settle();
    const beforeAbort = paths.length;
    controller.abort();
    await rejected;
    await settle();
    assert.equal(paths.length, beforeAbort, "unmounted UI must not send another status request");
  } finally {
    globalThis.window = { setTimeout, clearTimeout };
  }
});
