require('dotenv').config();
const { spawn } = require('child_process');
const path = require('path');

const PYTHON_BIN = process.env.PYTHON_BIN || 'python';
const BASE_DIR = path.resolve(__dirname, '..', '..');
const PYTHON_DIR = path.join(BASE_DIR, 'src', 'py');

function runArchive() {
  return new Promise((resolve, reject) => {
    let output = '';
    let errors = '';

    const py = spawn(PYTHON_BIN, [path.join(PYTHON_DIR, 'archive_props.py')], { cwd: BASE_DIR });

    py.stdout.on('data', (data) => {
      output += data.toString();
    });

    py.stderr.on('data', (data) => {
      errors += data.toString();
    });

    py.on('error', reject);
    py.on('close', (code) => {
      if (errors.trim()) {
        console.warn('[archive] Python stderr:', errors.trim());
      }

      if (code !== 0) {
        reject(new Error(output.trim() || `Python archiver exited with code ${code}`));
        return;
      }

      try {
        resolve(JSON.parse(output.trim()));
      } catch (error) {
        reject(new Error(`Invalid archiver output: ${error.message}`));
      }
    });
  });
}

async function main() {
  const result = await runArchive();
  if (!result.success) {
    throw new Error(result.error || 'Unknown archive failure');
  }

  console.log(
    `[archive] Archived posted=${result.archivedPosted} graded=${result.archivedGraded} retainedPosted=${result.retainedPosted} retainedGraded=${result.retainedGraded}`,
  );
}

main().catch((error) => {
  console.error('[archive] Failed:', error.message);
  process.exit(1);
});
