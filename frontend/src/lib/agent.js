/**
 * agent.js — Core agentic search pipeline
 *
 * Pipeline stages:
 *   1. planSchema()      → domain-specific column definitions + search queries
 *   2. searchWeb()       → Serper.dev (if key provided) or Claude web_search fallback
 *   3. scrapePages()     → fetch page content via Jina Reader (parallel, no LLM)
 *   4. extractEntities() → LLM-powered structured extraction with source attribution
 *   5. enrichEntities()  → dedup, quality-aware scoring, tag normalization, sorting
 */

const CLAUDE_MODEL      = 'claude-sonnet-4-20250514';
const MAX_CONTENT_CHARS = 24000;
const PAGE_CHAR_LIMIT   = 5000;
const MAX_TOKENS        = { schema: 1500, search: 5000, extract: 8192 };

// ─── Main pipeline orchestrator ──────────────────────────────────────────────

export async function runAgenticSearch(query, { onStep, apiKey, serperKey, location } = {}) {
  const t0 = Date.now();
  const timers = {};

  const step = (id, text, status, meta) => {
    if (status === 'running') timers[id] = Date.now();
    const elapsed = status === 'done' && timers[id] ? Date.now() - timers[id] : null;
    onStep?.({ id, text, status, meta, elapsed });
  };

  // Stage 1: Schema
  step('plan', 'Planning extraction schema…', 'running');
  const schema = await planSchema(query, { apiKey, location });
  step('plan', `Schema ready: ${schema.entity_type} · ${schema.columns.length} columns`, 'done',
    `Queries: ${schema.search_queries.slice(0,2).join(' | ')}`);

  // Stage 2: Web search
  step('search', 'Discovering web sources…', 'running');
  const sources = await searchWeb(query, schema, { apiKey, serperKey, location });
  step('search', `Found ${sources.length} sources`, 'done',
    sources.slice(0,3).map(s => s.title).filter(Boolean).join(', '));

  // Stage 3: Scrape via Jina Reader (parallel HTTP, no LLM)
  step('scrape', 'Fetching page content…', 'running');
  const pages = await scrapePages(sources);
  const totalChars = pages.reduce((s, p) => s + (p.content?.length ?? 0), 0);
  step('scrape', `Processed ${pages.length} pages`, 'done', `~${Math.round(totalChars / 1000)}k chars extracted`);

  // Stage 4: Extract
  step('extract', 'Extracting structured entities…', 'running');
  const rawEntities = await extractEntities(pages, schema, query, { apiKey });
  step('extract', `Extracted ${rawEntities.length} raw entities`, 'done', 'applying deduplication…');

  // Stage 5: Enrich
  step('enrich', 'Scoring confidence and finalizing…', 'running');
  const entities = enrichEntities(rawEntities, schema);
  step('enrich', `Done — ${entities.length} entities ready`, 'done',
    `Total: ${((Date.now() - t0) / 1000).toFixed(1)}s`);

  return { query, schema, entities, sources, elapsed: Date.now() - t0 };
}

// ─── Stage 1: Schema Planner ──────────────────────────────────────────────────

async function planSchema(query, { apiKey, location }) {
  const locationCtx = location
    ? `\nUser location: ${location}. Replace "near me", "nearby", or location-relative phrases in search queries with "${location}".`
    : '';

  const text = await callClaude({
    apiKey,
    maxTokens: MAX_TOKENS.schema,
    system: `You are an expert data schema designer. Given a search query, design an optimal extraction schema.
Output ONLY valid JSON (no markdown, no backticks, no explanation):
{
  "entity_type": "concise description of what entities we are finding",
  "columns": [
    { "key": "name", "label": "Name", "type": "text", "description": "primary name of the entity" },
    { "key": "...", "label": "...", "type": "text|url|tags|number", "description": "..." }
  ],
  "search_queries": ["diverse query 1", "query 2", "query 3"],
  "extraction_prompt": "2-3 sentence instructions for extracting these entities accurately"
}

Rules:
- ALWAYS include these first 3 columns: name, description, source_url
- Add 3-6 domain-specific columns. ALWAYS include quantitative metrics when available. Examples:
  - Companies/startups: founded, headquarters, funding_stage, total_funding, employee_count, website, focus_area
  - Restaurants/food: cuisine, price_range, rating, review_count, neighborhood, address
  - Software/tools: license, language, github_stars, category, website, maintainer
  - People: role, organization, expertise, location, social_url
- Column keys: snake_case. type "tags" = array of strings (e.g. technologies, categories)
- search_queries: 3 diverse queries maximizing source variety (broad, specific, listicle/directory)
- extraction_prompt: domain-specific instructions on what qualifies as an entity and field semantics`,
    messages: [{ role: 'user', content: `Query: "${query}"${locationCtx}` }],
  });

  const cleaned = text.replace(/```json\n?/g, '').replace(/```\n?/g, '').trim();
  try {
    return JSON.parse(cleaned);
  } catch {
    const match = cleaned.match(/\{[\s\S]*\}/);
    if (match) return JSON.parse(match[0]);
    throw new Error(`Schema planner returned unparseable output: ${cleaned.slice(0, 200)}`);
  }
}

