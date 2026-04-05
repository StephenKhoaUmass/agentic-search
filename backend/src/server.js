/**
 * backend/src/server.js
 *
 * Optional Express server that proxies Anthropic API calls server-side,
 * keeping your API key out of the browser.
 *
 * Usage:
 *   ANTHROPIC_API_KEY=sk-ant-... npm start
 *
 * Endpoints:
 *   POST /api/search   { query: string }  → SearchResult
 *   GET  /api/health
 */

import express from 'express';
import cors from 'cors';
import 'dotenv/config';
import { runAgenticSearch } from './lib/pipeline.js';

const app = express();
const PORT = process.env.PORT || 3001;

app.use(cors({ origin: process.env.FRONTEND_URL || '*' }));
app.use(express.json());

// Health check
app.get('/api/health', (req, res) => {
  res.json({ ok: true, model: 'claude-sonnet-4-20250514' });
});

// Main search endpoint — streams progress events via SSE
app.post('/api/search', async (req, res) => {
  const { query } = req.body;
  if (!query || typeof query !== 'string' || query.trim().length < 2) {
    return res.status(400).json({ error: 'query is required (min 2 chars)' });
  }

  // Use Server-Sent Events so the client sees pipeline progress in real time
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('X-Accel-Buffering', 'no');

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  try {
    const result = await runAgenticSearch(query, {
      apiKey: process.env.ANTHROPIC_API_KEY,
      serperKey: process.env.SERPER_API_KEY,
      location: req.body.location,
      onStep: (step) => send('step', step),
    });
    send('result', result);
  } catch (err) {
    send('error', { message: err.message || String(err) });
  } finally {
    res.end();
  }
});

app.listen(PORT, () => {
  console.log(`Agent Search backend running on http://localhost:${PORT}`);
  console.log(`Anthropic key: ${process.env.ANTHROPIC_API_KEY ? 'set ✓' : 'MISSING ✗'}`);
  console.log(`Serper key:    ${process.env.SERPER_API_KEY ? 'set ✓' : 'not set (using Claude web_search)'}`);
});