export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET');

  const FEEDS = [
    'https://techcrunch.com/category/venture/feed/',
    'https://news.crunchbase.com/feed/',
    'https://venturebeat.com/press-releases/feed/',
  ];

  const SEVEN_DAYS_AGO = Date.now() - 180 * 24 * 60 * 60 * 1000;

  const STAGE_PATTERNS = [
    { stage: 'Series B', pattern: /series[\s-]?b/i },
    { stage: 'Series A', pattern: /series[\s-]?a/i },
    { stage: 'Seed',     pattern: /\bseed\b/i },
  ];

  const AMOUNT_PATTERN = /\$\s*([\d,.]+)\s*(million|billion|M|B)\b/i;

  const URL_PATTERN = /https?:\/\/(?:www\.)?([\w-]+\.[\w.-]+)/;

  function parseAmount(text) {
    const m = text.match(AMOUNT_PATTERN);
    if (!m) return null;
    const num = parseFloat(m[1].replace(/,/g, ''));
    const unit = m[2].toLowerCase();
    if (unit === 'billion' || unit === 'b') return `$${num}B`;
    return `$${num}M`;
  }

  function extractCompanyUrl(text) {
    // Look for explicit "visit" or href-style URLs that aren't the news sources
    const skipDomains = ['techcrunch.com', 'crunchbase.com', 'venturebeat.com', 'twitter.com', 'linkedin.com', 'facebook.com', 'google.com'];
    const matches = [...text.matchAll(/https?:\/\/(?:www\.)?([\w-]+\.[\w.-]+[^\s"<>]*)/gi)];
    for (const m of matches) {
      const domain = m[1].split('/')[0].toLowerCase();
      if (!skipDomains.some(s => domain.endsWith(s))) {
        return m[0].split(/[\s"<>]/)[0];
      }
    }
    return null;
  }

  function parseFeedXml(xml) {
    const items = [];
    const itemRegex = /<item>([\s\S]*?)<\/item>/gi;
    let itemMatch;
    while ((itemMatch = itemRegex.exec(xml)) !== null) {
      const block = itemMatch[1];
      const get = (tag) => {
        const m = block.match(new RegExp(`<${tag}[^>]*><!\\[CDATA\\[([\\s\\S]*?)\\]\\]><\\/${tag}>|<${tag}[^>]*>([^<]*)<\\/${tag}>`, 'i'));
        return m ? (m[1] || m[2] || '').trim() : '';
      };
      items.push({
        title: get('title'),
        description: get('description'),
        link: get('link'),
        pubDate: get('pubDate'),
      });
    }
    return items;
  }

  const results = [];
  const seen = new Set();

  await Promise.all(
    FEEDS.map(async (url) => {
      try {
        const r = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0 (compatible; DealFlowBot/1.0)' } });
        if (!r.ok) return;
        const xml = await r.text();
        const items = parseFeedXml(xml);

        for (const item of items) {
          const pubDate = item.pubDate ? new Date(item.pubDate).getTime() : 0;
          if (pubDate && pubDate < SEVEN_DAYS_AGO) continue;

          const combined = `${item.title} ${item.description}`;

          let stage = null;
          for (const { stage: s, pattern } of STAGE_PATTERNS) {
            if (pattern.test(combined)) { stage = s; break; }
          }
          if (!stage) continue;

          const amount = parseAmount(combined);
          const companyUrl = extractCompanyUrl(combined) || item.link;

          // Derive company name: first capitalized phrase before "raises" / "secures" / "closes" / "announces"
          const nameMatch = item.title.match(/^(.+?)\s+(?:raises|secures|closes|announces|gets|lands|receives)/i);
          const company = nameMatch ? nameMatch[1].trim() : item.title.split(' ').slice(0, 3).join(' ');

          const key = `${company}-${stage}-${amount}`;
          if (seen.has(key)) continue;
          seen.add(key);

          results.push({ company, stage, amount: amount || 'Undisclosed', url: companyUrl, pubDate: item.pubDate, articleUrl: item.link });
        }
      } catch (e) {
        // skip failed feed
      }
    })
  );

  results.sort((a, b) => new Date(b.pubDate) - new Date(a.pubDate));

  res.status(200).json({ results, fetchedAt: new Date().toISOString() });
}
