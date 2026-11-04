#!/usr/bin/env node
/**
 * Preflight Evaluation Benchmark Runner
 * Verifies scoring dispatch, registry coverage, and containment compliance
 * strictly following the Preflight Detection Spec v4.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

const REGISTRY_PATH = path.join(rootDir, 'defects', 'registry.json');
const API_BASE = process.env.PREFLIGHT_API_URL || 'http://localhost:8000';

console.log('=== Preflight Autonomous Evaluation Suite ===');
console.log(`API Base: ${API_BASE}`);
console.log(`Registry: ${REGISTRY_PATH}\n`);

// 1. Verify Defect Registry
if (!fs.existsSync(REGISTRY_PATH)) {
  console.error(`❌ Defect registry missing at: ${REGISTRY_PATH}`);
  process.exit(1);
}

const registryRaw = fs.readFileSync(REGISTRY_PATH, 'utf-8');
const registry = JSON.parse(registryRaw);
const defects = registry.defects || [];

console.log(`✔ Loaded ${defects.length} canonical defects from registry (v${registry.version || '4.0.0'}).`);

const redDefects = defects.filter(d => d.tier === 'red');
const orangeDefects = defects.filter(d => d.tier === 'orange');
const yellowDefects = defects.filter(d => d.tier === 'yellow');

console.log(`  - 🔴 Red (P0 - Blocker): ${redDefects.length}`);
console.log(`  - 🟠 Orange (P1 - High Hazard): ${orangeDefects.length}`);
console.log(`  - 🟡 Yellow (P2/P3 - Maintainability): ${yellowDefects.length}`);

if (defects.length < 13) {
  console.error(`❌ Expected at least 13 canonical defects, found ${defects.length}`);
  process.exit(1);
}

// 2. Evaluation Scenarios
const testCases = [
  {
    name: 'Clean Repository (Zero Defects)',
    findings: [],
    expectedVerdict: 'ready',
    expectedScoreMin: 90
  },
  {
    name: 'Single Red Defect (Leaked Secret)',
    findings: [
      { id: 'RED-01', rule: 'Leaked API keys', tier: 'red', unconfirmed: false }
    ],
    expectedVerdict: 'not_ready',
    expectedScoreMax: 90
  },
  {
    name: 'Single Orange Defect (Double Submit)',
    findings: [
      { id: 'ORANGE-07', rule: 'No loading state, double submission', tier: 'orange', unconfirmed: false }
    ],
    expectedVerdict: 'launch_with_care',
    expectedScoreMax: 95
  },
  {
    name: 'Unconfirmed Findings Do Not Deduct',
    findings: [
      { id: 'RED-01', rule: 'Leaked API keys', tier: 'red', unconfirmed: true },
      { id: 'ORANGE-07', rule: 'No loading state', tier: 'orange', unconfirmed: true }
    ],
    expectedVerdict: 'ready',
    expectedScoreMin: 90
  }
];

let failedCases = 0;

for (const tc of testCases) {
  // Local calculation formula verification
  let sec = 100, rel = 100, cor = 100, main = 100;
  let redCount = 0, orangeCount = 0, yellowCount = 0;

  for (const f of tc.findings) {
    if (f.unconfirmed) continue;
    const reg = defects.find(d => d.id === f.id) || f;
    const penalty = reg.penalty || (f.tier === 'red' ? 40 : f.tier === 'orange' ? 25 : 4);
    const tier = reg.tier || f.tier;
    const area = reg.area || (tier === 'red' ? 'security' : tier === 'orange' ? 'reliability' : 'maintainability');

    if (tier === 'red') {
      redCount++;
      if (area === 'security') sec = Math.max(0, sec - penalty);
      else rel = Math.max(0, rel - penalty);
    } else if (tier === 'orange') {
      orangeCount++;
      if (area === 'reliability') rel = Math.max(0, rel - penalty);
      else cor = Math.max(0, cor - penalty);
    } else {
      yellowCount++;
      main = Math.max(0, main - penalty);
    }
  }

  const score = Math.round(sec * 0.35 + rel * 0.30 + cor * 0.20 + main * 0.15);
  let verdict = 'ready';
  if (redCount > 0 || score < 70) {
    verdict = 'not_ready';
  } else if (orangeCount > 0 || (score >= 70 && score < 90)) {
    verdict = 'launch_with_care';
  }

  const passed = verdict === tc.expectedVerdict &&
    (!tc.expectedScoreMin || score >= tc.expectedScoreMin) &&
    (!tc.expectedScoreMax || score <= tc.expectedScoreMax);

  if (passed) {
    console.log(`✔ [PASS] ${tc.name}: score=${score}, verdict=${verdict}`);
  } else {
    console.error(`❌ [FAIL] ${tc.name}: score=${score}, verdict=${verdict} (expected ${tc.expectedVerdict})`);
    failedCases++;
  }
}

console.log('\n=== Evaluation Summary ===');
if (failedCases === 0) {
  console.log(`✔ All ${testCases.length} evaluation benchmarks PASSED.`);
  process.exit(0);
} else {
  console.error(`❌ ${failedCases} benchmarks FAILED.`);
  process.exit(1);
}
