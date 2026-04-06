/**
 * Vercel Serverless Function — /api/search
 *
 * Runs the full agentic search pipeline server-side.
 * API keys come from Vercel Environment Variables (never exposed to the browser).
 * Streams pipeline progress to the client via Server-Sent Events.
 */

import { runAgenticSearch } from '../frontend/src/lib/agent.js';

export const config = { maxDuration: 60 };

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { query, location } = req.body || {};
  if (!query || typeof query !== 'string' || query.trim().length < 2) {
    return res.status(400).json({ error: 'query is required (min 2 chars)' });
  }

  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
  });

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  try {
    const result = await runAgenticSearch(query, {
      apiKey: process.env.ANTHROPIC_API_KEY,
      serperKey: process.env.SERPER_API_KEY,
      location: location || undefined,
      onStep: (step) => send('step', step),
    });
    send('result', result);
  } catch (err) {
    send('error', { message: err.message || String(err) });
  }

  res.end();
}
