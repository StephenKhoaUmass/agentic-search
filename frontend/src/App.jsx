import { useState, useCallback } from 'react';
import SearchBar from './components/SearchBar.jsx';
import PipelineProgress from './components/PipelineProgress.jsx';
import EntityTable from './components/EntityTable.jsx';
import ExportButtons from './components/ExportButtons.jsx';
import './App.css';

export default function App() {
  const [location, setLocation] = useState(() => sessionStorage.getItem('user_location') || '');
  const [steps, setSteps] = useState([]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [running, setRunning] = useState(false);

  const saveLocation = (val) => {
    setLocation(val);
    if (val.trim()) sessionStorage.setItem('user_location', val.trim());
    else sessionStorage.removeItem('user_location');
  };

  const handleSearch = useCallback(async (query) => {
    setSteps([]);
    setResult(null);
    setError(null);
    setRunning(true);

    try {
      const res = await fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, location: location.trim() || undefined }),
      });

      if (!res.ok && !res.headers.get('content-type')?.includes('text/event-stream')) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `Server error ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';

        for (const part of parts) {
          const lines = part.split('\n');
          let event = '', data = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) event = line.slice(7);
            else if (line.startsWith('data: ')) data = line.slice(6);
          }
          if (!event || !data) continue;

          const parsed = JSON.parse(data);

          if (event === 'step') {
            setSteps(prev => {
              const idx = prev.findIndex(s => s.id === parsed.id);
              if (idx >= 0) {
                const next = [...prev];
                next[idx] = parsed;
                return next;
              }
              return [...prev, parsed];
            });
          } else if (event === 'result') {
            setResult(parsed);
          } else if (event === 'error') {
            throw new Error(parsed.message);
          }
        }
      }
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setRunning(false);
    }
  }, [location]);

  return (
    <div className="app">
      <header className="header">
        <span className="logo">agent<span className="dim">//</span>search</span>
        <span className="badge">beta</span>
      </header>

      <main className="main">
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
