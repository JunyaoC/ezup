/**
 * ezupdate Worker: the store side of `ezcl share`.
 *
 * A client publishes a session by registering it once (POST /v1/session) and
 * then appending byte ranges of the local JSONL transcript (POST /v1/chunk).
 * R2 objects are immutable and rate limited to one write per second per key, so
 * every chunk gets its own key derived from its byte offset -- publishing is
 * append-only by construction, and a retry of an already-stored range is a
 * database lookup rather than a write.
 *
 * D1 holds the index (who, what, which ranges); R2 holds the bytes. Nothing is
 * ever mutated in place, which is what makes an interrupted publish safe to
 * resume from the last acknowledged offset.
 *
 * Authorization is per session, not per team. Every session records the device
 * that registered it; only that device may write, delete or re-register it.
 * Reading is governed by the device's role: a plain `device` sees only its own
 * sessions, a `reader` (the PM pulling everything) sees all of them.
 *
 * A device can also mint its own readers (POST /v1/token): a reader whose
 * `scoped_device_id` names the minting device, so an operator can pull that
 * one machine's sessions and nothing else. Scoped readers are strictly
 * read-only -- they cannot write, delete, or mint further tokens.
 */

import { VIEWER_HTML } from "./viewer";

export interface Env {
  BUCKET: R2Bucket;
  DB: D1Database;
  /** Secret. Only gates POST /v1/device; absent means device minting is off. */
  ADMIN_TOKEN?: string;
}

/** Worker request bodies can reach 100 MB, but a chunk that large would sit in
 *  the isolate's 128 MB budget alongside its digest. The client chunks instead. */
const MAX_CHUNK = 8 * 1024 * 1024;
/** Session registration is a small JSON envelope; anything bigger is a mistake. */
const MAX_JSON = 64 * 1024;
/** One page of the polling feed. Clients advance `since` to walk past it. */
const SESSION_PAGE = 1000;
/** R2 accepts at most 1000 keys per bulk delete. */
const DELETE_BATCH = 1000;

/** Admin-token guessing budget: this many failures from one IP inside the
 *  window locks that IP out for the lockout. One correct guess is a token with
 *  read access to somebody's transcripts, so the budget is deliberately small. */
const ADMIN_FAILURE_LIMIT = 5;
const ADMIN_WINDOW_MS = 15 * 60 * 1000;
const ADMIN_LOCKOUT_MS = 15 * 60 * 1000;

const HEX64 = /^[0-9a-f]{64}$/;
/** Session ids and author names land verbatim in R2 keys, so they are
 *  restricted to characters that cannot escape a path segment. The first
 *  character must be alphanumeric, which is what rules out `.`, `..` and
 *  dotfiles: R2 keys are flat strings and would not traverse, but a puller
 *  turns a key back into a local path, and this is the grammar its own
 *  validator (transport.parse_chunk_key) enforces. Diverging would mean
 *  minting keys no client will accept. */
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

/** A device publishes and reads its own work; a reader (the PM) reads all of
 *  it and still publishes only its own. Roles never grant write access to
 *  another device's session -- that is ownership, and ownership is not a role. */
type Role = "device" | "reader";
const ROLES: readonly string[] = ["device", "reader"];

// --- small helpers --------------------------------------------------------

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

function fail(status: number, message: string, headers?: Record<string, string>): Response {
  return json({ error: message }, status, headers);
}

function hex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}

async function sha256Hex(text: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

/** Constant-time comparison of two equal-length hex strings.
 *  Callers pass digests, never raw secrets, so length is already uniform. */
function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function nowIso(): string {
  return new Date().toISOString();
}

/** Zero-padded so the lexical order of R2 keys is the byte order of the file. */
function chunkKey(author: string, session: string, offset: number, length: number): string {
  return `raw/${author}/${session}/${String(offset).padStart(12, "0")}-${length}.jsonl`;
}

function positiveInt(value: string | null, min: number): number | null {
  if (value === null || !/^\d{1,15}$/.test(value)) return null;
  const parsed = Number(value);
  return parsed >= min ? parsed : null;
}

/** The IP Cloudflare saw. Only used as a rate-limit bucket, never as identity:
 *  a spoofed or shared value costs its owner attempts, it never grants any. */
function clientIp(request: Request): string {
  return request.headers.get("cf-connecting-ip") ?? "unknown";
}

/** Reads at most `limit` bytes of the body.
 *
 *  content-length is a hint, not a guarantee: a chunked request declares no
 *  length at all, so the cap has to be enforced while reading rather than by
 *  trusting the header and letting request.json() buffer whatever arrives. */
async function readBodyText(request: Request, limit: number): Promise<string | null> {
  const declared = Number(request.headers.get("content-length") ?? "");
  if (Number.isFinite(declared) && declared > limit) return null;
  if (!request.body) return null;

  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > limit) {
        await reader.cancel();
        return null;
      }
      parts.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const joined = new Uint8Array(total);
  let at = 0;
  for (const part of parts) {
    joined.set(part, at);
    at += part.byteLength;
  }
  return new TextDecoder().decode(joined);
}