// ─── Stage 2: Web Search ──────────────────────────────────────────────────────

async function searchWeb(query, schema, { apiKey, serperKey, location }) {
  if (serperKey) {
    try {
      const results = await searchViaSerper(schema.search_queries || [query], serperKey, location);
      if (results.length > 0) return results;
    } catch {
      // CORS (browser) or network error — fall back to Claude web_search
    }
  }

  const text = await callClaude({
    apiKey,
    maxTokens: MAX_TOKENS.search,
    useWebSearch: true,
    system: `You are a research agent. Search the web to discover high-quality, diverse sources about the given topic.
Use ALL 3 provided search queries. After searching, output ONLY a JSON array (no markdown):
[{ "title": "...", "url": "https://...", "snippet": "brief 1-2 sentence description" }]
Goals:
- Find 6-12 sources
- Prioritize: authoritative directories, curated lists, industry databases, review sites, news
- Maximize diversity: different domains, perspectives, date ranges
- Exclude paywalled sources when possible`,
    messages: [{
      role: 'user',
      content: `Topic: "${query}"\n\nSearch queries to use:\n${schema.search_queries.map((q,i) => `${i+1}. ${q}`).join('\n')}`,
    }],
  });

  const results = extractJSON(text, []);
  if (!Array.isArray(results) || results.length === 0) {
    return [{ title: query, url: `https://www.google.com/search?q=${encodeURIComponent(query)}`, snippet: '' }];
  }
  return results.filter(r => r?.url?.startsWith('http')).slice(0, 12);
}

async function searchViaSerper(queries, apiKey, location) {
  const seen = new Set();
  const results = [];

  for (const q of queries.slice(0, 3)) {
    const body = { q, num: 8 };
    if (location) body.location = location;

    const res = await fetch('https://google.serper.dev/search', {
      method: 'POST',
      headers: { 'X-API-KEY': apiKey, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Serper ${res.status}`);
    const data = await res.json();

    for (const r of (data.organic || [])) {
      if (r.link && !seen.has(r.link)) {
        seen.add(r.link);
        results.push({ title: r.title || '', url: r.link, snippet: r.snippet || '' });
      }
    }
  }

  return results.slice(0, 15);
}

// ─── Stage 3: Page Scraper (Jina Reader — parallel, no LLM) ─────────────────

async function scrapePages(sources) {
  const results = await Promise.allSettled(
    sources.slice(0, 10).map(async (s) => {
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
          const res = await fetch(`https://r.jina.ai/${s.url}`, {
            headers: { Accept: 'text/plain' },
            signal: controller.signal,
          });
          if (!res.ok) return null;
          const text = await res.text();
          return {
            url: s.url,
            title: s.title || text.split('\n')[0]?.slice(0, 100) || 'Untitled',
            snippet: s.snippet || '',
            content: text.slice(0, PAGE_CHAR_LIMIT),
          };
        } finally {
          clearTimeout(timeout);
        }
      } catch {
        return null;
      }
    })
  );

  const pages = results
    .filter(r => r.status === 'fulfilled' && r.value && r.value.content?.length > 50)
    .map(r => r.value);

  if (pages.length === 0) {
    return sources.map(s => ({ url: s.url, title: s.title, snippet: s.snippet || '', content: s.snippet || s.title }));
  }
  return pages;
}

// ─── Stage 4: Entity Extractor ────────────────────────────────────────────────

async function extractEntities(pages, schema, query, { apiKey }) {
  const colsDesc = schema.columns
    .map(c => `  - ${c.key} (${c.type}): ${c.description}`)
    .join('\n');

  const exampleObj = Object.fromEntries(
    schema.columns.map(c => [c.key, c.type === 'tags' ? ['tag1','tag2'] : c.type === 'number' ? 0 : 'value or null'])
  );
  exampleObj.source_url   = 'https://...';
  exampleObj.source_title = 'Page Title';

  const taggedContent = pages
    .map(p => {
      let block = `=== SOURCE: ${p.url} ===\nTitle: ${p.title}`;
      if (p.snippet) block += `\nSnippet: ${p.snippet}`;
      block += `\n${p.content}`;
      return block;
    })
    .join('\n\n')
    .slice(0, MAX_CONTENT_CHARS);

  const text = await callClaude({
    apiKey,
    maxTokens: MAX_TOKENS.extract,
    system: `You are a precision data extraction agent. ${schema.extraction_prompt}

Extract entities from the content and output ONLY a valid JSON array (no markdown, no explanation):
[${JSON.stringify(exampleObj, null, 2)}]

Column definitions:
${colsDesc}

STRICT rules:
- Extract ALL distinct entities mentioned in the source content — be thorough, aim for 10-25 entities
- For each entity, fill in every field where data IS present in the source text
- For number fields: extract from any textual format (e.g., "4.6 stars" → 4.6, "1,523 reviews" → 1523, "$$$" → 3, "$25-35" → 30)
- Use null ONLY when a field's data is genuinely not present — do NOT leave a field null if the source mentions it in any format
- NEVER invent or hallucinate values not supported by the source text
- source_url MUST be copied verbatim from a "=== SOURCE: url ===" header
- source_title MUST match the "Title:" field under that source header
- type "tags" → JSON array of strings
- type "url" → full https:// URL or null
- Deduplicate: if the same entity appears in multiple sources, merge all available data into one record
- Do not skip entities just because some fields are missing — partial data is valuable`,
    messages: [{
      role: 'user',
      content: `Query: "${query}"\n\nSource Content:\n${taggedContent}\n\nExtract ${schema.entity_type}:`,
    }],
  });

  const entities = extractJSON(text, []);
  if (!Array.isArray(entities)) throw new Error('Extractor returned non-array output');
  return entities;
}

