require('dotenv').config();
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const REPORTS_DIR = path.join(__dirname, 'reports');
const SUMMARY_FILE = path.join(REPORTS_DIR, 'gradingSummary.json');

function resolvePythonBinary() {
  const configured = String(process.env.PYTHON_BIN || '').trim();
  if (configured && !['python', 'python3', 'py'].includes(configured.toLowerCase())) {
    return configured;
  }

  if (process.platform === 'win32') {
    try {
      const whereResult = spawnSync('where.exe', ['python'], { encoding: 'utf8' });
      if (whereResult.status === 0 && whereResult.stdout) {
        const resolved = whereResult.stdout
          .split(/\r?\n/)
          .map((line) => line.trim())
          .find(Boolean);
        if (resolved) {
          return resolved;
        }
      }
    } catch (error) {
      // Fall through to the generic command below.
    }

    const localPython = 'C:\\Python314\\python.exe';
    if (fs.existsSync(localPython)) {
      return localPython;
    }
  }

  return 'python';
}

function runPythonGrader() {
  return new Promise((resolve, reject) => {
    const pythonBinary = resolvePythonBinary();

    console.error(`[grading] Using python binary: ${pythonBinary}`);

    const py = spawn(pythonBinary, ['grade_props.py'], {
      cwd: __dirname,
      stdio: 'inherit',
    });

    py.on('error', (error) => {
      reject(new Error(`Failed to spawn ${pythonBinary}: ${error.code || 'unknown'} ${error.message}`));
    });
    py.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(`Python grader exited with code ${code}`));
        return;
      }

      resolve({ success: true });
    });
  });
}

function loadSummaryFromDisk() {
  if (!fs.existsSync(SUMMARY_FILE)) {
    return null;
  }

  const raw = fs.readFileSync(SUMMARY_FILE, 'utf8');
  return JSON.parse(raw);
}

async function main() {
  const graderOutput = await runPythonGrader();
  if (!graderOutput.success) {
    throw new Error(graderOutput.error || 'Unknown grading failure');
  }

  const summary = loadSummaryFromDisk();
  const batch = summary?.batch || {};
  const overall = summary?.overall?.uniqueLineLevel || summary?.overall || {};

  console.log(
    [
      '[grading] Grading complete.',
      `newly_graded=${batch.newlyGraded || 0}`,
      `pending_checked=${batch.pendingChecked || 0}`,
      `unique_lines=${overall.gradedCount || 0}`,
      'Run `npm run recap` to post the Discord recap.',
    ].join(' ')
  );
}

main().catch((error) => {
  console.error('[grading] Failed:', error.message);
  process.exit(1);
});