async function readJson(request: Request): Promise<Record<string, unknown> | null> {
  const text = await readBodyText(request, MAX_JSON);
  if (text === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : null;
}

function str(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

// --- auth -----------------------------------------------------------------

interface Device {
  id: string;
  name: string;
  email: string;
  role: Role;
  /** For a device-minted reader: the device whose sessions it may read.
   *  NULL on plain devices and on the admin-minted global reader. */
  scoped_device_id: string | null;
}

/** The ownership columns every authorization decision is made from. */
interface SessionOwner {
  device_id: string | null;
  author: string;
}

function bearer(request: Request): string | null {
  const header = request.headers.get("authorization");
  if (!header) return null;
  const match = /^Bearer\s+(\S+)$/i.exec(header.trim());
  return match ? match[1] : null;
}

/** Resolves a bearer token to a device row. The token itself is hashed before
 *  it touches the database or any log line; the plaintext never leaves here. */
async function authenticate(request: Request, env: Env): Promise<Device | null> {
  const token = bearer(request);
  if (!token) return null;
  const digest = await sha256Hex(token);
  const row = await env.DB.prepare(
    "SELECT id, name, email, role, scoped_device_id FROM devices WHERE token_sha256 = ?1 AND revoked_at IS NULL",
  )
    .bind(digest)
    .first<{
      id: string;
      name: string;
      email: string;
      role: string | null;
      scoped_device_id: string | null;
    }>();
  if (!row) return null;
  // An unrecognised role degrades to the least privilege rather than failing
  // open: a row written before the role column existed reads as a device.
  // A scope on a non-reader row would be meaningless, so it is dropped rather
  // than left where a later check might misread it.
  const role: Role = row.role === "reader" ? "reader" : "device";
  return { ...row, role, scoped_device_id: role === "reader" ? row.scoped_device_id ?? null : null };
}

/** The device whose sessions this token may read, or null for "all of them".
 *  A plain device reads itself; a device-minted reader reads its minter; the
 *  admin-minted global reader (scoped_device_id NULL) reads the whole team. */
function readScope(device: Device): string | null {
  if (device.role !== "reader") return device.id;
  return device.scoped_device_id;
}

/** True when `device` is allowed to see this session at all. */
function canRead(device: Device, owner: SessionOwner): boolean {
  const scope = readScope(device);
  return scope === null || owner.device_id === scope;
}

/** A device-minted reader may only read. The admin-minted global reader
 *  (scope NULL) keeps the old contract -- it reads everything and may still
 *  publish sessions of its own -- but a token handed to an operator to *pull*
 *  one machine's sessions must never be able to grow that machine's history. */
function readOnlyDenied(device: Device): Response | null {
  return device.role === "reader" && device.scoped_device_id !== null
    ? fail(403, "reader tokens are read-only")
    : null;
}

/** The response to send when `device` may not modify this session, or null when
 *  it may. A row with no owner predates the ownership migration: it is frozen
 *  rather than adopted, because adoption is exactly the takeover this guards. */
function writeDenied(device: Device, owner: SessionOwner): Response | null {
  if (owner.device_id === device.id) return null;
  if (owner.device_id === null) {
    return fail(
      409,
      "session has no owner: it predates the ownership migration, assign sessions.device_id first",
    );
  }
  return fail(403, "session belongs to another device");
}

// --- POST /v1/device ------------------------------------------------------

interface AdminFailureRow {
  failures: number;
  locked_until_ms: number | null;
}

/** Records one failed admin attempt and reports the resulting lockout.
 *
 *  Counting happens in a single upsert so two simultaneous guesses cannot both
 *  read "0 failures" and both write "1". The window resets by comparing the
 *  stored window start against the cutoff inside the statement, which keeps the
 *  whole decision in one round trip. */
async function recordAdminFailure(env: Env, ip: string, now: number): Promise<number | null> {
  const cutoff = now - ADMIN_WINDOW_MS;
  const row = await env.DB.prepare(
    `INSERT INTO admin_failures (ip, failures, window_start_ms, locked_until_ms)
     VALUES (?1, 1, ?2, NULL)
     ON CONFLICT(ip) DO UPDATE SET
       failures = CASE WHEN admin_failures.window_start_ms < ?3 THEN 1
                       ELSE admin_failures.failures + 1 END,
       window_start_ms = CASE WHEN admin_failures.window_start_ms < ?3 THEN ?2
                              ELSE admin_failures.window_start_ms END,
       locked_until_ms = CASE
         WHEN (CASE WHEN admin_failures.window_start_ms < ?3 THEN 1
                    ELSE admin_failures.failures + 1 END) >= ?4 THEN ?5
         ELSE admin_failures.locked_until_ms END
     RETURNING failures, locked_until_ms`,
  )
    .bind(ip, now, cutoff, ADMIN_FAILURE_LIMIT, now + ADMIN_LOCKOUT_MS)
    .first<AdminFailureRow>();
  return row?.locked_until_ms ?? null;
}

/** Seconds a locked-out IP must wait, or 0 when it is not locked out. */
async function adminLockoutSeconds(env: Env, ip: string, now: number): Promise<number> {
  const row = await env.DB.prepare("SELECT locked_until_ms FROM admin_failures WHERE ip = ?1")
    .bind(ip)
    .first<{ locked_until_ms: number | null }>();
  const until = row?.locked_until_ms ?? 0;
  return until > now ? Math.ceil((until - now) / 1000) : 0;
}

async function createDevice(request: Request, env: Env): Promise<Response> {
  const admin = env.ADMIN_TOKEN;
  if (!admin) return fail(503, "device minting disabled: ADMIN_TOKEN is not set");

  const ip = clientIp(request);
  const now = Date.now();
  const waitFor = await adminLockoutSeconds(env, ip, now);
  if (waitFor > 0) {
    console.warn(`POST /v1/device locked out ip=${ip} retry_after=${waitFor}s`);
    return fail(429, "too many failed admin attempts", { "retry-after": String(waitFor) });
  }

  const supplied = bearer(request);
  // Hash both sides first: equal-length digests make the compare constant time
  // regardless of how long the guessed token was.
  const [suppliedDigest, adminDigest] = await Promise.all([
    sha256Hex(supplied ?? ""),
    sha256Hex(admin),
  ]);
  if (!supplied || !constantTimeEqual(suppliedDigest, adminDigest)) {
    const lockedUntil = await recordAdminFailure(env, ip, now);
    // Never the token, not even its digest -- a digest of a nearly-correct
    // guess is still a guess worth grinding offline. IP and count only.
    console.warn(
      `POST /v1/device auth failure ip=${ip} reason=${supplied ? "bad-token" : "no-token"}` +
        (lockedUntil && lockedUntil > now ? " locked=yes" : ""),
    );
    return supplied ? fail(403, "forbidden") : fail(401, "missing bearer token");
  }

  const body = await readJson(request);
  if (!body) return fail(400, "expected a JSON object body");
  const name = str(body, "name");
  const email = str(body, "email");
  if (!name || !email) return fail(400, "name and email are required");
  const role = str(body, "role") ?? "device";
  if (!ROLES.includes(role)) return fail(400, "role must be 'device' or 'reader'");

  const secret = hex(crypto.getRandomValues(new Uint8Array(32)).buffer);
  const token = `ezu_${secret}`;
  const id = crypto.randomUUID();
  // RETURNING makes the mint atomic with the write: the plaintext is only
  // handed out once a row provably exists. A D1 failure throws instead, and the
  // handler turns it into a sanitized 500 -- never a token with no row.
  const created = await env.DB.prepare(
    `INSERT INTO devices (id, name, email, role, token_sha256, created_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6)
     RETURNING id`,
  )
    .bind(id, name, email, role, await sha256Hex(token), nowIso())
    .first<{ id: string }>();
  if (!created) return fail(500, "device was not created");

  // A correct admin token clears the guessing counter for that IP.
  await env.DB.prepare("DELETE FROM admin_failures WHERE ip = ?1").bind(ip).run();

  // The only moment the plaintext exists outside the client.
  return json({ token, id: created.id, role }, 201);
}

// --- /v1/token — dev-minted readers ---------------------------------------

/** A device mints a reader scoped to itself: the operator it hands the token
 *  to can pull that device's sessions and nothing else. Readers cannot mint
 *  (no recursion), and only the minting device can list or revoke its readers. */
async function mintReader(request: Request, env: Env, device: Device): Promise<Response> {
  if (device.role !== "device") return fail(403, "only a device token can mint readers");

  const body = await readJson(request);
  if (!body) return fail(400, "expected a JSON object body");
  const name = str(body, "name");
  if (!name) return fail(400, "name is required — say who this token is for");

  const secret = hex(crypto.getRandomValues(new Uint8Array(32)).buffer);
  const token = `ezr_${secret}`;
  const id = crypto.randomUUID();
  // Same atomic mint-with-RETURNING discipline as createDevice: the plaintext
  // leaves this function only if the row provably exists.
  const created = await env.DB.prepare(
    `INSERT INTO devices (id, name, email, role, scoped_device_id, token_sha256, created_at)
     VALUES (?1, ?2, ?3, 'reader', ?4, ?5, ?6)
     RETURNING id`,
  )
    .bind(id, name, device.email, device.id, await sha256Hex(token), nowIso())
    .first<{ id: string }>();
  if (!created) return fail(500, "token was not created");

  return json(
    {
      token,
      id: created.id,
      grants: `read-only access to sessions published by device ${device.name}`,
    },
    201,
  );
}

async function listReaders(env: Env, device: Device): Promise<Response> {
  if (device.role !== "device") return fail(403, "only a device token can list its readers");
  const rows = await env.DB.prepare(
    `SELECT id, name, created_at, revoked_at FROM devices
     WHERE scoped_device_id = ?1 AND role = 'reader'
     ORDER BY created_at`,
  )
    .bind(device.id)
    .all<{ id: string; name: string; created_at: string; revoked_at: string | null }>();
  // id/name/dates only — the token value was never stored, so it cannot leak here.
  return json({ tokens: rows.results ?? [] });
}

async function revokeReader(env: Env, url: URL, device: Device): Promise<Response> {
  if (device.role !== "device") return fail(403, "only a device token can revoke its readers");
  const id = url.searchParams.get("id") ?? "";
  if (!id) return fail(400, "id is required");
  // The scope predicate is the ownership check: a device can only ever revoke
  // readers it minted, so a guessed id belonging to someone else is a no-op.
  const done = await env.DB.prepare(
    `UPDATE devices SET revoked_at = ?1
     WHERE id = ?2 AND scoped_device_id = ?3 AND role = 'reader' AND revoked_at IS NULL
     RETURNING id`,
  )
    .bind(nowIso(), id, device.id)
    .first<{ id: string }>();
  if (!done) return fail(404, "no such active reader token minted by this device");
  return json({ ok: true, revoked: id });
}

// --- POST /v1/session -----------------------------------------------------

async function putSession(request: Request, env: Env, device: Device): Promise<Response> {
  const body = await readJson(request);
  if (!body) return fail(400, "expected a JSON object body");

  const session = str(body, "session");
  const author = str(body, "author");
  if (!session || !SAFE_ID.test(session)) return fail(400, "invalid session id");
  if (!author || !SAFE_ID.test(author)) return fail(400, "invalid author");

  const level = str(body, "level") ?? "raw";
  if (level !== "raw") return fail(400, "only level=raw is supported");

  const updated = nowIso();
  const updatedMs = Date.now();
  // Re-registering is how a client refreshes the title or last_ts as a session
  // grows, so this upserts. first_ts is kept at its earliest value and
  // deleted_at is cleared: re-registering after a delete is an explicit
  // decision to share again, and the old chunks are already gone.
  //
  // The WHERE on the conflict branch is the ownership check, and it is part of
  // the same statement rather than a lookup before it: a check-then-write pair
  // can be raced, a guarded upsert cannot. No rows back means either another
  // device owns the id or nobody does, and only then is a second query needed
  // to say which.
  const written = await env.DB.prepare(
    `INSERT INTO sessions
       (session, device_id, author, project, branch, cwd, title,
        first_ts, last_ts, size, updated_at, updated_ms, deleted_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 0, ?10, ?11, NULL)
     ON CONFLICT(session) DO UPDATE SET
       author     = excluded.author,
       project    = excluded.project,
       branch     = excluded.branch,
       cwd        = excluded.cwd,
       title      = excluded.title,
       first_ts   = COALESCE(MIN(sessions.first_ts, excluded.first_ts), excluded.first_ts, sessions.first_ts),
       last_ts    = COALESCE(MAX(sessions.last_ts, excluded.last_ts), excluded.last_ts, sessions.last_ts),
       updated_at = excluded.updated_at,
       updated_ms = excluded.updated_ms,
       deleted_at = NULL
     WHERE sessions.device_id = ?2
     RETURNING session`,
  )
    .bind(
      session,
      device.id,
      author,
      str(body, "project"),
      str(body, "branch"),
      str(body, "cwd"),
      str(body, "title"),
      str(body, "first_ts"),
      str(body, "last_ts"),
      updated,
      updatedMs,
    )
    .first<{ session: string }>();

  if (!written) {
    const owner = await env.DB.prepare("SELECT device_id, author FROM sessions WHERE session = ?1")
      .bind(session)
      .first<SessionOwner>();
    // The row vanished between the upsert and this lookup; the caller can retry.
    if (!owner) return fail(409, "session changed concurrently, retry");
    return writeDenied(device, owner) ?? fail(409, "session changed concurrently, retry");
  }

  return json({ ok: true });
}

// --- POST /v1/chunk -------------------------------------------------------

interface Hashed {
  stream: ReadableStream<Uint8Array>;
  digest: Promise<ArrayBuffer>;
  size: () => number;
  exceeded: () => boolean;
}

/** Hashes the body as it flows to R2 rather than buffering it first.
 *  A TransformStream is used instead of tee() so the digest side inherits R2's
 *  backpressure and cannot accumulate an unread copy of the chunk. */
function hashingPassThrough(source: ReadableStream<Uint8Array>, limit: number): Hashed {
  const hasher = new crypto.DigestStream("SHA-256");
  const writer = hasher.getWriter();
  let size = 0;
  let exceeded = false;

  const stream = source.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      async transform(part, controller) {
        size += part.byteLength;
        if (size > limit) {
          // Recorded as a flag as well as thrown: by the time the rejection
          // surfaces out of R2 it may be wrapped, and the caller has to tell an
          // oversized body (the client's fault) from a storage outage (ours).
          exceeded = true;
          // Abort rather than close: the digest is meaningless now, and the
          // rejection propagates out through the R2 put below.
          await writer.abort(new Error("body exceeds declared length"));
          throw new Error("body exceeds declared length");
        }
        await writer.write(part);
        controller.enqueue(part);
      },
      async flush() {
        await writer.close();
      },
    }),
  );

  // On the abort path nobody awaits the digest, and an unobserved rejection
  // would tear down the isolate mid-request.
  const digest = hasher.digest;
  digest.catch(() => {});
  return { stream, digest, size: () => size, exceeded: () => exceeded };
}

