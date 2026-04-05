import { useState, useCallback, useEffect } from 'react';
import SearchBar from './components/SearchBar.jsx';
import PipelineProgress from './components/PipelineProgress.jsx';
import EntityTable from './components/EntityTable.jsx';
import ExportButtons from './components/ExportButtons.jsx';
import { runAgenticSearch } from './lib/agent.js';
import './App.css';

export default function App() {
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem('anthropic_key') || '');
  const [keyReady, setKeyReady] = useState(() => !!sessionStorage.getItem('anthropic_key'));
  const [serperKey, setSerperKey] = useState(() => sessionStorage.getItem('serper_key') || '');
  const [location, setLocation] = useState(() => sessionStorage.getItem('user_location') || '');
  const [steps, setSteps] = useState([]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [running, setRunning] = useState(false);

  const saveKey = () => {
    const k = apiKey.trim();
    if (k) {
      sessionStorage.setItem('anthropic_key', k);
      setKeyReady(true);
    }
  };

  const clearKey = () => {
    sessionStorage.removeItem('anthropic_key');
    setApiKey('');
    setKeyReady(false);
  };

  const saveSerperKey = (val) => {
    setSerperKey(val);
    if (val.trim()) sessionStorage.setItem('serper_key', val.trim());
    else sessionStorage.removeItem('serper_key');
  };

  const saveLocation = (val) => {
    setLocation(val);
    if (val.trim()) sessionStorage.setItem('user_location', val.trim());
    else sessionStorage.removeItem('user_location');
  };

  const handleSearch = useCallback(async (query) => {
    const key = sessionStorage.getItem('anthropic_key') || apiKey.trim();
    if (!key) {
      setError('Please enter your Anthropic API key above.');
      return;
    }

    setSteps([]);
    setResult(null);
    setError(null);
    setRunning(true);

    try {
      const data = await runAgenticSearch(query, {
        apiKey: key,
        serperKey: serperKey.trim() || undefined,
        location: location.trim() || undefined,
        onStep: (step) => setSteps(prev => {
          const idx = prev.findIndex(s => s.id === step.id);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = step;
            return next;
          }
          return [...prev, step];
        }),
      });
      setResult(data);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setRunning(false);
    }
  }, [apiKey, serperKey, location]);

  return (
    <div className="app">
      <header className="header">
        <span className="logo">agent<span className="dim">//</span>search</span>
        <span className="badge">beta</span>
      </header>

      <main className="main">
        <div className="key-card">
          <span className="key-card-label">API Key</span>
          <div className="key-input-wrap">
            <input
              className="key-input"
              type="password"
              placeholder="sk-ant-api..."
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && saveKey()}
            />
            <button className="key-btn" onClick={saveKey}>Save</button>
            <button className="key-btn" onClick={clearKey}>Clear</button>
          </div>
          <span className={`key-status ${keyReady ? '' : 'missing'}`}>
            {keyReady ? '✓ ready' : '⚠ not set'}
          </span>
        </div>

        <div className="key-card">
          <span className="key-card-label">Serper</span>
          <div className="key-input-wrap">
            <input
              className="key-input"
              type="password"
              placeholder="serper.dev key (optional — faster search with location support)"
              value={serperKey}
              onChange={e => saveSerperKey(e.target.value)}
            />
          </div>
          <span className={`key-status ${serperKey.trim() ? '' : 'missing'}`}>
            {serperKey.trim() ? '✓ set' : 'optional'}
          </span>
        </div>

        <div className="key-card">
          <span className="key-card-label">Location</span>
          <div className="key-input-wrap">
            <input
              className="key-input"
              type="text"
              placeholder="e.g. Amherst, MA (optional — improves 'near me' queries)"
              value={location}
              onChange={e => saveLocation(e.target.value)}
            />
          </div>
        </div>

        <SearchBar onSearch={handleSearch} disabled={running} />

        {error && <div className="error-box">// error: {error}</div>}

        {(running || steps.length > 0) && !result && (
          <PipelineProgress steps={steps} running={running} />
        )}

        {result && (
          <>
            <div className="meta-row">
              <span className="meta-item">query: <b>"{result.query}"</b></span>
              <span className="meta-item">elapsed: <b>{(result.elapsed / 1000).toFixed(1)}s</b></span>
              <span className="meta-item">sources: <b>{result.sources.length}</b></span>
              <span className="meta-item">entity type: <b>{result.schema.entity_type}</b></span>
            </div>
            <EntityTable schema={result.schema} entities={result.entities} />
            <ExportButtons schema={result.schema} entities={result.entities} />
          </>
        )}
      </main>
    </div>
  );
}
