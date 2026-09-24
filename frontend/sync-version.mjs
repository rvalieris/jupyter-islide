// Sync the JS package version from pyproject.toml (the single source of
// truth) into frontend/package.json and frontend/package-lock.json.
// Runs automatically as `prebuild` (see package.json scripts).
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

const pyproject = readFileSync(join(here, "..", "pyproject.toml"), "utf8");
const match = pyproject.match(/^version\s*=\s*"([^"]+)"/m);
if (!match) {
  throw new Error('Could not find version = "..." under [project] in pyproject.toml');
}
const version = match[1];

// package.json
const pkgPath = join(here, "package.json");
const pkg = JSON.parse(readFileSync(pkgPath, "utf8"));
if (pkg.version !== version) {
  pkg.version = version;
  writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n");
}

// package-lock.json (lockfile v3): top-level + root package entry
const lockPath = join(here, "package-lock.json");
const lock = JSON.parse(readFileSync(lockPath, "utf8"));
let lockChanged = false;
if (lock.version !== version) {
  lock.version = version;
  lockChanged = true;
}
if (lock.packages && lock.packages[""] && lock.packages[""].version !== version) {
  lock.packages[""].version = version;
  lockChanged = true;
}
if (lockChanged) {
  writeFileSync(lockPath, JSON.stringify(lock, null, 2) + "\n");
}

console.log(`sync-version: frontend versions set to ${version}`);