/** Best-effort cleanup. The caller is already returning an error, and a failed
 *  cleanup must not replace that error with a confusing 500. */
async function forget(env: Env, key: string): Promise<void> {
  try {
    await env.BUCKET.delete(key);
  } catch (error) {
    console.error(`orphaned R2 object ${key}: ${(error as Error)?.message ?? "unknown"}`);
  }
}

async function putChunk(request: Request, env: Env, url: URL, device: Device): Promise<Response> {
  const session = url.searchParams.get("session");
  const offset = positiveInt(url.searchParams.get("offset"), 0);
  const length = positiveInt(url.searchParams.get("length"), 1);
  const expected = url.searchParams.get("sha256");

  if (!session || !SAFE_ID.test(session)) return fail(400, "invalid session id");
  if (offset === null) return fail(400, "invalid offset");
  if (length === null) return fail(400, "invalid length");
  if (!expected || !HEX64.test(expected)) return fail(400, "invalid sha256");
  if (length > MAX_CHUNK) return fail(413, `chunk exceeds ${MAX_CHUNK} bytes`);

  const declared = request.headers.get("content-length");
  if (declared !== null) {
    const size = Number(declared);
    if (size > MAX_CHUNK) return fail(413, `chunk exceeds ${MAX_CHUNK} bytes`);
    if (size !== length) return fail(400, "content-length does not match length");
  }

  const row = await env.DB.prepare(
    "SELECT author, device_id FROM sessions WHERE session = ?1 AND deleted_at IS NULL",
  )
    .bind(session)
    .first<SessionOwner>();
  if (!row) return fail(404, "unknown session: register it with POST /v1/session first");
  const denied = writeDenied(device, row);
  if (denied) return denied;

  // Fast path only. The same range with the same content is already durable, so
  // say so without touching R2. The authoritative decision is the upsert below;
  // this lookup exists to keep a routine retry from re-uploading megabytes.
  const existing = await env.DB.prepare(
    'SELECT sha256, length, key FROM chunks WHERE session = ?1 AND "offset" = ?2',
  )
    .bind(session, offset)
    .first<{ sha256: string; length: number; key: string }>();
  if (existing) {
    if (existing.sha256 === expected && existing.length === length) {
      return json({ ok: true, key: existing.key });
    }
    return fail(409, "offset already published with different content");
  }

  if (!request.body) return fail(400, "missing body");

  const key = chunkKey(row.author, session, offset, length);
  const hashed = hashingPassThrough(request.body, length);
  try {
    // R2 refuses a stream of unknown length, and piping through the digest
    // transform erased the one the request carried. The declared length is
    // re-imposed here; FixedLengthStream also makes a short body an error
    // instead of a silently truncated object.
    const sized = hashed.stream.pipeThrough(new FixedLengthStream(length));
    await env.BUCKET.put(key, sized);
  } catch (error) {
    // A put that threw should have left nothing behind, but a partial object
    // here would be one no chunks row can ever point at. Sweep it either way.
    await forget(env, key);
    if (hashed.exceeded()) return fail(413, "body exceeded the declared length");
    if (hashed.size() < length) {
      // The client's fault, not storage's: retrying the same short body can
      // never succeed, so it must not look like a 5xx.
      return fail(400, "body shorter than the declared length");
    }
    // Everything else is a storage failure on this side. It has to be 5xx: the
    // client only retries 5xx and 429, and a 4xx would strand a publish that
    // would have succeeded a second later.
    console.error(`R2 put ${key} failed: ${(error as Error)?.message ?? "unknown"}`);
    return fail(502, "object storage write failed, retry");
  }

  const actual = hex(await hashed.digest);
  if (hashed.size() !== length || !constantTimeEqual(actual, expected)) {
    // The object exists but no chunks row points at it, so it is unreachable by
    // any reader; delete it anyway so a failed publish costs nothing.
    await forget(env, key);
    return fail(400, "sha256 does not match the received body");
  }

  const written = nowIso();
  const writtenMs = Date.now();
  // Claiming the offset is one guarded upsert, not a check followed by an
  // insert. Two clients posting the same range at once both pass any prior
  // existence check, and the loser used to hit the PRIMARY KEY and get a 500
  // for what the protocol documents as an idempotent retry. Here the loser's
  // statement takes the conflict branch: identical digest and length means the
  // no-op update succeeds and the existing row comes back, anything else fails
  // the WHERE, returns nothing, and is answered with 409.
  const claimed = await env.DB.prepare(
    `INSERT INTO chunks (session, "offset", length, sha256, key, created_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6)
     ON CONFLICT(session, "offset") DO UPDATE SET created_at = chunks.created_at
     WHERE chunks.sha256 = ?4 AND chunks.length = ?3
     RETURNING key`,
  )
    .bind(session, offset, length, expected, key, written)
    .first<{ key: string }>();

  if (!claimed) {
    // Same offset, different bytes: the two clients disagree about the file's
    // history. Refuse rather than fork it. The object just written is only
    // dropped when the winning row points somewhere else -- when both requests
    // derived the same key, that object is now the winner's and deleting it
    // would destroy a published chunk to punish the loser.
    const winner = await env.DB.prepare(
      'SELECT key FROM chunks WHERE session = ?1 AND "offset" = ?2',
    )
      .bind(session, offset)
      .first<{ key: string }>();
    if (winner && winner.key !== key) await forget(env, key);
    return fail(409, "offset already published with different content");
  }
  // The winner's row may name a different key than ours if the session was
  // re-registered under a new author between the two publishes. Its bytes are
  // the ones readers get; ours are unreachable, so drop them.
  if (claimed.key !== key) await forget(env, key);

  // size is the published prefix length, so a chunk that lands out of order or
  // after a resend cannot shrink or double-count it.
  await env.DB.prepare(
    "UPDATE sessions SET size = MAX(size, ?2), updated_at = ?3, updated_ms = ?4 WHERE session = ?1",
  )
    .bind(session, offset + length, written, writtenMs)
    .run();

  return json({ ok: true, key: claimed.key });
}

