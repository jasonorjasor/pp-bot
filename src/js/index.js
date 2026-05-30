require('dotenv').config();
const { Client, GatewayIntentBits, EmbedBuilder } = require('discord.js');
const fetch = require('node-fetch');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const REQUIRED_ENV_VARS = ['DISCORD_TOKEN', 'CHANNEL_ID'];
const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const LOG_LEVEL = process.env.LOG_LEVEL || 'info';
const POLL_INTERVAL_MS = parsePositiveInteger(process.env.POLL_INTERVAL_MS, 300000);
const PYTHON_BIN = process.env.PYTHON_BIN || 'python';
const PYTHON_TIMEOUT_MS = 60000;
const CONTEXT_REFRESH_TIMEOUT_MS = 180000;
const SEND_DELAY_MS = 2000;
const MIN_RECOMMENDATION_SCORE = parsePositiveNumber(process.env.MIN_RECOMMENDATION_SCORE, 6.5);
const MIN_SAMPLE_SIZE = parsePositiveInteger(process.env.MIN_SAMPLE_SIZE, 6);
const BEST_BET_SCORE = parsePositiveNumber(process.env.BEST_BET_SCORE, 8.0);
const WATCHLIST_SCORE = parsePositiveNumber(process.env.WATCHLIST_SCORE, 6.5);
const CONTEXT_CACHE_TTL_HOURS = parsePositiveNumber(process.env.CONTEXT_CACHE_TTL_HOURS, 24);
const CONTEXT_ENABLE_PACE = parseBoolean(process.env.CONTEXT_ENABLE_PACE, true);
const CONTEXT_ENABLE_OPPONENT = parseBoolean(process.env.CONTEXT_ENABLE_OPPONENT, true);
const CONTEXT_ENABLE_REST = parseBoolean(process.env.CONTEXT_ENABLE_REST, true);
const CONTEXT_ENABLE_ROLE = parseBoolean(process.env.CONTEXT_ENABLE_ROLE, true);
const BASE_DIR = path.resolve(__dirname, '..', '..');
const PYTHON_DIR = path.join(BASE_DIR, 'src', 'py');
const DATA_DIR = path.join(BASE_DIR, 'data');
const ACTIVE_DATA_DIR = path.join(DATA_DIR, 'active');
const SEEN_FILE = path.join(ACTIVE_DATA_DIR, 'seenProps.json');
const POSTED_ALERTS_FILE = path.join(ACTIVE_DATA_DIR, 'postedProps.jsonl');
const TEAM_CONTEXT_CACHE_FILE = path.join(ACTIVE_DATA_DIR, 'teamContextCache.json');
const PRIZEPICKS_DEVICE_ID_FILE = path.join(ACTIVE_DATA_DIR, 'prizepicksDeviceId.json');
const PRIZEPICKS_DEVICE_ID = process.env.PRIZEPICKS_DEVICE_ID || loadPrizePicksDeviceId();
const PRIZEPICKS_COOKIE = process.env.PRIZEPICKS_COOKIE || '';
const PRIZEPICKS_URLS = [
  'https://api.prizepicks.com/projections?league_id=7&per_page=250&single_stat=true',
  'https://api.prizepicks.com/projections?league_id=7&per_page=250&single_stat=true&state=CA',
  'https://api.prizepicks.com/projections?league_id=7&per_page=250&single_stat=true&game_mode=pickem',
];
const PRIZEPICKS_USER_AGENTS = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36',
];
const PRIZEPICKS_HEADERS_BASE = {
  Accept: 'application/json, text/plain, */*',
  'Accept-Language': 'en-US,en;q=0.9',
  'Cache-Control': 'no-cache',
  Pragma: 'no-cache',
  Origin: 'https://app.prizepicks.com',
  Referer: 'https://app.prizepicks.com/',
  'X-Requested-With': 'XMLHttpRequest',
  'X-Device-ID': PRIZEPICKS_DEVICE_ID,
  'Sec-Fetch-Site': 'same-site',
  'Sec-Fetch-Mode': 'cors',
  'Sec-Fetch-Dest': 'empty',
  'Sec-CH-UA': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
  'Sec-CH-UA-Mobile': '?0',
  'Sec-CH-UA-Platform': '"Windows"',
};
const PRIZEPICKS_BACKOFF_MS = parsePositiveInteger(process.env.PRIZEPICKS_BACKOFF_MS, 300000);
let prizePicksBackoffUntil = 0;
let prizePicksUaIndex = 0;

const client = new Client({ intents: [GatewayIntentBits.Guilds] });

