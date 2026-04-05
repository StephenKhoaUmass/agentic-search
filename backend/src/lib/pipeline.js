/**
 * backend/src/lib/pipeline.js
 *
 * Server-side mirror of frontend/src/lib/agent.js.
 * Uses ANTHROPIC_API_KEY from env instead of browser headers.
 *
 * Keeping this as a separate file (rather than sharing) makes deployment
 * simpler — no need for a monorepo build setup.
 */

// Re-export the same pipeline but override the API key injection
export { runAgenticSearch } from '../../../frontend/src/lib/agent.js';
// In a real deployment, copy agent.js here and use process.env.ANTHROPIC_API_KEY
// directly in callClaude() instead of accepting it as a parameter.
