#!/usr/bin/env node
/**
 * skewlens scanner.
 *
 * Reads a JSONL file in line-aligned byte ranges, profiles every column in
 * parallel worker threads, and writes one JSON profile to stdout.
 *
 * Memory stays bounded because every worker ships compact sketches rather than
 * raw values:
 *   - HyperLogLog (2^14 registers, ~0.8% standard error) for cardinality,
 *     with an exact set kept only while a column stays under 2k distinct values;
 *   - a Count-Min sketch (4096 x 4) plus Space-Saving candidates for frequent
 *     values, merged by element-wise addition;
 *   - Chan's parallel formula for numeric moments.
 *
 * Usage: node --experimental-strip-types scan.ts --input data.jsonl [--workers N]
 */

import { closeSync, openSync, readSync, statSync } from "node:fs";
import { StringDecoder } from "node:string_decoder";
import { Worker, isMainThread, parentPort, workerData } from "node:worker_threads";

const HLL_P = 14;
const HLL_M = 1 << HLL_P;
const EXACT_LIMIT = 2000;
const CMS_WIDTH = 4096;
const CMS_DEPTH = 4;
const CANDIDATES = 32;
const READ_CHUNK = 1 << 20;

type Numeric = { n: number; mean: number; m2: number; min: number; max: number };

type RawColumn = {
  count: number;
  nulls: number;
  types: Record<string, number>;
  hll: Uint8Array;
  cms: Uint32Array;
  candidates: Map<string, number>;
  exact: Set<string> | null;
  numeric: Numeric;
};

type Partial = { rows: number; columns: Record<string, RawColumn>; rss_mb: number; heap_mb: number };

const EMPTY_NUMERIC: Numeric = { n: 0, mean: 0, m2: 0, min: Infinity, max: -Infinity };

function murmur3(input: string, seed: number): number {
  let h = seed >>> 0;
  const len = input.length;
  let i = 0;
  while (i + 4 <= len) {
    let k =
      (input.charCodeAt(i) & 0xff) |
      ((input.charCodeAt(i + 1) & 0xff) << 8) |
      ((input.charCodeAt(i + 2) & 0xff) << 16) |
      ((input.charCodeAt(i + 3) & 0xff) << 24);
    k = Math.imul(k, 0xcc9e2d51);
    k = (k << 15) | (k >>> 17);
    k = Math.imul(k, 0x1b873593);
    h ^= k;
    h = (h << 13) | (h >>> 19);
    h = (Math.imul(h, 5) + 0xe6546b64) | 0;
    i += 4;
  }
  let tail = 0;
  switch (len & 3) {
    case 3:
      tail ^= (input.charCodeAt(i + 2) & 0xff) << 16;
    /* falls through */
    case 2:
      tail ^= (input.charCodeAt(i + 1) & 0xff) << 8;
    /* falls through */
    case 1:
      tail ^= input.charCodeAt(i) & 0xff;
      tail = Math.imul(tail, 0xcc9e2d51);
      tail = (tail << 15) | (tail >>> 17);
      tail = Math.imul(tail, 0x1b873593);
      h ^= tail;
  }
  h ^= len;
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return h >>> 0;
}

function newColumn(): RawColumn {
  return {
    count: 0,
    nulls: 0,
    types: {},
    hll: new Uint8Array(HLL_M),
    cms: new Uint32Array(CMS_WIDTH * CMS_DEPTH),
    candidates: new Map<string, number>(),
    exact: new Set<string>(),
    numeric: { ...EMPTY_NUMERIC },
  };
}

function profileValue(column: RawColumn, value: unknown): void {
  column.count += 1;
  if (value === null || value === undefined) {
    column.nulls += 1;
    column.types.null = (column.types.null ?? 0) + 1;
    return;
  }
  const kind = Array.isArray(value) ? "array" : typeof value;
  column.types[kind] = (column.types[kind] ?? 0) + 1;
  if (kind === "object" || kind === "array") return;

  if (kind === "number") {
    const number = value as number;
    if (Number.isFinite(number)) {
      const stats = column.numeric;
      stats.n += 1;
      const delta = number - stats.mean;
      stats.mean += delta / stats.n;
      stats.m2 += delta * (number - stats.mean);
      if (number < stats.min) stats.min = number;
      if (number > stats.max) stats.max = number;
    }
  }

  const text = String(value);
  const h1 = murmur3(text, 0x9747b28c);
  const h2 = murmur3(text, 0x5bd1e995);
  const bucket = h1 >>> (32 - HLL_P);
  const rank = Math.clz32(h2 | 1) + 1;
  if (rank > column.hll[bucket]) column.hll[bucket] = rank;

  if (column.exact !== null) {
    column.exact.add(text);
    if (column.exact.size > EXACT_LIMIT) column.exact = null;
  }

  for (let depth = 0; depth < CMS_DEPTH; depth++) {
    // Kirsch-Mitzenmacher double hashing: each row of the sketch must use a
    // per-key offset. A constant offset would shift one table four times, so a
    // single collision would corrupt every row at once.
    const index = depth * CMS_WIDTH + ((h1 + Math.imul(depth, h2)) >>> 0) % CMS_WIDTH;
    column.cms[index] += 1;
  }

  const current = column.candidates.get(text);
  if (current !== undefined) {
    column.candidates.set(text, current + 1);
  } else if (column.candidates.size < CANDIDATES) {
    column.candidates.set(text, 1);
  } else if ((h1 & 63) === 0) {
    // Admission sampling: without it, a 1M-row high-cardinality column would
    // pay a 32-entry decrement on every unseen value. The Count-Min sketch
    // still holds the real counts, so sampling only decides membership.
    for (const [key, count] of column.candidates) {
      if (count <= 1) column.candidates.delete(key);
      else column.candidates.set(key, count - 1);
    }
    column.candidates.set(text, 1);
  }
}