function buildPrizePicksHeaders() {
  const ua = PRIZEPICKS_USER_AGENTS[prizePicksUaIndex % PRIZEPICKS_USER_AGENTS.length];
  const cookieHeader = PRIZEPICKS_COOKIE ? { Cookie: PRIZEPICKS_COOKIE } : {};
  return {
    ...PRIZEPICKS_HEADERS_BASE,
    'User-Agent': ua,
    ...cookieHeader,
  };
}

let seenProps = loadJsonObject(SEEN_FILE, 'seen props');
const statCache = {};
let isRunning = false;
let pollTimer = null;
let contextRefreshPromise = null;

function parsePositiveInteger(value, fallback) {
  if (!value) {
    return fallback;
  }
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function parsePositiveNumber(value, fallback) {
  if (!value) {
    return fallback;
  }
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function loadPrizePicksDeviceId() {
  try {
    if (fs.existsSync(PRIZEPICKS_DEVICE_ID_FILE)) {
      const raw = fs.readFileSync(PRIZEPICKS_DEVICE_ID_FILE, 'utf8');
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.deviceId === 'string' && parsed.deviceId.trim()) {
        return parsed.deviceId.trim();
      }
    }
  } catch (error) {
    // Ignore and regenerate below.
  }

  const deviceId = crypto.randomUUID();
  try {
    ensureParentDir(PRIZEPICKS_DEVICE_ID_FILE);
    fs.writeFileSync(PRIZEPICKS_DEVICE_ID_FILE, JSON.stringify({ deviceId }), 'utf8');
  } catch (error) {
    // Ignore persistence errors; still return the generated id.
  }
  return deviceId;
}

function parseBoolean(value, fallback) {
  if (value === undefined || value === null || value === '') {
    return fallback;
  }
  return !['0', 'false', 'no', 'off'].includes(String(value).trim().toLowerCase());
}

function getLogLevelValue(level) {
  return Object.prototype.hasOwnProperty.call(LOG_LEVELS, level)
    ? LOG_LEVELS[level]
    : LOG_LEVELS.info;
}

function log(level, scope, message, extra) {
  const configuredLevel = getLogLevelValue(LOG_LEVEL);
  const messageLevel = getLogLevelValue(level);
  if (messageLevel > configuredLevel) {
    return;
  }

  const line = `[${scope}] ${message}`;
  if (level === 'error') {
    console.error(line, extra ?? '');
  } else if (level === 'warn') {
    console.warn(line, extra ?? '');
  } else {
    console.log(line, extra ?? '');
  }
}

function validateEnv() {
  const missing = REQUIRED_ENV_VARS.filter((name) => !process.env[name]);

  if (missing.length > 0) {
    throw new Error(`Missing required environment variables: ${missing.join(', ')}`);
  }
  if (!Number.isFinite(POLL_INTERVAL_MS) || POLL_INTERVAL_MS <= 0) {
    throw new Error(`POLL_INTERVAL_MS must be a positive integer. Received: ${process.env.POLL_INTERVAL_MS}`);
  }
  if (BEST_BET_SCORE < WATCHLIST_SCORE) {
    throw new Error('BEST_BET_SCORE must be greater than or equal to WATCHLIST_SCORE.');
  }
  if (!Number.isFinite(CONTEXT_CACHE_TTL_HOURS) || CONTEXT_CACHE_TTL_HOURS <= 0) {
    throw new Error(`CONTEXT_CACHE_TTL_HOURS must be a positive number. Received: ${process.env.CONTEXT_CACHE_TTL_HOURS}`);
  }
}

function loadJsonObject(filePath, label) {
  if (!fs.existsSync(filePath)) {
    log('info', 'startup', `No ${label} file found; starting with empty state.`);
    return {};
  }

  try {
    const raw = fs.readFileSync(filePath, 'utf8').trim();
    if (!raw) {
      log('warn', 'startup', `${label} file is empty; starting with empty state.`);
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('JSON root must be an object');
    }
    return parsed;
  } catch (error) {
    log('error', 'startup', `Failed to parse ${path.basename(filePath)}; starting with empty state.`, error.message);
    return {};
  }
}

function ensureParentDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function saveSeenProps(nextSeenProps) {
  ensureParentDir(SEEN_FILE);
  fs.writeFileSync(SEEN_FILE, JSON.stringify(nextSeenProps, null, 2));
}

function appendPostedAlert(record) {
  ensureParentDir(POSTED_ALERTS_FILE);
  fs.appendFileSync(POSTED_ALERTS_FILE, `${JSON.stringify(record)}\n`, 'utf8');
}

function loadContextCacheMetadata() {
  if (!fs.existsSync(TEAM_CONTEXT_CACHE_FILE)) {
    return null;
  }

  try {
    const raw = JSON.parse(fs.readFileSync(TEAM_CONTEXT_CACHE_FILE, 'utf8'));
    return {
      generatedAt: raw.generatedAt ? new Date(raw.generatedAt) : null,
      season: raw.season || null,
      source: raw.source || null,
    };
  } catch (error) {
    log('warn', 'context', 'Failed to read team context cache metadata.', error.message);
    return null;
  }
}

function isSameLocalDay(a, b) {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

function isContextCacheStale(metadata) {
  if (!metadata?.generatedAt || Number.isNaN(metadata.generatedAt.getTime())) {
    return true;
  }

  const now = new Date();
  const ageMs = now.getTime() - metadata.generatedAt.getTime();
  if (ageMs > CONTEXT_CACHE_TTL_HOURS * 60 * 60 * 1000) {
    return true;
  }

  return !isSameLocalDay(now, metadata.generatedAt);
}

function isContextEnabled() {
  return CONTEXT_ENABLE_PACE || CONTEXT_ENABLE_OPPONENT || CONTEXT_ENABLE_REST || CONTEXT_ENABLE_ROLE;
}

function refreshContextCache() {
  return new Promise((resolve, reject) => {
    let output = '';
    let errors = '';
    let settled = false;

    const py = spawn(PYTHON_BIN, [path.join(PYTHON_DIR, 'refresh_team_context.py')], {
      cwd: BASE_DIR,
    });

    const timer = setTimeout(() => {
      if (settled) {
        return;
      }
      settled = true;
      py.kill();
      reject(new Error(`Context refresh timed out after ${CONTEXT_REFRESH_TIMEOUT_MS}ms.`));
    }, CONTEXT_REFRESH_TIMEOUT_MS);

    py.stdout.on('data', (data) => {
      output += data.toString();
    });

    py.stderr.on('data', (data) => {
      errors += data.toString();
    });

    py.on('error', (error) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      reject(error);
    });

    py.on('close', (code) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);

      if (errors.trim()) {
        log('warn', 'context', `refresh stderr: ${errors.trim()}`);
      }

      if (code !== 0) {
        reject(new Error(output.trim() || `Context refresh exited with code ${code}`));
        return;
      }

      try {
        resolve(JSON.parse(output.trim()));
      } catch (error) {
        reject(new Error(`Invalid context refresh output: ${error.message}`));
      }
    });
  });
}

