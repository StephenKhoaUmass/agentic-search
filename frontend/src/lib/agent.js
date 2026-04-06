/**
 * agent.js — Core agentic search pipeline
 *
 * Pipeline stages:
 *   1. planSchema()      → domain-specific column definitions + search queries
 *   2. searchWeb()       → Serper.dev or Claude web_search fallback
 *      + fetchPlacesRef()→ single Serper Places call for authoritative data
 *   3. scrapePages()     → fetch page content via Jina Reader (parallel, no LLM)
 *   4. extractEntities() → LLM-powered structured extraction with source attribution
 *   5. enrichEntities()  → fuzzy merge → Places correction → scoring → filter
 */

const CLAUDE_MODEL      = 'claude-sonnet-4-20250514';
const MAX_CONTENT_CHARS = 24000;
const PAGE_CHAR_LIMIT   = 6000;
const MAX_TOKENS        = { schema: 1500, search: 5000, extract: 12000 };

// Schema column key → Serper Places API field (for cross-referencing)
const PLACES_COL_MAP = [
  { colPattern: /^rating$|^score$/i,                          field: 'rating',      mode: 'prefer' },
  { colPattern: /review.*(count|num)|num.*review|^reviews$/i, field: 'ratingCount', mode: 'max' },
  { colPattern: /^address$|^location$/i,                      field: 'address',     mode: 'fill' },
  { colPattern: /phone/i,                                     field: 'phoneNumber', mode: 'fill' },
  { colPattern: /price/i,                                     field: 'priceLevel',  mode: 'fill' },
];

// ─── Main pipeline orchestrator ──────────────────────────────────────────────