function hllEstimate(registers: Uint8Array): number {
  const m = registers.length;
  let harmonic = 0;
  let zeros = 0;
  for (let i = 0; i < m; i++) {
    const value = registers[i];
    if (value === 0) zeros += 1;
    harmonic += Math.pow(2, -value);
  }
  const alpha = 0.7213 / (1 + 1.079 / m);
  const raw = (alpha * m * m) / harmonic;
  if (zeros > 0 && raw <= 2.5 * m) {
    return m * Math.log(m / zeros);
  }
  return raw;
}

function cmsQuery(cms: Uint32Array, value: string): number {
  const h1 = murmur3(value, 0x9747b28c);
  const h2 = murmur3(value, 0x5bd1e995);
  let estimate = Infinity;
  for (let depth = 0; depth < CMS_DEPTH; depth++) {
    const index = depth * CMS_WIDTH + ((h1 + Math.imul(depth, h2)) >>> 0) % CMS_WIDTH;
    if (cms[index] < estimate) estimate = cms[index];
  }
  return estimate === Infinity ? 0 : estimate;
}

function scanRange(path: string, start: number, end: number): Partial {
  const columns = new Map<string, RawColumn>();
  const buffer = Buffer.allocUnsafe(READ_CHUNK);
  const decoder = new StringDecoder("utf8");
  const fd = openSync(path, "r");
  let remainder = "";
  let position = start;
  let rows = 0;

  const consume = (line: string): void => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return;
    rows += 1;
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      let column = columns.get(key);
      if (column === undefined) {
        column = newColumn();
        columns.set(key, column);
      }
      profileValue(column, value);
    }
  };

  try {
    while (position < end) {
      const toRead = Math.min(READ_CHUNK, end - position);
      const read = readSync(fd, buffer, 0, toRead, position);
      if (read <= 0) break;
      position += read;
      const text = remainder + decoder.write(buffer.subarray(0, read));
      let lineStart = 0;
      let newline = text.indexOf("\n");
      while (newline >= 0) {
        consume(text.slice(lineStart, newline));
        lineStart = newline + 1;
        newline = text.indexOf("\n", lineStart);
      }
      remainder = text.slice(lineStart);
    }
    remainder += decoder.end();
    if (remainder) consume(remainder);
  } finally {
    closeSync(fd);
  }

  const output: Record<string, RawColumn> = {};
  for (const [name, column] of columns) output[name] = column;
  const usage = process.memoryUsage();
  return {
    rows,
    columns: output,
    rss_mb: usage.rss / 1048576,
    heap_mb: usage.heapUsed / 1048576,
  };
}

function mergeNumeric(a: Numeric, b: Numeric): Numeric {
  if (a.n === 0) return { ...b };
  if (b.n === 0) return { ...a };
  const n = a.n + b.n;
  const delta = b.mean - a.mean;
  return {
    n,
    mean: a.mean + (delta * b.n) / n,
    m2: a.m2 + b.m2 + (delta * delta * a.n * b.n) / n,
    min: Math.min(a.min, b.min),
    max: Math.max(a.max, b.max),
  };
}

function finalize(name: string, column: RawColumn, rows: number) {
  const distinct =
    column.exact !== null ? column.exact.size : Math.round(hllEstimate(column.hll));
  const scored = [...column.candidates.keys()].map((value) => ({
    value,
    count: Math.min(cmsQuery(column.cms, value), column.count),
  }));
  scored.sort((a, b) => b.count - a.count || (a.value < b.value ? -1 : 1));
  const top = scored.slice(0, 5).map((item) => ({
    value: item.value,
    count: item.count,
    share: column.count > 0 ? item.count / column.count : 0,
  }));
  const stats = column.numeric;
  return {
    column: name,
    count: column.count,
    nulls: column.nulls,
    coverage: rows > 0 ? column.count / rows : 0,
    types: column.types,
    distinct,
    distinct_method: column.exact !== null ? "exact" : "hll",
    distinct_ratio: column.count > 0 ? Math.min(1, distinct / column.count) : 0,
    top,
    numeric:
      stats.n > 0
        ? {
            n: stats.n,
            min: stats.min,
            max: stats.max,
            mean: stats.mean,
            std: stats.n > 1 ? Math.sqrt(stats.m2 / (stats.n - 1)) : 0,
          }
        : null,
  };
}