async function ensureContextCacheFresh() {
  if (!isContextEnabled()) {
    return;
  }

  const metadata = loadContextCacheMetadata();
  if (!isContextCacheStale(metadata)) {
    return;
  }

  if (contextRefreshPromise) {
    return contextRefreshPromise;
  }

  log('info', 'context', 'Refreshing daily team context cache.');
  contextRefreshPromise = refreshContextCache()
    .then((result) => {
      if (!result?.success) {
        throw new Error(result?.error || 'Unknown context refresh failure');
      }
      log('info', 'context', `Team context cache refreshed for season ${result.season}.`);
    })
    .catch((error) => {
      log('warn', 'context', 'Context refresh failed; continuing with cached or base model only.', error.message);
    })
    .finally(() => {
      contextRefreshPromise = null;
    });

  return contextRefreshPromise;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function classifyPythonError(result, fallback = 'unknown') {
  const errorText = String(result?.error || fallback).toLowerCase();
  if (errorText.includes('unknown stat type')) {
    return 'unknown_stat_type';
  }
  if (errorText.includes('player not found')) {
    return 'missing_player';
  }
  if (errorText.includes('timed out')) {
    return 'timeout';
  }
  return fallback;
}

function callPython(playerName, statType, line, gameHint, startTime) {
  return new Promise((resolve) => {
    let output = '';
    let errors = '';
    let settled = false;

    const args = [path.join(PYTHON_DIR, 'nba_stats.py'), playerName, statType, String(line)];
    if (gameHint) {
      args.push(String(gameHint));
      if (startTime) {
        args.push(String(startTime));
      }
    }

    const py = spawn(PYTHON_BIN, args, { cwd: BASE_DIR });

    const timer = setTimeout(() => {
      if (settled) {
        return;
      }

      settled = true;
      py.kill();
      log('error', 'python', `Timed out for ${playerName} ${statType} after ${PYTHON_TIMEOUT_MS}ms.`);
      resolve({ success: false, error: 'Python timed out' });
    }, PYTHON_TIMEOUT_MS);

    py.stdout.on('data', (data) => {
      output += data.toString();
    });

    py.stderr.on('data', (data) => {
      errors += data.toString();
    });

    py.on('error', (error) => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timer);
      log('error', 'python', `Failed to start Python for ${playerName} ${statType}.`, error.message);
      resolve({ success: false, error: `spawn_error:${error.message}` });
    });

    py.on('close', (code, signal) => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timer);

      if (errors.trim()) {
        log('warn', 'python', `stderr for ${playerName} ${statType}: ${errors.trim()}`);
      }

      if (signal) {
        log('error', 'python', `Process exited via signal ${signal} for ${playerName} ${statType}.`);
        resolve({ success: false, error: `signal:${signal}` });
        return;
      }

      if (!output.trim()) {
        log('error', 'python', `No output returned for ${playerName} ${statType}. Exit code: ${code ?? 'unknown'}`);
        resolve({ success: false, error: code && code !== 0 ? `exit_code:${code}` : 'empty_output' });
        return;
      }

      try {
        const parsed = JSON.parse(output.trim());
        if (code && code !== 0 && parsed.success !== false) {
          parsed.success = false;
          parsed.error = parsed.error || `exit_code:${code}`;
        }
        resolve(parsed);
      } catch (error) {
        log('error', 'python', `Invalid JSON returned for ${playerName} ${statType}.`, error.message);
        resolve({ success: false, error: 'invalid_json' });
      }
    });
  });
}

