const fs = require('fs');

async function updateArchive() {
  const feedUrl = 'http://rss.arxiv.org/rss/math.RA/';
  const proxyUrl = `https://api.rss2json.com/v1/api.json?rss_url=${encodeURIComponent(feedUrl)}`;

  try {
    console.log('Fetching live feeds...');
    // 1. Fetch live data
    const res = await fetch(proxyUrl);
    const data = await res.json();
    
    if (data.status !== 'ok') throw new Error('Failed to fetch RSS');
    
    const liveItems = data.items.map(item => ({ ...item, __source: feedUrl }));

    // 2. Read existing archive
    let archive = [];
    if (fs.existsSync('archive.json')) {
      archive = JSON.parse(fs.readFileSync('archive.json', 'utf8'));
    }

    // 3. Merge and deduplicate
    const seenLinks = new Set(archive.map(item => item.link));
    const newItems = liveItems.filter(item => !seenLinks.has(item.link));

    // 4. Save back to file if there are new items
    if (newItems.length > 0) {
      archive = [...archive, ...newItems];
      archive.sort((a, b) => new Date(b.pubDate) - new Date(a.pubDate));
      
      fs.writeFileSync('archive.json', JSON.stringify(archive, null, 2));
      console.log(`Successfully added ${newItems.length} new items to the archive.`);
    } else {
      console.log('No new items to add. Archive is up to date.');
    }
  } catch (error) {
    console.error('Error updating archive:', error);
    process.exit(1); // Tell GitHub Actions this failed
  }
}

updateArchive();
