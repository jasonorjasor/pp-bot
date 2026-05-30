require('dotenv').config();
const { Client, GatewayIntentBits, EmbedBuilder } = require('discord.js');
const fs = require('fs');
const path = require('path');

const GRADING_CHANNEL_ID = process.env.GRADING_CHANNEL_ID || process.env.CHANNEL_ID;
const DATA_DIR = path.join(__dirname, 'data');
const ACTIVE_DATA_DIR = path.join(DATA_DIR, 'active');
const REPORTS_DIR = path.join(__dirname, 'reports');
const SUMMARY_FILE = path.join(REPORTS_DIR, 'gradingSummary.json');
const LAST_RECAP_FILE = path.join(ACTIVE_DATA_DIR, 'lastRecapPosted.json');

function loadSummaryFromDisk() {
  const raw = fs.readFileSync(SUMMARY_FILE, 'utf8');
  return JSON.parse(raw);
}

function formatSideRecord(sideSummary) {
  return `W ${sideSummary.win} | L ${sideSummary.loss} | P ${sideSummary.push} | V ${sideSummary.void}`;
}

function formatUnresolvedReasons(unresolvedByReason = {}) {
  const entries = Object.entries(unresolvedByReason);
  if (entries.length === 0) {
    return 'None';
  }

  return entries
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([reason, count]) => `${reason}: ${count}`)
    .join('\n');
}

function pickTopBreakdownEntry(breakdown = {}, minimumCountable = 20) {
  return Object.entries(breakdown)
    .filter(([, stats]) => (stats.countable || 0) >= minimumCountable)
    .sort((a, b) => {
      if (b[1].winRate !== a[1].winRate) {
        return b[1].winRate - a[1].winRate;
      }
      return b[1].countable - a[1].countable;
    })[0] || null;
}

function stripGeneratedAt(value) {
  if (Array.isArray(value)) {
    return value.map(stripGeneratedAt);
  }
  if (!value || typeof value !== 'object') {
    return value;
  }
  const next = {};
  for (const [key, entry] of Object.entries(value)) {
    if (key === 'generatedAt') {
      continue;
    }
    next[key] = stripGeneratedAt(entry);
  }
  return next;
}

function computeRecapHash(summary) {
  const batch = summary.batch || summary;
  const overallSummary = summary.overall || summary;
  const slateDate = getPrimarySlateDate(summary);
  const slateView =
    (slateDate && batch.byGameDate?.[slateDate]) ||
    (slateDate && overallSummary.byGameDate?.[slateDate]) ||
    batch;
  const payload = {
    slateDate,
    slateView: stripGeneratedAt(slateView),
    overall: stripGeneratedAt(overallSummary?.uniqueLineLevel || {}),
  };
  const json = JSON.stringify(payload);
  let hash = 0;
  for (let i = 0; i < json.length; i += 1) {
    hash = ((hash << 5) - hash + json.charCodeAt(i)) | 0;
  }
  return `${hash}`;
}

function saveLastRecap(record) {
  fs.mkdirSync(path.dirname(LAST_RECAP_FILE), { recursive: true });
  fs.writeFileSync(LAST_RECAP_FILE, JSON.stringify(record, null, 2));
}

function getPrimarySlateDate(summary) {
  const batch = summary.batch || summary;
  if (batch.primaryGameDate) {
    return batch.primaryGameDate;
  }

  if (Array.isArray(batch.gameDates) && batch.gameDates.length > 0) {
    return batch.gameDates[batch.gameDates.length - 1];
  }

  const overall = summary.overall || {};
  if (overall.primaryGameDate) {
    return overall.primaryGameDate;
  }

  if (Array.isArray(overall.gameDates) && overall.gameDates.length > 0) {
    return overall.gameDates[overall.gameDates.length - 1];
  }

  return null;
}