async function getAnalytics(playerName, statType, line, gameHint, startTime) {
  const cacheKey = `${playerName}|${statType}|${line}|${gameHint || ''}|${startTime || ''}`;
  if (statCache[cacheKey]) {
    log('debug', 'python', `Cache hit for ${playerName} ${statType} ${line}.`);
    return statCache[cacheKey];
  }

  log('info', 'python', `Fetching analytics for ${playerName} ${statType} ${line}.`);
  const result = await callPython(playerName, statType, line, gameHint, startTime);

  if (!result || !result.success) {
    const reason = classifyPythonError(result, result?.error ?? 'unknown');
    log('warn', 'python', `Analytics failed for ${playerName} ${statType}. Reason: ${reason}.`);
    return null;
  }

  statCache[cacheKey] = result.analytics;
  return result.analytics;
}

function getTierLabel(tier) {
  if (tier === 'best_bet') {
    return '🔥 Best Bet';
  }
  if (tier === 'watchlist') {
    return '👀 Watchlist';
  }
  return '⏭️ Skip';
}

function getRecommendationLabel(side) {
  if (side === 'over') {
    return 'Lean Over';
  }
  if (side === 'under') {
    return 'Lean Under';
  }
  return 'Pass';
}

function getRecommendationColor(analytics, tier) {
  if (!analytics || analytics.recommendedSide === 'pass') {
    return 0x808080;
  }
  if (tier === 'best_bet') {
    return analytics.recommendedSide === 'over' ? 0x00ff88 : 0xff6b6b;
  }
  if (tier === 'watchlist') {
    return analytics.recommendedSide === 'over' ? 0x66cc66 : 0xffaa00;
  }
  return 0x808080;
}

function decideRecommendation(analytics) {
  if (!analytics) {
    return {
      shouldPost: false,
      tier: 'skip',
      recommendation: 'pass',
      score: 0,
      skipReason: 'Analytics unavailable.',
    };
  }

  if (analytics.sampleSize < MIN_SAMPLE_SIZE) {
    return {
      shouldPost: false,
      tier: 'skip',
      recommendation: 'pass',
      score: analytics.recommendationStrength || 0,
      skipReason: `Sample too small (${analytics.sampleSize} clean games).`,
    };
  }

  if (analytics.recommendedSide === 'pass') {
    return {
      shouldPost: false,
      tier: 'skip',
      recommendation: 'pass',
      score: analytics.recommendationStrength || 0,
      skipReason: analytics.reasonSummary || 'Analytics recommended pass.',
    };
  }

  if ((analytics.recommendationStrength || 0) < MIN_RECOMMENDATION_SCORE) {
    return {
      shouldPost: false,
      tier: 'skip',
      recommendation: analytics.recommendedSide,
      score: analytics.recommendationStrength || 0,
      skipReason: `Recommendation score ${analytics.recommendationStrength}/10 is below post threshold.`,
    };
  }

  const tier = analytics.recommendationStrength >= BEST_BET_SCORE
    ? 'best_bet'
    : analytics.recommendationStrength >= WATCHLIST_SCORE
      ? 'watchlist'
      : 'skip';

  return {
    shouldPost: tier !== 'skip',
    tier,
    recommendation: analytics.recommendedSide,
    score: analytics.recommendationStrength,
    skipReason: tier === 'skip' ? 'Score did not meet watchlist threshold.' : null,
  };
}

