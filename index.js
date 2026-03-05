// ============================================
// STEP 1: IMPORT YOUR TOOLS
// These are the packages you installed with npm
// ============================================

require('dotenv').config(); 
const { Client, GatewayIntentBits, EmbedBuilder } = require('discord.js'); 
const fetch = require('node-fetch');
const fs = require('fs');


const client = new Client({ intents: [GatewayIntentBits.Guilds] });


const SEEN_FILE = './seenProps.json'; 


let seenProps = fs.existsSync(SEEN_FILE)
  ? JSON.parse(fs.readFileSync(SEEN_FILE)) 
  : {}; // File doesn't exist yet → start empty

// This function saves the updated seenProps object back to the file
// We call it every time a new prop is posted
function saveSeenProps() {
  fs.writeFileSync(SEEN_FILE, JSON.stringify(seenProps, null, 2));
}


async function fetchPrizePicksProps() {

  // The PrizePicks API URL with filters:
  // league_id=7 → NBA only
  // per_page=250 → grab up to 250 props at once
  // single_stat=true → regular lines only (no combos)
  const url = 'https://api.prizepicks.com/projections?league_id=7&per_page=250&single_stat=true';

  const res = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0' }
  });

  const data = await res.json();

  // Grab the Discord channel where we'll post props
  // process.env.CHANNEL_ID pulls the value from your .env file
  const channel = await client.channels.fetch(process.env.CHANNEL_ID);


  const playerMap = {};

  for (const item of data.included || []) {
    if (item.type === 'new_player') {
      // Only grab player entries (there are other types in here too)
      playerMap[item.id] = item.attributes.display_name;
      // Store it like: { "12345": "LeBron James", "67890": "Stephen Curry" }
    }
  }

  for (const proj of data.data || []) {

    const attr = proj.attributes; 
    const propId = proj.id;
    // DUPLICATE CHECK
    if (seenProps[propId]) continue;

    seenProps[propId] = true; 
    saveSeenProps(); 

    const playerId = proj.relationships?.new_player?.data?.id;
    const playerName = playerMap[playerId] ?? 'Unknown Player';

    const embed = new EmbedBuilder()
      .setColor(0x00d4a3) // PrizePicks green color (hex code)
      .setTitle(`🏀 New PrizePicks NBA Prop`) // Bold title at the top
      .addFields(

        { name: 'Player',     value: playerName,           inline: true },
        { name: 'Stat',       value: attr.stat_type,       inline: true }, 
        { name: 'Line',       value: `${attr.line_score}`, inline: true },
        { name: 'Game',       value: attr.description,     inline: true }, 
        { name: 'Start Time', value: attr.start_time
            ? new Date(attr.start_time).toLocaleString('en-US', {
                timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', hour12: true })
            : 'TBD',                                       inline: true }, 
      )
      .setFooter({ text: 'PrizePicks NBA' }) 
      .setTimestamp(); 

    await channel.send({ embeds: [embed] });

  }
}


client.once('ready', () => {
  console.log(`Bot is online as ${client.user.tag}`); 

  fetchPrizePicksProps(); 


  setInterval(fetchPrizePicksProps, 5 * 60 * 1000);
});


client.login(process.env.DISCORD_TOKEN);