export async function runAgenticSearch(query, { onStep, apiKey, serperKey, location } = {}) {
  apiKey    = apiKey    || import.meta.env?.VITE_ANTHROPIC_KEY;
  serperKey = serperKey || import.meta.env?.VITE_SERPER_KEY;

  const t0 = Date.now();
  const timers = {};

  const step = (id, text, status, meta) => {
    if (status === 'running') timers[id] = Date.now();
    const elapsed = status === 'done' && timers[id] ? Date.now() - timers[id] : null;
    onStep?.({ id, text, status, meta, elapsed });
  };

  step('plan', 'Planning extraction schema…', 'running');
  const schema = await planSchema(query, { apiKey, location });
  step('plan', `Schema ready: ${schema.entity_type} · ${schema.columns.length} columns`, 'done',
    `Queries: ${schema.search_queries.slice(0,2).join(' | ')}`);

  step('search', 'Discovering web sources…', 'running');
  const sources = await searchWeb(query, schema, { apiKey, serperKey, location });
  const placesRef = await fetchPlacesRef(query, schema, serperKey, location);
  step('search', `Found ${sources.length} sources` + (placesRef.length ? ` · ${placesRef.length} Places refs` : ' · Places: unavailable'), 'done',
    sources.slice(0,3).map(s => s.title).filter(Boolean).join(', '));

  step('scrape', 'Fetching page content…', 'running');
  const pages = await scrapePages(sources);
  const totalChars = pages.reduce((s, p) => s + (p.content?.length ?? 0), 0);
  step('scrape', `Processed ${pages.length} pages`, 'done', `~${Math.round(totalChars / 1000)}k chars extracted`);

  step('extract', 'Extracting structured entities…', 'running');
  const rawEntities = await extractEntities(pages, schema, query, { apiKey });
  step('extract', `Extracted ${rawEntities.length} raw records`, 'done', 'merging & scoring…');

  step('enrich', 'Merging entities, cross-referencing Places, scoring…', 'running');
  const entities = enrichEntities(rawEntities, schema, placesRef);
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
  - Restaurants/food: cuisine, price_range, rating, review_count, address, phone
  - Software/tools: license (e.g. MIT, Apache 2.0, GPL), primary_language (e.g. Java, Python), github_stars (number), category, website
  - People: role, organization, expertise, location, social_url
- Column keys: snake_case. type "tags" = array of strings (e.g. technologies, categories)
- search_queries: EXACTLY 4 queries, each targeting a DIFFERENT source type for maximum diversity:
  1. A broad aggregator/listicle query (e.g. "top X in Y 2025", "best X for Y")
  2. A structured data query targeting comparison tables, directories, or data-rich sources (e.g. "X comparison chart features", "X vs Y vs Z", "awesome-X github", "site:github.com topic:X stars")
  3. A specific/niche query from a different angle (e.g. community forums, industry reports, alternative perspectives)
  4. A curated/directory query (e.g. "X funded by Y combinator", "X directory list", "awesome-X curated")
  Goal: each query should find DIFFERENT domains. Avoid queries that all return the same blog posts.
- extraction_prompt: 2-3 sentence instructions covering:
  1. What qualifies as an entity (be STRICT: if the query says "startups", exclude established corporations; if it says "open source", exclude proprietary tools)
  2. How to handle the domain-specific fields`,
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

  for (const q of queries.slice(0, 4)) {
    const body = { q, num: 10 };
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

/**
 * Single Serper Places call — returns Google's authoritative entity data
 * (rating, ratingCount, address, phoneNumber, priceLevel) for cross-referencing.
 * Uses 1 Serper credit. Returns [] on failure (CORS, no key, etc.).
 */
async function fetchPlacesRef(query, schema, serperKey, location) {
  if (!serperKey) return [];

  // Only worth calling if schema has columns we can cross-reference
  const hasMappable = schema.columns.some(col =>
    PLACES_COL_MAP.some(pm => pm.colPattern.test(col.key))
  );
  if (!hasMappable) return [];

  try {
    // Use the raw query + location for the most stable Places results.
    // Schema-generated search queries vary between runs and may not match Places well.
    let q = query;
    if (location) q = q.replace(/\bnear me\b|\bnearby\b/gi, location);

    const body = { q };
    if (location) body.location = location;

    const res = await fetch('https://google.serper.dev/places', {
      method: 'POST',
      headers: { 'X-API-KEY': serperKey, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) return [];
    const data = await res.json();
    return data.places || [];
  } catch {
    return [];
  }
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
    temperature: 0,
    system: `You are a precision data extraction agent. ${schema.extraction_prompt}

Extract entities from the content and output ONLY a valid JSON array (no markdown, no explanation):
[${JSON.stringify(exampleObj, null, 2)}]

Column definitions:
${colsDesc}

STRICT rules:
- Extract ALL distinct entities mentioned across ALL source documents — be thorough
- IMPORTANT: distribute extraction across EVERY source, not just the first few. Each source should contribute entities
- CRITICAL: only extract entities that are DIRECTLY about the query topic and match ALL qualifiers:
  - "AI startups in healthcare" → only healthcare startups. Skip large corporations (Google Health, Microsoft), non-healthcare companies, and entities mentioned only as cross-industry comparisons
  - "open source database tools" → only open-source tools. Skip proprietary/commercial-only tools
  - If an entity is mentioned as a comparison, analogy, or contrast to the main topic (e.g., "unlike Company X in legal tech"), do NOT extract it
- When the same entity appears in multiple sources, create a SEPARATE record for each source occurrence (each with its own source_url). This enables cross-source validation — duplicates will be merged later
- For number fields: extract from any textual format (e.g., "4.6 stars" → 4.6, "1,523 reviews" → 1523, "$$$" → 3, "$25-35" → 30, "47.2k stars" → 47200)
- Use null ONLY when a field's data is genuinely not present — do NOT leave a field null if the source mentions it in any format
- NEVER invent or hallucinate values not supported by the source text
- source_url MUST be copied verbatim from a "=== SOURCE: url ===" header
- source_title MUST match the "Title:" field under that source header
- type "tags" → JSON array of strings
- type "url" → full https:// URL or null
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

// ─── Stage 5: Enrichment (fuzzy merge → Places correction → score → filter) ─

function enrichEntities(entities, schema, placesRef = []) {
  // Step 1: Fuzzy-merge entities by normalized name
  const groups = [];

  for (const e of entities) {
    const name = normalizeName(e.name);
    if (!name) continue;

    let matched = false;
    for (const group of groups) {
      if (fuzzyMatch(name, group.key)) {
        group.records.push(e);
        if ((e.name || '').length > group.canonicalName.length) group.canonicalName = e.name;
        if (name.length > group.key.length) group.key = name;
        matched = true;
        break;
      }
    }

    if (!matched) {
      groups.push({ key: name, canonicalName: e.name || '', records: [e] });
    }
  }

  // Step 2: Build merged entities
  const merged = groups.map(group => {
    const records = group.records;
    const sourceUrls = new Set(records.map(r => r.source_url).filter(Boolean));
    const result = { name: group.canonicalName, _sourceUrls: sourceUrls };

    for (const col of schema.columns) {
      if (col.key === 'name') continue;
      if (col.key === 'source_url') { result.source_url = records[0].source_url; continue; }

      const values = records
        .map(r => r[col.key])
        .filter(v => v != null && v !== '' && v !== 'null');

      if (values.length === 0) { result[col.key] = null; continue; }

      if (col.type === 'number') {
        const nums = values.map(Number).filter(n => !isNaN(n) && n > 0);
        if (nums.length === 0) { result[col.key] = null; continue; }
        if (/rating|score/i.test(col.key)) {
          nums.sort((a, b) => a - b);
          result[col.key] = nums[Math.floor(nums.length / 2)];
        } else {
          result[col.key] = Math.max(...nums);
        }
      } else if (col.type === 'tags') {
        const allTags = new Set();
        for (const v of values) {
          if (Array.isArray(v)) v.forEach(t => allTags.add(t));
          else if (typeof v === 'string') v.split(/[,;]+/).map(t => t.trim()).filter(Boolean).forEach(t => allTags.add(t));
        }
        result[col.key] = [...allTags];
      } else {
        result[col.key] = values.reduce((best, v) =>
          String(v).length > String(best).length ? v : best, values[0]);
      }
    }

    result.source_title = records[0]?.source_title || '';
    return result;
  });

  // Step 3: Cross-reference with Places data to correct/fill values
  if (placesRef.length > 0) {
    const colMappings = [];
    for (const col of schema.columns) {
      for (const pm of PLACES_COL_MAP) {
        if (pm.colPattern.test(col.key)) {
          colMappings.push({ colKey: col.key, placeField: pm.field, mode: pm.mode });
          break;
        }
      }
    }

    for (const entity of merged) {
      const place = placesRef.find(p =>
        fuzzyMatch(normalizeName(entity.name), normalizeName(p.title || ''))
      );
      if (!place) continue;

      for (const m of colMappings) {
        const placeVal = place[m.placeField];
        if (placeVal == null || placeVal === '') continue;

        const curVal = entity[m.colKey];
        const curEmpty = curVal == null || curVal === '' || curVal === 'null';

        if (m.mode === 'fill' && curEmpty) {
          entity[m.colKey] = placeVal;
        } else if (m.mode === 'max') {
          const pv = Number(placeVal), cv = Number(curVal);
          if (!isNaN(pv) && pv > 0) {
            if (curEmpty || isNaN(cv) || pv > cv) entity[m.colKey] = pv;
          }
        } else if (m.mode === 'prefer') {
          const pv = Number(placeVal);
          if (!isNaN(pv) && pv > 0) entity[m.colKey] = pv;
        }
      }
    }
  }

  // Step 4: Detect quality signal columns from schema
  const ratingCols = schema.columns.filter(c =>
    c.type === 'number' && /rating|score/i.test(c.key)
  );
  const popularityCols = schema.columns.filter(c =>
    c.type === 'number' && /review|count|votes|popularity|funding|revenue|users|downloads/i.test(c.key)
  );
  const starsCols = schema.columns.filter(c =>
    c.type === 'number' && /stars/i.test(c.key) && !ratingCols.includes(c) && !popularityCols.includes(c)
  );
  const qualityCols = [...ratingCols, ...popularityCols, ...starsCols];
  const hasQualitySignals = qualityCols.length > 0;

  // Check globally: does ANY entity have quality data?
  // If no entity has quality data, don't penalize — the sources just didn't have it.
  const anyEntityHasQuality = hasQualitySignals && merged.some(e =>
    qualityCols.some(c => { const v = Number(e[c.key]); return !isNaN(v) && v > 0; })
  );

  // Step 5: Score each merged entity
  const scored = merged.map(e => {
    const sourceUrls = e._sourceUrls || new Set();
    const sourceCount = sourceUrls.size;
    const sourceDomains = new Set(
      [...sourceUrls].map(u => { try { return new URL(u).hostname.replace(/^(www|m|mobile)\./, ''); } catch { return u; } })
    );
    delete e._sourceUrls;

    // Normalize tags
    schema.columns.filter(c => c.type === 'tags').forEach(c => {
      if (typeof e[c.key] === 'string') {
        e[c.key] = e[c.key].split(/[,;]+/).map(t => t.trim()).filter(Boolean);
      }
    });

    const domainCols = schema.columns.filter(c => c.key !== 'source_url' && c.key !== 'name');
    const filled = domainCols.filter(c => {
      const v = e[c.key];
      return v != null && v !== '' && v !== 'null' && !(Array.isArray(v) && v.length === 0);
    }).length;
    const completeness = filled / Math.max(domainCols.length, 1);

    // Weighted quality: popularity columns count 2x over rating columns
    let qualityWeightedSum = 0, qualityWeightTotal = 0;

    for (const c of ratingCols) {
      const v = Number(e[c.key]);
      if (!isNaN(v) && v > 0) { qualityWeightedSum += 1 * Math.min(v / 5, 1); qualityWeightTotal += 1; }
    }
    for (const c of popularityCols) {
      const v = Number(e[c.key]);
      if (!isNaN(v) && v > 0) { qualityWeightedSum += 2 * Math.min(Math.log10(v + 1) / 4, 1); qualityWeightTotal += 2; }
    }
    for (const c of starsCols) {
      const v = Number(e[c.key]);
      if (!isNaN(v) && v > 0) {
        if (v <= 5) { qualityWeightedSum += 1 * (v / 5); qualityWeightTotal += 1; }
        else { qualityWeightedSum += 2 * Math.min(Math.log10(v + 1) / 4, 1); qualityWeightTotal += 2; }
      }
    }
    const qualityScore = qualityWeightTotal > 0 ? qualityWeightedSum / qualityWeightTotal : 0;
    const hasQualityData = qualityWeightTotal > 0;

    const diversityBonus = Math.min(sourceDomains.size - 1, 3) * 0.05;

    let score;
    if (anyEntityHasQuality) {
      // Some entities have quality data: reward those that do, penalize those that don't
      if (hasQualityData) {
        score = 0.15 * completeness + 0.55 * qualityScore + 0.30 * Math.min(sourceCount / 3, 1);
      } else {
        score = completeness * 0.35;
      }
    } else {
      // No entity has quality data (or schema has no quality columns): score on completeness + sources
      score = 0.5 * completeness + 0.5 * Math.min(sourceCount / 3, 1);
    }
    score += diversityBonus;

    return {
      ...e,
      _sources: sourceCount,
      _confidence: score >= 0.5 ? 'high' : score >= 0.25 ? 'mid' : 'low',
      _score: Math.round(score * 100) / 100,
    };
  });

  // Step 6: Filter near-empty entities
  return scored
    .filter(e => {
      const domainCols = schema.columns.filter(c => c.key !== 'name' && c.key !== 'source_url');
      const filled = domainCols.filter(c => {
        const v = e[c.key];
        return v != null && v !== '' && v !== 'null' && !(Array.isArray(v) && v.length === 0);
      }).length;
      return filled >= 2;
    })
    .sort((a, b) => b._score - a._score);
}

// ─── Name Matching Helpers ────────────────────────────────────────────────────

function normalizeName(name) {
  const s = String(name || '').toLowerCase().trim()
    .replace(/['''\u2019`]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return (!s || s === 'null') ? null : s;
}

function stemToken(t) {
  if (t.length > 3 && t.endsWith('s')) return t.slice(0, -1);
  return t;
}

function fuzzyMatch(a, b) {
  if (a === b) return true;
  if (a.includes(b) || b.includes(a)) return true;

  const tokA = new Set(a.split(/\s+/).filter(t => t.length > 1).map(stemToken));
  const tokB = new Set(b.split(/\s+/).filter(t => t.length > 1).map(stemToken));
  const overlap = [...tokA].filter(t => tokB.has(t));

  if (overlap.length < 2) return false;
  const containment = Math.max(overlap.length / tokA.size, overlap.length / tokB.size);
  return containment >= 0.7;
}

// ─── Shared LLM Client ────────────────────────────────────────────────────────

async function callClaude({ apiKey, system, messages, maxTokens = 2000, useWebSearch = false, temperature }) {
  const body = {
    model: CLAUDE_MODEL,
    max_tokens: maxTokens,
    system,
    messages,
  };
  if (temperature != null) body.temperature = temperature;

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