function formatHomeAwaySplit(analytics) {
  const overHome = analytics.homeOverPct ?? 'N/A';
  const underHome = analytics.homeUnderPct ?? 'N/A';
  const overAway = analytics.awayOverPct ?? 'N/A';
  const underAway = analytics.awayUnderPct ?? 'N/A';
  const homeAvg = analytics.homeAvg ?? 'N/A';
  const awayAvg = analytics.awayAvg ?? 'N/A';
  const homeGames = analytics.homeGP ?? 0;
  const awayGames = analytics.awayGP ?? 0;

  return [
    `🏠 Home: **${homeAvg}** avg | O ${overHome}% / U ${underHome}% (${homeGames}G)`,
    `✈️ Away: **${awayAvg}** avg | O ${overAway}% / U ${underAway}% (${awayGames}G)`,
  ].join('\n');
}

function formatProbability(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return 'N/A';
  }
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function formatNumber(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return 'N/A';
  }
  return Number(value).toFixed(digits);
}

function buildEmbed(playerName, attr, lineChangeText, analytics, decision) {
  const line = attr.line_score;
  const color = getRecommendationColor(analytics, decision.tier);
  const embed = new EmbedBuilder()
    .setColor(color)
    .setTitle(`🏀 PrizePicks NBA - ${playerName}`)
    .setFooter({ text: `PrizePicks NBA • Standard Lines Only • Sample: ${analytics?.sampleSize ?? '?'} clean games` })
    .setTimestamp();

  embed.addFields(
    { name: '📋 Stat', value: attr.stat_type, inline: true },
    { name: '🎯 Line', value: `**${line}**`, inline: true },
    { name: '📈 Movement', value: lineChangeText, inline: true },
    { name: '🏟️ Game', value: attr.description || 'TBD', inline: true },
    {
      name: '⏰ Start',
      value: attr.start_time
        ? new Date(attr.start_time).toLocaleString('en-US', {
          timeZone: 'America/New_York',
          hour: 'numeric',
          minute: '2-digit',
          hour12: true,
        })
        : 'TBD',
      inline: true,
    },
    { name: '🏷️ Tier', value: getTierLabel(decision.tier), inline: true },
  );

  if (!analytics) {
    embed.addFields({ name: '📊 Analytics', value: 'N/A', inline: false });
    return embed;
  }

  const edgeForSide = analytics.recommendedSide === 'under' ? -analytics.edge : analytics.edge;
  const edgePctForSide = analytics.recommendedSide === 'under' ? -analytics.edgePct : analytics.edgePct;
  const edgeSign = edgeForSide >= 0 ? '+' : '';
  const edgePctSign = edgePctForSide >= 0 ? '+' : '';
  const contextDelta = analytics.contextScoreDelta ?? 0;
  const contextDeltaSign = contextDelta >= 0 ? '+' : '';

  embed.addFields({
    name: '🧭 Recommendation',
    value: [
      `**${getRecommendationLabel(decision.recommendation)}**`,
      `Score: **${decision.score}/10**`,
      analytics.reasonSummary || 'No summary available.',
      analytics.contextSummary ? `Context: ${analytics.contextSummary}` : null,
    ].filter(Boolean).join('\n'),
    inline: false,
  });

  embed.addFields({
    name: `⚖️ Over vs Under (L${analytics.hitSampleSize} clean games)`,
    value: [
      `Over:  ${analytics.overBar} **${analytics.overHitRate}%** | Weighted **${analytics.overWeightedPct}%** | Score **${analytics.overScore}/10**`,
      `Under: ${analytics.underBar} **${analytics.underHitRate}%** | Weighted **${analytics.underWeightedPct}%** | Score **${analytics.underScore}/10**`,
      `Pushes: **${analytics.pushes}**`,
    ].join('\n'),
    inline: false,
  });

  embed.addFields({
    name: '📐 Averages and Edge',
    value: [
      `Mean: **${analytics.mean}** | Median: **${analytics.median}** | Std Dev: **${analytics.stdDev}**`,
      `Selected edge: **${edgeSign}${edgeForSide}** (${edgePctSign}${edgePctForSide}% vs line)`,
      `Volatility: **${analytics.volatility}** | Avg Minutes: **${analytics.avgMinutes}**`,
      `Base score: **${analytics.baseScore ?? analytics.recommendationStrength}/10** | Context delta: **${contextDeltaSign}${contextDelta}**`,
    ].join('\n'),
    inline: false,
  });

  if (analytics.projectionMethod || analytics.projectionMean !== undefined) {
    const lowConf = analytics.projectionLowConfidence
      ? `Yes${analytics.projectionConfidenceReasons && analytics.projectionConfidenceReasons.length > 0
        ? ` (${analytics.projectionConfidenceReasons.join(', ')})`
        : ''}`
      : 'No';
    const confidenceBand = analytics.projectionConfidenceBand ?? 'N/A';
    const familyStatus = analytics.projectionFamilyStatus ?? 'N/A';
    embed.addFields({
      name: '🔮 Projection (Full-role)',
      value: [
        `Method: **${analytics.projectionMethod ?? 'N/A'}** | Band: **${confidenceBand}** | Family: **${familyStatus}**`,
        `Low conf: **${lowConf}**`,
        `Proj mins: **${formatNumber(analytics.projectionMinutes, 1)}** | Proj rate: **${formatNumber(analytics.projectionRate, 4)}**`,
        `Proj mean: **${formatNumber(analytics.projectionMean, 2)}** | Std: **${formatNumber(analytics.projectionStd, 2)}**`,
        `P(over): **${formatProbability(analytics.pOverFull)}** | P(under): **${formatProbability(analytics.pUnderFull)}**`,
        `Adj P(over): **${formatProbability(analytics.pOverAdjusted)}** | Adj P(under): **${formatProbability(analytics.pUnderAdjusted)}** | Void: **${formatProbability(analytics.pVoid)}**`,
      ].join('\n'),
      inline: false,
    });
  }

  embed.addFields({
    name: '🏠 Home / Away Split',
    value: formatHomeAwaySplit(analytics),
    inline: false,
  });

  const last5 = analytics.games.slice(0, 5).map((game) => {
    let resultIcon = '➖';
    if (game.over) {
      resultIcon = '✅';
    } else if (game.under) {
      resultIcon = '❌';
    }
    const location = game.home ? '🏠' : '✈️';
    const rest = game.restDays !== null && game.restDays !== undefined ? ` | ${game.restDays}d rest` : '';
    return `${resultIcon}${location} ${game.date} - **${game.value}** (${game.minutes}min)${rest}`;
  }).join('\n');

  embed.addFields({
    name: '🗓️ Last 5 Clean Games',
    value: last5 || 'N/A',
    inline: false,
  });

  if (analytics.flaggedList && analytics.flaggedList.length > 0) {
    const flagged = analytics.flaggedList.map((game) => (
      `⚠️ ${game.date} ${game.matchup} - ${game.minutes}min (${game.flagReason})`
    )).join('\n');

    embed.addFields({
      name: '🚫 Excluded Games',
      value: flagged,
      inline: false,
    });
  }

  return embed;
}

