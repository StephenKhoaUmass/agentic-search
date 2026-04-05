import { useState } from 'react';

const EXAMPLES = [
  'AI startups in healthcare',
  'top pizza places in Brooklyn',
  'open source database tools',
  'climate tech companies Series A 2024',
  'best running shoes 2025',
];

export default function SearchBar({ onSearch, disabled }) {
  const [query, setQuery] = useState('');

  const handleSubmit = () => { if (query.trim()) onSearch(query.trim()); };

  return (
    <div className="search-section">
      <div className="search-label">// query</div>
      <div className="search-row">
        <input
          className="search-input"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSubmit()}
          placeholder="e.g. AI startups in healthcare, top pizza places in Brooklyn..."
          disabled={disabled}
        />
        <button className="run-btn" onClick={handleSubmit} disabled={disabled || !query.trim()}>
          {disabled ? 'Running...' : 'Run →'}
        </button>
      </div>
      <div className="examples">
        {EXAMPLES.map(ex => (
          <span key={ex} className="example-chip" onClick={() => !disabled && setQuery(ex)}>{ex}</span>
        ))}
      </div>
    </div>
  );
}
