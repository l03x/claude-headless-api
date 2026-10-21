// k6 stress test for the API running in test mode (scripts/test_mode.sh): the fake Claude CLI
// answers, so no tokens are spent. Refuses to run against a server backed by the real CLI.
//
//   k6 run loadtest/stress.js                        # smoke: 2 VUs, 30 s
//   k6 run -e PROFILE=stress loadtest/stress.js      # smoke | load | stress | spike | soak
//
// Options (-e NAME=value): BASE_URL (default http://localhost:8020), ADMIN_USER / ADMIN_PASSWORD
// (default admin / test-mode-password), TOKENS (API tokens to spread load over, default 10),
// MODEL (default haiku), THINK (max seconds between requests per VU, default 1), TIMEOUT (client
// timeout per request, default 5m: like real SDK clients, so queueing shows up as latency).
//
// Setup signs in to the dashboard, creates a throwaway user with unlimited-rate tokens, and
// teardown deletes that user again (with its tokens and sessions).

import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";
import exec from "k6/execution";

const BASE = (__ENV.BASE_URL || "http://localhost:8020").replace(/\/$/, "");
const ADMIN_USER = __ENV.ADMIN_USER || "admin";
const ADMIN_PASSWORD = __ENV.ADMIN_PASSWORD || "test-mode-password";
const TOKENS = Number(__ENV.TOKENS || 10);
const MODEL = __ENV.MODEL || "haiku";
const THINK = Number(__ENV.THINK || 1);
const PROFILE = __ENV.PROFILE || "smoke";
const TIMEOUT = __ENV.TIMEOUT || "5m";
const ADMIN = { tags: { name: "admin" }, timeout: "5m" };

const PROFILES = {
  smoke: { executor: "constant-vus", vus: 2, duration: "30s" },
  load: {
    executor: "ramping-vus",
    stages: [
      { duration: "1m", target: 20 },
      { duration: "3m", target: 20 },
      { duration: "30s", target: 0 },
    ],
  },
  stress: {
    executor: "ramping-vus",
    stages: [
      { duration: "1m", target: 25 },
      { duration: "2m", target: 50 },
      { duration: "2m", target: 100 },
      { duration: "2m", target: 150 },
      { duration: "1m", target: 0 },
    ],
  },
  spike: {
    executor: "ramping-vus",
    stages: [
      { duration: "10s", target: 5 },
      { duration: "10s", target: 200 },
      { duration: "1m", target: 200 },
      { duration: "10s", target: 5 },
      { duration: "30s", target: 5 },
    ],
  },
  soak: { executor: "constant-vus", vus: 20, duration: "30m" },
};

if (!PROFILES[PROFILE]) throw new Error(`Unknown PROFILE '${PROFILE}'. Use: ${Object.keys(PROFILES).join(", ")}`);

// Under overload some failures are the expected result (timeouts, queueing); the thresholds flag them
// without hiding the latency numbers. Tighten them for smoke/load runs.
const MAX_FAILED = PROFILE === "smoke" || PROFILE === "load" ? 0.01 : 0.1;

export const options = {
  scenarios: {
    [PROFILE]: {
      ...PROFILES[PROFILE],
      gracefulStop: "60s",
      ...(PROFILES[PROFILE].executor === "ramping-vus" ? { gracefulRampDown: "60s" } : {}),
    },
  },
  thresholds: {
    http_req_failed: [`rate<${MAX_FAILED}`],
    checks: [`rate>${1 - MAX_FAILED}`],
    "http_req_duration{name:models}": ["p(95)<500"], // no Claude involved: pure app overhead
  },
  setupTimeout: "2m",
  teardownTimeout: "2m",
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const claudeLatency = new Trend("claude_turn_duration", true);
const rateLimited = new Counter("status_429");
const upstreamErrors = new Counter("status_5xx");

// --- dashboard helpers (setup / teardown) -----------------------------------------------

function csrf(path) {
  const res = http.get(`${BASE}${path}`, ADMIN);
  const m = res.body && res.body.match(/name="csrf_token" value="([^"]+)"/);
  if (!m) fail(`No CSRF token on ${path} (status ${res.status}). Is the server up at ${BASE}?`);
  return m[1];
}

function login() {
  const token = csrf("/admin/login");
  const res = http.post(
    `${BASE}/admin/login`,
    { username: ADMIN_USER, password: ADMIN_PASSWORD, csrf_token: token, next: "/admin" },
    ADMIN,
  );
  if (res.status !== 200 || !res.url.endsWith("/admin")) fail(`Admin login failed (status ${res.status}).`);
}

const headers = (token) => ({ Authorization: `Bearer ${token}`, "Content-Type": "application/json" });