async function fetchPrizePicksData() {
  if (Date.now() < prizePicksBackoffUntil) {
    const waitSeconds = Math.ceil((prizePicksBackoffUntil - Date.now()) / 1000);
    throw new Error(`PrizePicks backoff active for ${waitSeconds}s.`);
  }

  log('info', 'prizepicks', 'Fetching latest props.');
  let sawForbidden = false;
  let lastError = null;

  for (const url of PRIZEPICKS_URLS) {
    const response = await fetch(url, {
      headers: buildPrizePicksHeaders(),
    });
    if (response.ok) {
      return response.json();
    }
    let bodySnippet = '';
    try {
      const text = await response.text();
      bodySnippet = text ? text.slice(0, 160).replace(/\s+/g, ' ').trim() : '';
      if (bodySnippet) {
        log('warn', 'prizepicks', `Non-OK response body (truncated): ${bodySnippet}`);
      }
      if (text && /cloudflare|cf_clearance|attention required/i.test(text)) {
        log('warn', 'prizepicks', 'Response looks like a Cloudflare challenge. You may need a valid PRIZEPICKS_COOKIE from a browser session.');
      }
    } catch (error) {
      // ignore response body parse errors
    }
    log('warn', 'prizepicks', `PrizePicks request failed with status ${response.status} for ${url}.`);
    if (response.status === 403) {
      sawForbidden = true;
      prizePicksUaIndex = (prizePicksUaIndex + 1) % PRIZEPICKS_USER_AGENTS.length;
      lastError = new Error('PrizePicks request blocked (403).');
      continue;
    }
    lastError = new Error(`PrizePicks request failed with status ${response.status}.`);
  }

  if (sawForbidden) {
    prizePicksBackoffUntil = Date.now() + PRIZEPICKS_BACKOFF_MS;
    throw new Error(`PrizePicks request blocked (403). Backing off for ${Math.round(PRIZEPICKS_BACKOFF_MS / 60000)}m.`);
  }

  throw lastError || new Error('PrizePicks request failed without a response.');
}

