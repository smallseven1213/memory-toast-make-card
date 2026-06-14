#!/usr/bin/env node
'use strict';

/**
 * Installer for the memory-toast-make-card Claude skill.
 *
 *   npx memory-toast-make-card install            # → ~/.claude/skills/
 *   npx memory-toast-make-card install --project  # → ./.claude/skills/
 *   npx memory-toast-make-card install --dir PATH # → PATH/
 *
 * Copies SKILL.md + scripts/ + references/ into a Claude skills directory.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const SKILL_NAME = 'memory-toast-make-card';
const PAYLOAD = ['SKILL.md', 'scripts', 'references'];
const SKIP = new Set(['__pycache__', 'build', '.DS_Store']);
const pkgRoot = path.resolve(__dirname, '..');

function copyRecursive(src, dest) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const entry of fs.readdirSync(src)) {
      if (SKIP.has(entry) || entry.endsWith('.pyc')) continue;
      copyRecursive(path.join(src, entry), path.join(dest, entry));
    }
  } else {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
  }
}

function main() {
  const args = process.argv.slice(2);
  const cmd = args.find((a) => !a.startsWith('-')) || 'install';
  if (cmd !== 'install') {
    console.error(`Unknown command "${cmd}".`);
    console.error(`Usage: npx ${SKILL_NAME} install [--project] [--dir <path>]`);
    process.exit(1);
  }

  let baseDir;
  const dirIdx = args.indexOf('--dir');
  if (dirIdx !== -1 && args[dirIdx + 1]) {
    baseDir = path.resolve(args[dirIdx + 1]);
  } else if (args.includes('--project')) {
    baseDir = path.resolve(process.cwd(), '.claude', 'skills');
  } else {
    baseDir = path.join(os.homedir(), '.claude', 'skills');
  }

  const target = path.join(baseDir, SKILL_NAME);
  for (const item of PAYLOAD) {
    const src = path.join(pkgRoot, item);
    if (!fs.existsSync(src)) {
      console.error(`ERROR: missing payload "${item}" in package at ${pkgRoot}`);
      process.exit(1);
    }
    copyRecursive(src, path.join(target, item));
  }

  console.log(`Installed "${SKILL_NAME}" skill → ${target}`);
  console.log('');
  console.log('Next steps:');
  console.log(`  1. python3 "${path.join(target, 'scripts', 'mt_login.py')}"   # log in once`);
  console.log('  2. (optional) export OPENAI_API_KEY or GEMINI_API_KEY to generate card images');
  console.log('  3. In Claude Code, ask: "make a Memory Toast deck about <topic>"');
}

main();