function lineRanges(path: string, size: number, chunks: number): Array<[number, number]> {
  const fd = openSync(path, "r");
  const bounds: number[] = [0];
  const probe = Buffer.allocUnsafe(65536);
  try {
    for (let i = 1; i < chunks; i++) {
      let position = Math.floor((size * i) / chunks);
      let found = -1;
      while (position < size && found < 0) {
        const read = readSync(fd, probe, 0, Math.min(probe.length, size - position), position);
        if (read <= 0) break;
        const index = probe.subarray(0, read).indexOf(10);
        if (index >= 0) found = position + index + 1;
        else position += read;
      }
      if (found > 0 && found < size) bounds.push(found);
    }
  } finally {
    closeSync(fd);
  }
  bounds.push(size);
  const ranges: Array<[number, number]> = [];
  for (let i = 0; i + 1 < bounds.length; i++) {
    if (bounds[i + 1] > bounds[i]) ranges.push([bounds[i], bounds[i + 1]]);
  }
  return ranges;
}

function parseArgs(argv: string[]): Record<string, string> {
  const args: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) continue;
    const key = argv[i].slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      args[key] = next;
      i += 1;
    } else {
      args[key] = "true";
    }
  }
  return args;
}

if (!isMainThread) {
  const data = workerData as { path: string; start: number; end: number };
  parentPort!.postMessage(scanRange(data.path, data.start, data.end));
} else {
  const args = parseArgs(process.argv.slice(2));
  const input = args.input;
  if (!input) {
    console.error("usage: scan.ts --input <file.jsonl> [--workers N]");
    process.exit(2);
  }
  const requested = Math.max(1, Number(args.workers ?? 4));
  const size = statSync(input).size;
  const ranges = lineRanges(input, size, requested);
  const started = process.hrtime.bigint();
  const partials: Partial[] = [];

  const finish = (): void => {
    const elapsed = Number(process.hrtime.bigint() - started) / 1e9;
    const merged = new Map<string, RawColumn>();
    let rows = 0;
    let peak = 0;
    let peakHeap = 0;
    for (const partial of partials) {
      rows += partial.rows;
      peak = Math.max(peak, partial.rss_mb);
      peakHeap = Math.max(peakHeap, partial.heap_mb);
      for (const [name, source] of Object.entries(partial.columns)) {
        const target = merged.get(name);
        if (target === undefined) {
          merged.set(name, {
            ...source,
            hll: source.hll.slice(),
            cms: source.cms.slice(),
            candidates: new Map(source.candidates),
            exact: source.exact === null ? null : new Set(source.exact),
            numeric: { ...source.numeric },
            types: { ...source.types },
          });
          continue;
        }
        target.count += source.count;
        target.nulls += source.nulls;
        for (const [kind, value] of Object.entries(source.types)) {
          target.types[kind] = (target.types[kind] ?? 0) + value;
        }
        for (let i = 0; i < HLL_M; i++) {
          if (source.hll[i] > target.hll[i]) target.hll[i] = source.hll[i];
        }
        for (let i = 0; i < target.cms.length; i++) target.cms[i] += source.cms[i];
        for (const [candidate, count] of source.candidates) {
          target.candidates.set(candidate, (target.candidates.get(candidate) ?? 0) + count);
        }
        while (target.candidates.size > CANDIDATES) {
          let smallestKey = "";
          let smallest = Infinity;
          for (const [key, count] of target.candidates) {
            if (count < smallest) {
              smallest = count;
              smallestKey = key;
            }
          }
          target.candidates.delete(smallestKey);
        }
        if (target.exact === null || source.exact === null) {
          target.exact = null;
        } else {
          const union = new Set([...target.exact, ...source.exact]);
          target.exact = union.size <= EXACT_LIMIT ? union : null;
        }
        target.numeric = mergeNumeric(target.numeric, source.numeric);
      }
    }

    const columns = [...merged.entries()]
      .map(([name, column]) => finalize(name, column, rows))
      .sort((a, b) => a.column.localeCompare(b.column));

    process.stdout.write(
      JSON.stringify({
        schema_version: 1,
        input,
        bytes: size,
        rows,
        elapsed_s: Number(elapsed.toFixed(4)),
        mb_per_s: Number((size / 1048576 / Math.max(elapsed, 1e-9)).toFixed(1)),
        workers: ranges.length,
        peak_worker_rss_mb: Number(peak.toFixed(1)),
        peak_worker_heap_mb: Number(peakHeap.toFixed(1)),
        columns,
      }),
    );
  };

  if (ranges.length === 0) {
    finish();
  } else {
    let remaining = ranges.length;
    for (const [start, end] of ranges) {
      const worker = new Worker(new URL(import.meta.url), {
        workerData: { path: input, start, end },
      });
      worker.on("message", (message: Partial) => {
        partials.push(message);
        remaining -= 1;
        if (remaining === 0) finish();
      });
      worker.on("error", (error) => {
        console.error("worker failed:", error.message);
        process.exit(1);
      });
    }
  }
}