async function resolveChannel() {
  try {
    const channel = await client.channels.fetch(process.env.CHANNEL_ID);
    if (!channel || typeof channel.send !== 'function') {
      throw new Error('Configured channel is not a text channel');
    }
    return channel;
  } catch (error) {
    log('error', 'discord', 'Failed to resolve Discord channel.', error.message);
    throw error;
  }
}

function buildPlayerMap(data) {
  const playerMap = {};
  for (const item of data.included || []) {
    if (item.type === 'new_player') {
      playerMap[item.id] = item.attributes.display_name;
    }
  }
  return playerMap;
}

function buildAlertId(propId, postedAt, line, recommendedSide) {
  return [propId, postedAt, line, recommendedSide].join('|');
}

function buildPostedAlertRecord({ propId, playerName, attr, lineChangeText, decision, analytics }) {
  const postedAt = new Date().toISOString();
  return {
    alertId: buildAlertId(propId, postedAt, attr.line_score, decision.recommendation),
    postedAt,
    propId,
    playerName,
    statType: attr.stat_type,
    line: attr.line_score,
    lineChangeText,
    recommendedSide: decision.recommendation,
    tier: decision.tier,
    score: decision.score,
    game: attr.description || null,
    startTime: attr.start_time || null,
    analytics: {
      sampleSize: analytics.sampleSize,
      hitSampleSize: analytics.hitSampleSize,
      mean: analytics.mean,
      median: analytics.median,
      stdDev: analytics.stdDev,
      edge: analytics.edge,
      edgePct: analytics.edgePct,
      overHitRate: analytics.overHitRate,
      underHitRate: analytics.underHitRate,
      overWeightedPct: analytics.overWeightedPct,
      underWeightedPct: analytics.underWeightedPct,
      overScore: analytics.overScore,
      underScore: analytics.underScore,
      baseScore: analytics.baseScore,
      finalScore: analytics.finalScore,
      minutesBaseline: analytics.minutesBaseline,
      minutesRegimeCounts: analytics.minutesRegimeCounts,
      dnpRateWeighted: analytics.dnpRateWeighted,
      limitedRateWeighted: analytics.limitedRateWeighted,
      roleRiskPenalty: analytics.roleRiskPenalty,
      projectionMethod: analytics.projectionMethod,
      projectionConfidenceBand: analytics.projectionConfidenceBand,
      projectionFamilyStatus: analytics.projectionFamilyStatus,
      projectionMinutes: analytics.projectionMinutes,
      projectionRate: analytics.projectionRate,
      projectionMean: analytics.projectionMean,
      projectionStd: analytics.projectionStd,
      projectionSampleSizeFull: analytics.projectionSampleSizeFull,
      projectionSampleSizeLimited: analytics.projectionSampleSizeLimited,
      pOverFull: analytics.pOverFull,
      pUnderFull: analytics.pUnderFull,
      pOverAdjusted: analytics.pOverAdjusted,
      pUnderAdjusted: analytics.pUnderAdjusted,
      pVoid: analytics.pVoid,
      projectionLowConfidence: analytics.projectionLowConfidence,
      projectionConfidenceReasons: analytics.projectionConfidenceReasons,
      projectionInputs: analytics.projectionInputs,
      paceAdjustment: analytics.paceAdjustment,
      opponentAdjustment: analytics.opponentAdjustment,
      restAdjustment: analytics.restAdjustment,
      roleAdjustment: analytics.roleAdjustment,
      contextScoreDelta: analytics.contextScoreDelta,
      contextSummary: analytics.contextSummary,
      contextInputs: analytics.contextInputs,
      reasonSummary: analytics.reasonSummary,
    },
  };
}