export function setup() {
  login();
  const name = `k6-${Date.now()}`;
  const res = http.post(`${BASE}/admin/users`, { username: name, csrf_token: csrf("/admin/users") }, ADMIN);
  const userId = (res.url.match(/\/admin\/users\/(\d+)$/) || [])[1];
  if (!userId) fail(`Could not create the load-test user (status ${res.status}).`);

  const tokens = [];
  for (let i = 0; i < TOKENS; i++) {
    const page = http.post(
      `${BASE}/admin/tokens`,
      { csrf_token: csrf("/admin/tokens"), user_id: userId, name: `k6-${i}`, rate_limit_rpm: "0" },
      ADMIN,
    );
    const m = page.body.match(/id="new-token">(chk_[^<]+)</);
    if (!m) fail(`Could not create API token ${i} (status ${page.status}).`);
    tokens.push(m[1]);
  }

  // Safety: only the fake CLI answers "echo[…]". Anything else means real Claude, so stop now.
  const probe = http.post(
    `${BASE}/v1/chat/completions`,
    JSON.stringify({ model: MODEL, effort: "low", messages: [{ role: "user", content: "ping" }] }),
    { headers: headers(tokens[0]), tags: { name: "probe" } },
  );
  const text = probe.status === 200 ? probe.json("choices.0.message.content") : "";
  if (!String(text).startsWith("echo[")) {
    teardown({ userId });
    fail(`Target is not in test mode (status ${probe.status}, reply ${JSON.stringify(text).slice(0, 80)}). ` +
         "Start it with scripts/test_mode.sh; this script never runs against real Claude.");
  }
  console.log(`Load-test user ${name} (#${userId}) with ${tokens.length} tokens, profile ${PROFILE}.`);
  return { userId, tokens };
}

export function teardown(data) {
  if (!data || !data.userId) return;
  try {
    login();
    http.post(`${BASE}/admin/users/${data.userId}/delete`, { csrf_token: csrf(`/admin/users/${data.userId}`) }, ADMIN);
  } catch (e) {
    console.warn(`Could not delete load-test user #${data.userId} (${e}). Delete it in the dashboard.`);
  }
}

// --- traffic --------------------------------------------------------------------------

function record(res, kind) {
  if (res.status === 429) rateLimited.add(1);
  if (res.status >= 500) upstreamErrors.add(1, { status: String(res.status) });
  if (kind !== "models") claudeLatency.add(res.timings.duration, { kind });
}

function chat(token) {
  const body = JSON.stringify({
    model: MODEL,
    effort: "low",
    messages: [
      { role: "system", content: "Be brief." },
      { role: "user", content: `Load test message ${exec.scenario.iterationInTest}` },
    ],
  });
  const res = http.post(`${BASE}/v1/chat/completions`, body, { headers: headers(token), tags: { name: "chat" }, timeout: TIMEOUT });
  record(res, "chat");
  check(res, {
    "chat: 200": (r) => r.status === 200,
    "chat: has reply": (r) => r.status === 200 && r.json("choices.0.message.content") !== "",
  });
}

// Each VU keeps one session for a few turns, then deletes it and starts another.
let sessionId = null;
let turns = 0;

function sessionTurn(token) {
  if (!sessionId) {
    const res = http.post(`${BASE}/v1/sessions`, JSON.stringify({ model: MODEL, effort: "low" }), {
      headers: headers(token),
      tags: { name: "session_create" },
      timeout: TIMEOUT,
    });
    if (!check(res, { "session: created": (r) => r.status === 201 })) return;
    sessionId = res.json("id");
    turns = 0;
  }
  const res = http.post(
    `${BASE}/v1/sessions/${sessionId}/messages`,
    JSON.stringify({ content: `Turn ${turns + 1}` }),
    { headers: headers(token), tags: { name: "session_turn" }, timeout: TIMEOUT },
  );
  record(res, "session");
  turns += 1;
  check(res, {
    "session turn: 200": (r) => r.status === 200,
    // The fake CLI numbers resumed turns ("#2", "#3", …): proves the Claude session really resumed.
    "session turn: resumed": (r) => r.status === 200 && r.json("choices.0.message.content").includes(`#${turns}:`),
  });
  if (turns >= 5) {
    const del = http.del(`${BASE}/v1/sessions/${sessionId}`, null, { headers: headers(token), tags: { name: "session_delete" }, timeout: TIMEOUT });
    check(del, { "session: deleted": (r) => r.status === 200 });
    sessionId = null;
  }
}

function models(token) {
  const res = http.get(`${BASE}/v1/models`, { headers: headers(token), tags: { name: "models" }, timeout: TIMEOUT });
  record(res, "models");
  check(res, { "models: 200": (r) => r.status === 200 });
}

export default function (data) {
  const token = data.tokens[(exec.vu.idInTest - 1) % data.tokens.length];
  const roll = Math.random();
  if (roll < 0.6) chat(token);
  else if (roll < 0.9) sessionTurn(token);
  else models(token);
  sleep(Math.random() * THINK);
}
