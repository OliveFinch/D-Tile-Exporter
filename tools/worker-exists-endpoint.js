/**
 * A batch tile-existence endpoint for the Magic Parks Explorer Worker.
 *
 * Drop-in addition to the Worker that already proxies TDR tiles. It exists to
 * fix two things that make boundary tracing through the tile proxy painful,
 * both of which are consequences of that proxy doing its actual job well.
 *
 * ## 1. The proxy throws away the answer we need
 *
 * Serving imagery, upstream 403 and 404 both sensibly become "204, nothing to
 * draw" — the viewer does not care why a tile is absent. Measuring coverage,
 * that is the whole question. A 404 means the map ends here. A 403 means the
 * CloudFront signature was rejected and the tile's existence is simply unknown.
 * Collapsing them means a walk done while the cookies are stale traces a
 * confident border around an outage.
 *
 * This endpoint keeps them apart: `P` present, `A` absent, `R` refused.
 *
 * ## 2. One HTTP request per tile is the wrong unit
 *
 * A trace of both TDR modes is roughly 15,600 tile probes — through the tile
 * proxy, 15,600 Worker requests, competing with live viewer traffic. Batched it
 * measures at about 5,000, near enough a third.
 *
 * Not the 48x the batch size implies, and worth being clear about why: the seed
 * lattice fills a batch, but the border walk dominates the cost and each step
 * asks about one ring of eight, most of which are already cached. One step is
 * one call whatever a batch could hold. The saving is real but secondary — the
 * reason to deploy this is the paragraph above.
 *
 * Upstream is asked with `Range: bytes=0-0`, so this costs a header round trip
 * per tile rather than a JPEG. Existence is all we are asking about.
 *
 * ## Mounting it
 *
 * In the Worker's existing fetch handler, before the tile-proxy route:
 *
 *     import { handleExists, EXISTS_PATH } from "./exists.js";
 *
 *     if (url.pathname.endsWith(EXISTS_PATH)) {
 *       return handleExists(request, env);
 *     }
 *
 * `env` needs the same CloudFront values the tile proxy already uses:
 * `CF_POLICY`, `CF_SIGNATURE`, `CF_KEY_PAIR_ID`, and optionally `TDR_ORIGIN_BASE`
 * and `TDR_USER_AGENT`. Nothing new to configure if the proxy works.
 *
 * Keep it behind whatever guard the rest of the Worker uses. It is far cheaper
 * per tile than the proxy, which also makes it a cheaper thing to abuse.
 */

export const EXISTS_PATH = "/exists";

/** Cloudflare's free tier allows 50 subrequests per invocation. Leave headroom. */
export const MAX_TILES_PER_CALL = 48;

const DEFAULT_ORIGIN_BASE =
  "https://contents-portal.tokyodisneyresort.jp/limited/map-image/{serverId}/{mode}/";
const DEFAULT_REFERER = "https://www.tokyodisneyresort.jp/";
const MODES = new Set(["daytime", "nighttime"]);

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
    },
  });

/**
 * POST { sid, mode, z, tiles: [[x, y], ...] }
 *   -> { z, verdicts: "PPAAR...", present, absent, refused }
 *
 * One character per requested tile, in the order asked. Same length as `tiles`,
 * always — a caller lining verdicts up with coordinates by index must be able
 * to rely on that even when everything failed.
 */
export async function handleExists(request, env) {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type",
      },
    });
  }
  if (request.method !== "POST") {
    return json({ error: "POST a JSON body" }, 405);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "body is not valid JSON" }, 400);
  }

  const { sid, mode, z, tiles } = body || {};
  if (!sid || !/^[0-9]+$/.test(String(sid))) {
    return json({ error: "sid must be the digits-only server id" }, 400);
  }
  if (!MODES.has(mode)) {
    return json({ error: `mode must be one of ${[...MODES].join(", ")}` }, 400);
  }
  if (!Number.isInteger(z) || z < 0 || z > 24) {
    return json({ error: "z must be a plausible zoom level" }, 400);
  }
  if (!Array.isArray(tiles) || tiles.length === 0) {
    return json({ error: "tiles must be a non-empty array of [x, y]" }, 400);
  }
  if (tiles.length > MAX_TILES_PER_CALL) {
    // Refusing beats silently truncating: a short verdict string would be
    // lined up against the wrong coordinates by every caller.
    return json(
      { error: `at most ${MAX_TILES_PER_CALL} tiles per call, got ${tiles.length}` },
      413,
    );
  }
  for (const tile of tiles) {
    if (!Array.isArray(tile) || tile.length !== 2 || !tile.every(Number.isInteger)) {
      return json({ error: "each tile must be [x, y] integers" }, 400);
    }
  }

  const cookies = [
    ["CloudFront-Policy", env.CF_POLICY],
    ["CloudFront-Signature", env.CF_SIGNATURE],
    ["CloudFront-Key-Pair-Id", env.CF_KEY_PAIR_ID],
  ];
  if (cookies.some(([, value]) => !value)) {
    // Not "everything is absent". Saying so plainly here is the entire point.
    return json({ error: "worker is missing its CloudFront credentials" }, 503);
  }

  const base = (env.TDR_ORIGIN_BASE || DEFAULT_ORIGIN_BASE)
    .replace("{serverId}", String(sid))
    .replace("{mode}", mode)
    .replace(/\/?$/, "/");
  const headers = {
    Cookie: cookies.map(([name, value]) => `${name}=${value}`).join("; "),
    "User-Agent": env.TDR_USER_AGENT || "MagicParksExplorer/exists",
    Referer: env.TDR_REFERER || DEFAULT_REFERER,
    Accept: "image/*,*/*;q=0.8",
    // Existence, not imagery. Servers that ignore Range send the whole tile,
    // which still answers the question.
    Range: "bytes=0-0",
  };

  const verdicts = await Promise.all(
    tiles.map(([x, y]) => probe(`${base}z${z}/${x}_${y}.jpg`, headers)),
  );

  const line = verdicts.join("");
  return json({
    z,
    verdicts: line,
    present: count(line, "P"),
    absent: count(line, "A"),
    refused: count(line, "R"),
  });
}

const count = (text, char) => text.split(char).length - 1;

async function probe(url, headers) {
  let response;
  try {
    response = await fetch(url, { headers, redirect: "follow" });
  } catch {
    return "R";                       // never heard back; says nothing about the tile
  }
  const status = response.status;
  // Drain, so the connection is not left hanging on a body we do not want.
  try { await response.arrayBuffer(); } catch { /* ignore */ }

  if (status === 200 || status === 206) return "P";
  if (status === 404) return "A";     // the origin says there is nothing, and means it
  if (status === 403 || status === 401) return "R";   // signature rejected
  if (status === 204) return "A";     // shouldn't reach here from the origin, but harmless
  if (status === 429 || status >= 500) return "R";
  return "R";                         // anything unexpected is not evidence of absence
}

export default { fetch: handleExists };