async function fetchPrizePicksProps() {
  const summary = {
    fetched: 0,
    skippedUnchanged: 0,
    skippedDecision: 0,
    analyzed: 0,
    posted: 0,
    failed: 0,
  };
  const nextSeenProps = { ...seenProps };
  let stateDirty = false;

  try {
    const data = await fetchPrizePicksData();
    const channel = await resolveChannel();
    const playerMap = buildPlayerMap(data);

    summary.fetched = Array.isArray(data.data) ? data.data.length : 0;

    for (const proj of data.data || []) {
      const attr = proj.attributes;
      const propId = proj.id;
      const currentLine = attr.line_score;

      if (attr.odds_type !== 'standard') {
        continue;
      }

      let lineChangeText = '🆕 New';
      const existing = nextSeenProps[propId];
      if (existing) {
        const oldLine = existing.line;
        if (currentLine === oldLine) {
          summary.skippedUnchanged += 1;
          continue;
        }

        const diff = currentLine - oldLine;
        lineChangeText = diff > 0 ? `⬆️ Up from ${oldLine}` : `⬇️ Down from ${oldLine}`;
        existing.line = currentLine;
        stateDirty = true;
      } else {
        nextSeenProps[propId] = { seen: true, line: currentLine };
        stateDirty = true;
      }

      const playerId = proj.relationships?.new_player?.data?.id;
      const playerName = playerMap[playerId] ?? 'Unknown Player';

      try {
        const analytics = await getAnalytics(
          playerName,
          attr.stat_type,
          currentLine,
          attr.description || null,
          attr.start_time || null,
        );
        summary.analyzed += 1;

        const decision = decideRecommendation(analytics);
        if (!decision.shouldPost) {
          summary.skippedDecision += 1;
          log('info', 'decision', `Skipping ${playerName} ${attr.stat_type} ${currentLine}. ${decision.skipReason}`);
          continue;
        }

        const embed = buildEmbed(playerName, attr, lineChangeText, analytics, decision);
        await channel.send({ embeds: [embed] });
        appendPostedAlert(buildPostedAlertRecord({
          propId,
          playerName,
          attr,
          lineChangeText,
          decision,
          analytics,
        }));
        summary.posted += 1;
      } catch (error) {
        summary.failed += 1;
        log('error', 'discord', `Failed to process/send prop ${propId} for ${playerName}.`, error.message);
      }

      await sleep(SEND_DELAY_MS);
    }

    if (stateDirty) {
      saveSeenProps(nextSeenProps);
      seenProps = nextSeenProps;
      log('debug', 'poll', 'Persisted seen props snapshot.');
    }
  } catch (error) {
    summary.failed += 1;
    log('error', 'poll', 'Poll cycle failed.', error.message);
  } finally {
    log(
      'info',
      'poll',
      `Cycle summary: fetched=${summary.fetched} skipped_unchanged=${summary.skippedUnchanged} skipped_decision=${summary.skippedDecision} analyzed=${summary.analyzed} posted=${summary.posted} failed=${summary.failed}`,
    );
  }
}

async function runPollCycle() {
  if (isRunning) {
    log('warn', 'poll', 'Previous cycle still running; skipping this interval.');
    return;
  }

  isRunning = true;
  try {
    await ensureContextCacheFresh();
    await fetchPrizePicksProps();
  } finally {
    isRunning = false;
  }
}

function startPolling() {
  runPollCycle().catch((error) => {
    log('error', 'poll', 'Initial poll failed unexpectedly.', error.message);
  });

  pollTimer = setInterval(() => {
    runPollCycle().catch((error) => {
      log('error', 'poll', 'Scheduled poll failed unexpectedly.', error.message);
    });
  }, POLL_INTERVAL_MS);

  log('info', 'startup', `Polling every ${POLL_INTERVAL_MS}ms.`);
}

function bootstrap() {
  try {
    validateEnv();
    log('info', 'startup', 'Environment validation passed.');
  } catch (error) {
    log('error', 'startup', error.message);
    process.exit(1);
  }

  client.once('ready', async () => {
    log('info', 'startup', `Bot online as ${client.user.tag}.`);
    await ensureContextCacheFresh();
    startPolling();
  });

  client.on('error', (error) => {
    log('error', 'discord', 'Discord client error.', error.message);
  });

  client.login(process.env.DISCORD_TOKEN).catch((error) => {
    log('error', 'startup', 'Discord login failed.', error.message);
    if (pollTimer) {
      clearInterval(pollTimer);
    }
    process.exit(1);
  });
}

if (require.main === module) {
  bootstrap();
}

module.exports = {
  buildPostedAlertRecord,
  buildAlertId,
  buildEmbed,
  decideRecommendation,
  getRecommendationLabel,
  getTierLabel,
};