// --- DELETE /v1/session ---------------------------------------------------

async function deleteSession(env: Env, url: URL, device: Device): Promise<Response> {
  const session = url.searchParams.get("session");
  if (!session || !SAFE_ID.test(session)) return fail(400, "invalid session id");

  const found = await env.DB.prepare(
    "SELECT device_id, author FROM sessions WHERE session = ?1",
  )
    .bind(session)
    .first<SessionOwner>();
  if (!found) return fail(404, "unknown session");
  const denied = writeDenied(device, found);
  if (denied) return denied;

  const listed = await env.DB.prepare("SELECT key FROM chunks WHERE session = ?1")
    .bind(session)
    .all<{ key: string }>();
  const keys = (listed.results ?? []).map((r) => r.key);
  for (let i = 0; i < keys.length; i += DELETE_BATCH) {
    await env.BUCKET.delete(keys.slice(i, i + DELETE_BATCH));
  }

  const when = nowIso();
  const whenMs = Date.now();
  // Tombstone instead of dropping the row: /v1/sessions consumers poll by
  // updated_at, and a vanished row would simply never be reported as gone.
  // The ownership guard is repeated in the UPDATE so a concurrent re-register
  // by another owner cannot be tombstoned by this request.
  await env.DB.batch([
    env.DB.prepare("DELETE FROM chunks WHERE session = ?1").bind(session),
    env.DB.prepare(
      `UPDATE sessions SET deleted_at = ?2, updated_at = ?2, updated_ms = ?3, size = 0
        WHERE session = ?1 AND device_id = ?4`,
    ).bind(session, when, whenMs, device.id),
  ]);

  return json({ ok: true, chunks_deleted: keys.length });
}