function getSlateLabel(summary) {
  const dateValue = getPrimarySlateDate(summary) || summary.generatedAt;
  const date = new Date(dateValue);
  return date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

function buildRecapEmbed(summary) {
  const batch = summary.batch || summary;
  const overallSummary = summary.overall || summary;
  const slateDate = getPrimarySlateDate(summary);
  const slateView =
    (slateDate && batch.byGameDate?.[slateDate]) ||
    (slateDate && overallSummary.byGameDate?.[slateDate]) ||
    batch;
  const primary =
    slateView.uniqueLineLevel ||
    batch.uniqueLineLevel ||
    overallSummary.uniqueLineLevel ||
    summary;
  const raw =
    slateView.alertLevel ||
    batch.alertLevel ||
    overallSummary.alertLevel ||
    summary;
  const overall = overallSummary.uniqueLineLevel || null;
  const slateLabel = getSlateLabel(summary);
  const topStatType = pickTopBreakdownEntry(primary.byStatType, 12);
  const topScoreBand = pickTopBreakdownEntry(primary.byScoreBand, 20);

  const embed = new EmbedBuilder()
    .setColor(0x4caf50)
    .setTitle('Daily Prop Grading Recap')
    .setDescription(`Slate date: **${slateLabel}**`)
    .addFields(
      {
        name: 'Overall',
        value: [
          `Newly graded: **${batch.newlyGraded || 0}**`,
          `Unique lines: **${primary.gradedCount}**`,
          `Record: **${primary.win}-${primary.loss}-${primary.push}**`,
          `Voids: **${primary.void}** | Unresolved: **${primary.unresolved}**`,
          `Hit rate: **${primary.winRate}%** on ${primary.countable} countable plays`,
        ].join('\n'),
        inline: false,
      },
      {
        name: 'Sides',
        value: [
          `Over: ${formatSideRecord(primary.bySide.over)}`,
          `Under: ${formatSideRecord(primary.bySide.under)}`,
        ].join('\n'),
        inline: false,
      },
      {
        name: 'Tiers',
        value: [
          `Best Bet: ${formatSideRecord(primary.byTier.best_bet)}`,
          `Watchlist: ${formatSideRecord(primary.byTier.watchlist)}`,
        ].join('\n'),
        inline: false,
      },
      {
        name: 'Deduping',
        value: [
          `Raw alerts graded: **${raw.gradedCount}**`,
          `Duplicate exact-line alerts removed: **${slateView.duplicateAlertsRemoved || 0}**`,
        ].join('\n'),
        inline: false,
      },
    )
    .setTimestamp();

  if (topStatType || topScoreBand) {
    const lines = [];
    if (topStatType) {
      const [name, stats] = topStatType;
      lines.push(`Top stat type: **${name}** at **${stats.winRate}%** on ${stats.countable} plays`);
    }
    if (topScoreBand) {
      const [band, stats] = topScoreBand;
      lines.push(`Top score band: **${band}** at **${stats.winRate}%** on ${stats.countable} plays`);
    }

    embed.addFields({
      name: 'Breakdowns',
      value: lines.join('\n'),
      inline: false,
    });
  }

  if (overall) {
    embed.addFields({
      name: 'All-Time',
      value: [
        `Unique lines: **${overall.gradedCount}**`,
        `Record: **${overall.win}-${overall.loss}-${overall.push}**`,
        `Hit rate: **${overall.winRate}%** on ${overall.countable} countable plays`,
      ].join('\n'),
      inline: false,
    });
  }

  if ((primary.unresolved || 0) > 0) {
    embed.addFields({
      name: 'Unresolved Reasons',
      value: formatUnresolvedReasons(primary.unresolvedByReason),
      inline: false,
    });
  }

  return embed;
}

async function postRecap(summary) {
  if (!process.env.DISCORD_TOKEN || !GRADING_CHANNEL_ID) {
    throw new Error('DISCORD_TOKEN and a grading channel id are required for recap posting.');
  }

  const client = new Client({ intents: [GatewayIntentBits.Guilds] });

  try {
    console.log('[recap] Connecting to Discord...');
    const readyPromise = new Promise((resolve) => {
      client.once('ready', resolve);
      client.once('clientReady', resolve);
    });

    await client.login(process.env.DISCORD_TOKEN);
    await readyPromise;
    console.log(`[recap] Connected as ${client.user?.tag || 'unknown'}. Fetching grading channel...`);
    const channel = await client.channels.fetch(GRADING_CHANNEL_ID);
    if (!channel || typeof channel.send !== 'function') {
      throw new Error('Configured grading channel is not a text channel.');
    }

    console.log('[recap] Sending recap embed...');
    await channel.send({ embeds: [buildRecapEmbed(summary)] });
    console.log('[recap] Recap embed sent.');
  } finally {
    client.destroy();
  }
}

async function main() {
  if (!fs.existsSync(SUMMARY_FILE)) {
    throw new Error('reports/gradingSummary.json not found. Run grading first.');
  }

  const summary = loadSummaryFromDisk();
  await postRecap(summary);
  saveLastRecap({
    slateDate: getPrimarySlateDate(summary) || 'unknown',
    recapHash: computeRecapHash(summary),
    postedAt: new Date().toISOString(),
  });
  console.log(`[recap] Posted recap for slate ${getPrimarySlateDate(summary) || 'unknown'}.`);
}

main().catch((error) => {
  console.error('[recap] Failed:', error.message);
  process.exit(1);
});