// ─── Stage 5: Enrichment (quality-aware scoring) ─────────────────────────────

function enrichEntities(entities, schema) {
  const seen = new Set();

  const ratingCols = schema.columns.filter(c =>
    c.type === 'number' && /rating|score/i.test(c.key)
  );
  const popularityCols = schema.columns.filter(c =>
    c.type === 'number' && /review|count|votes|popularity|funding|revenue|users|downloads/i.test(c.key)
  );
  const starsCols = schema.columns.filter(c =>
    c.type === 'number' && /stars/i.test(c.key) && !ratingCols.includes(c) && !popularityCols.includes(c)
  );

  const hasQualitySignals = ratingCols.length + popularityCols.length + starsCols.length > 0;

  return entities
    .filter(e => {
      const key = String(e.name || '').toLowerCase().trim();
      if (!key || key === 'null' || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map(e => {
      // Normalize tags
      schema.columns.filter(c => c.type === 'tags').forEach(c => {
        if (typeof e[c.key] === 'string') {
          e[c.key] = e[c.key].split(/[,;]+/).map(t => t.trim()).filter(Boolean);
        }
      });

      // Field completeness (0–1)
      const filled = schema.columns.filter(c => {
        const v = e[c.key];
        return v != null && v !== '' && v !== 'null' && !(Array.isArray(v) && v.length === 0);
      }).length;
      const completeness = filled / schema.columns.length;

      // Quality signals (0–1 each, averaged)
      let qualitySum = 0;
      let qualityCount = 0;

      for (const c of ratingCols) {
        const v = Number(e[c.key]);
        if (!isNaN(v) && v > 0) {
          qualitySum += Math.min(v / 5, 1);
          qualityCount++;
        }
      }

      for (const c of popularityCols) {
        const v = Number(e[c.key]);
        if (!isNaN(v) && v > 0) {
          qualitySum += Math.min(Math.log10(v + 1) / 5, 1);
          qualityCount++;
        }
      }

      for (const c of starsCols) {
        const v = Number(e[c.key]);
        if (!isNaN(v) && v > 0) {
          if (v <= 5) qualitySum += v / 5;
          else qualitySum += Math.min(Math.log10(v + 1) / 5, 1);
          qualityCount++;
        }
      }

      const qualityScore = qualityCount > 0 ? qualitySum / qualityCount : 0;

      // Composite scoring:
      // - If schema expects quality data AND entity has it → blend completeness + quality
      // - If schema expects quality data but entity is MISSING it → penalize (cap at mid)
      // - If schema has no quality columns → pure completeness
      let score;
      if (hasQualitySignals) {
        if (qualityCount > 0) {
          score = 0.35 * completeness + 0.65 * qualityScore;
        } else {
          score = completeness * 0.5;
        }
      } else {
        score = completeness;
      }

      return {
        ...e,
        _confidence: score >= 0.5 ? 'high' : score >= 0.25 ? 'mid' : 'low',
        _score: Math.round(score * 100) / 100,
      };
    })
    .sort((a, b) => b._score - a._score);
}

// ─── Shared LLM Client ────────────────────────────────────────────────────────

async function callClaude({ apiKey, system, messages, maxTokens = 2000, useWebSearch = false }) {
  const body = {
    model: CLAUDE_MODEL,
    max_tokens: maxTokens,
    system,
    messages,
  };

  if (useWebSearch) {
    body.tools = [{ type: 'web_search_20250305', name: 'web_search' }];
  }

  const headers = {
    'Content-Type': 'application/json',
    'anthropic-version': '2023-06-01',
    'anthropic-dangerous-direct-browser-access': 'true',
  };
  if (apiKey) headers['x-api-key'] = apiKey;

  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error?.message || `Anthropic API ${res.status}: ${res.statusText}`);
  }

  const data = await res.json();
  return data.content
    .filter(b => b.type === 'text')
    .map(b => b.text)
    .join('');
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function extractJSON(text, fallback) {
  const cleaned = text.replace(/```json\n?/g, '').replace(/```\n?/g, '').trim();
  const arrMatch = cleaned.match(/\[[\s\S]*\]/);
  if (arrMatch) { try { return JSON.parse(arrMatch[0]); } catch {} }
  const objMatch = cleaned.match(/\{[\s\S]*\}/);
  if (objMatch) { try { return JSON.parse(objMatch[0]); } catch {} }
  return fallback;
}