// --- GET /v1/sessions -----------------------------------------------------

async function listSessions(env: Env, url: URL, device: Device): Promise<Response> {
  const since = url.searchParams.get("since") ?? "";
  let sinceMs = 0;
  if (since) {
    // The cursor is whatever the client last saw, re-serialized by its own
    // formatter: Python's isoformat writes microseconds and a +00:00 offset,
    // JS toISOString writes milliseconds and a Z. Those two do not order
    // lexically against each other, so the comparison is done on epoch
    // milliseconds -- a number both formatters agree on -- and never on text.
    const parsed = Date.parse(since);
    if (Number.isNaN(parsed)) return fail(400, "invalid since timestamp");
    sinceMs = parsed;
  }

  // The cursor is inclusive. Millisecond ties are possible (a client
  // registering a batch of sessions in one loop), and re-listing a session the
  // caller already has costs one empty chunk manifest, while skipping one loses
  // it forever.
  const columns = `session, author, project, branch, cwd, title,
                   first_ts, last_ts, size, updated_at`;
  const order = "ORDER BY updated_ms ASC, session ASC LIMIT ?2";
  const listed =
    device.role === "reader"
      ? await env.DB.prepare(
          `SELECT ${columns} FROM sessions
            WHERE deleted_at IS NULL AND updated_ms >= ?1 ${order}`,
        )
          .bind(sinceMs, SESSION_PAGE)
          .all()
      : await env.DB.prepare(
          `SELECT ${columns} FROM sessions
            WHERE deleted_at IS NULL AND updated_ms >= ?1 AND device_id = ?3 ${order}`,
        )
          .bind(sinceMs, SESSION_PAGE, device.id)
          .all();

  return json({ sessions: listed.results ?? [] });
}

