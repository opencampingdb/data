// Checks OSM opening_hours values with opening_hours.js, the reference the
// OSM wiki names for the tag.
//
// Reads a JSON array of strings on stdin and writes a JSON array of the same
// length on stdout, one result per value: {"out_of_spec": bool, "error": text
// or null}. pipeline/maproulette.py runs it. Install with `npm ci` here first.
//
// A value is read as Australian (country code au). Style warnings are not out
// of spec. The library has no Australian school holiday data, so a value with
// SH fails in Australia for that reason alone. Such a value is read a second
// time with a locale that has holiday data, only to find any real error behind
// the missing data: when that read passes, the value is not out of spec.

import opening_hours from "opening_hours";

const AUSTRALIA = { address: { country_code: "au" } };
// Only used to look past missing holiday data. Its holidays mean nothing here.
const HOLIDAY_DATA = { lat: 52.52, lon: 13.4, address: { country_code: "de", state: "Berlin" } };
const MISSING_HOLIDAYS = /no holidays \((SH|PH)\) defined|Country code missing/;

// The library prints its own messages. stdout carries only the results.
console.log = console.warn = console.error = console.info = () => {};

function firstError(value, locale) {
  try {
    new opening_hours(value, locale, { tag_key: "opening_hours" });
    return null;
  } catch (error) {
    return String(error?.message ?? error).split("\n")[0].trim();
  }
}

export function check(value) {
  if (typeof value !== "string") return { out_of_spec: true, error: "not text" };
  const error = firstError(value, AUSTRALIA);
  if (error === null) return { out_of_spec: false, error: null };
  if (MISSING_HOLIDAYS.test(error)) {
    const behind = firstError(value, HOLIDAY_DATA);
    if (behind === null || MISSING_HOLIDAYS.test(behind)) return { out_of_spec: false, error: null };
    return { out_of_spec: true, error: behind };
  }
  return { out_of_spec: true, error };
}

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const values = JSON.parse(Buffer.concat(chunks).toString("utf8") || "[]");
process.stdout.write(JSON.stringify(values.map(check)) + "\n");
