import { readFile } from "node:fs/promises";

const tests = [];

globalThis.Deno = {
  test(name, run) {
    tests.push({ name, run });
  },
  readTextFile(path) {
    return readFile(path, "utf8");
  }
};

await import("../supabase/functions/pilot-api/_shared/core_test.ts");

let failures = 0;
for (const test of tests) {
  try {
    await test.run();
    console.log("ok - " + test.name);
  } catch (error) {
    failures += 1;
    console.error("not ok - " + test.name);
    console.error(error);
  }
}

console.log(
  (failures === 0 ? "PASS" : "FAIL") +
    " backend core WebCrypto tests=" +
    tests.length +
    " failures=" +
    failures
);
if (failures !== 0) {
  process.exitCode = 1;
}