// --- GET /v1/chunks -------------------------------------------------------

async function listChunks(env: Env, url: URL, device: Device): Promise<Response> {
  const session = url.searchParams.get("session");
  if (!session || !SAFE_ID.test(session)) return fail(400, "invalid session id");

  const live = await env.DB.prepare(
    "SELECT device_id, author FROM sessions WHERE session = ?1 AND deleted_at IS NULL",
  )
    .bind(session)
    .first<SessionOwner>();
  // A session the caller may not read is reported as unknown rather than
  // forbidden: the id alone would otherwise confirm that a colleague is
  // sharing that session.
  if (!live || !canRead(device, live)) return fail(404, "unknown session");

  const listed = await env.DB.prepare(
    'SELECT "offset" AS "offset", length, sha256, key FROM chunks WHERE session = ?1 ORDER BY "offset" ASC',
  )
    .bind(session)
    .all();

  return json({ chunks: listed.results ?? [] });
}

// --- GET /v1/blob ---------------------------------------------------------

async function getBlob(env: Env, url: URL, device: Device): Promise<Response> {
  const key = url.searchParams.get("key");
  if (!key) return fail(400, "missing key");

  // The key is never trusted as a direct R2 address: it has to still be claimed
  // by a chunks row whose session is alive, so a delete revokes reads even if a
  // reader cached the key. The owner comes back with it so the same read rule
  // that governs the manifest governs the bytes.
  const owned = await env.DB.prepare(
    `SELECT s.device_id AS device_id, s.author AS author FROM chunks c
       JOIN sessions s ON s.session = c.session
      WHERE c.key = ?1 AND s.deleted_at IS NULL`,
  )
    .bind(key)
    .first<SessionOwner>();
  if (!owned || !canRead(device, owned)) return fail(404, "unknown key");

  const object = await env.BUCKET.get(key);
  if (!object) return fail(404, "blob missing");

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("content-type", "application/x-ndjson");
  headers.set("content-length", String(object.size));
  headers.set("etag", object.httpEtag);
  return new Response(object.body, { headers });
}

// --- entry point ----------------------------------------------------------

function unauthorized(): Response {
  return new Response(JSON.stringify({ error: "unauthorized" }), {
    status: 401,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "www-authenticate": 'Bearer realm="ezupdate"',
    },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    // Everything, authentication and device minting included, runs inside this
    // try. A D1 outage during either used to escape as an unhandled rejection.
    try {
      if (path === "/" && method === "GET") {
        // The developer log viewer. The page itself is a static shell and
        // carries no data; everything it shows comes through the same
        // token-gated /v1 routes as the CLI, so serving it unauthenticated
        // exposes nothing.
        return new Response(VIEWER_HTML, {
          headers: {
            "content-type": "text/html; charset=utf-8",
            "content-security-policy":
              "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
          },
        });
      }
      if (path === "/v1/device") {
        // The only route not gated by a device token: it is what mints them.
        return method === "POST"
          ? await createDevice(request, env)
          : fail(405, "method not allowed");
      }

      const device = await authenticate(request, env);
      if (!device) return unauthorized();

      if (path === "/v1/token") {
        if (method === "POST") return await mintReader(request, env, device);
        if (method === "DELETE") return await revokeReader(env, url, device);
        return fail(405, "method not allowed");
      }
      if (path === "/v1/tokens") {
        if (method !== "GET") return fail(405, "method not allowed");
        return await listReaders(env, device);
      }
      if (path === "/v1/session" || path === "/v1/chunk") {
        // Readers read. Ownership checks inside the handlers cover *whose*
        // session a device may touch, but they cannot cover a reader
        // registering a brand-new session and becoming its owner -- so the
        // role gate lives here, on every write route, unconditionally.
        if (device.role !== "device") return fail(403, "read-only token");
      }
      if (path === "/v1/session") {
        if (method === "POST") return await putSession(request, env, device);
        if (method === "DELETE") return await deleteSession(env, url, device);
        return fail(405, "method not allowed");
      }
      if (path === "/v1/chunk") {
        if (method !== "POST") return fail(405, "method not allowed");
        return await putChunk(request, env, url, device);
      }
      if (path === "/v1/sessions") {
        if (method !== "GET") return fail(405, "method not allowed");
        return await listSessions(env, url, device);
      }
      if (path === "/v1/chunks") {
        if (method !== "GET") return fail(405, "method not allowed");
        return await listChunks(env, url, device);
      }
      if (path === "/v1/blob") {
        if (method !== "GET") return fail(405, "method not allowed");
        return await getBlob(env, url, device);
      }
      return fail(404, "not found");
    } catch (error) {
      // Message only, never the request: a stray token in a log is the one
      // failure this service cannot take back.
      console.error(`${method} ${path} failed: ${(error as Error)?.message ?? "unknown"}`);
      return fail(500, "internal error");
    }
  },
} satisfies ExportedHandler<Env>;
