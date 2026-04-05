import { useState, useMemo } from 'react';

export default function EntityTable({ schema, entities }) {
  const [filter, setFilter] = useState('');
  const [sortKey, setSortKey] = useState(null);
  const [sortAsc, setSortAsc] = useState(true);

  const filtered = useMemo(() => {
    let rows = entities;
    if (filter) {
      const q = filter.toLowerCase();
      rows = rows.filter(e => JSON.stringify(e).toLowerCase().includes(q));
    }
    if (sortKey) {
      rows = [...rows].sort((a, b) => {
        const av = String(a[sortKey] ?? '');
        const bv = String(b[sortKey] ?? '');
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
    }
    return rows;
  }, [entities, filter, sortKey, sortAsc]);

  const handleSort = (key) => {
    if (sortKey === key) setSortAsc(a => !a);
    else { setSortKey(key); setSortAsc(true); }
  };

  return (
    <div className="results-section">
      <div className="results-header">
        <span className="results-title">// discovered entities</span>
        <span className="results-count">{filtered.length} results</span>
      </div>

      <div className="filter-row">
        <span className="filter-label">filter:</span>
        <input
          className="filter-input"
          placeholder="search within results..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
        />
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {schema.columns.map(col => (
                <th key={col.key} onClick={() => handleSort(col.key)} style={{ cursor: 'pointer' }}>
                  {col.label} {sortKey === col.key ? (sortAsc ? '↑' : '↓') : ''}
                </th>
              ))}
              <th>Confidence</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((entity, i) => (
              <tr key={i}>
                {schema.columns.map(col => (
                  <td key={col.key}>
                    <CellValue value={entity[col.key]} type={col.type} colKey={col.key} highlight={filter} />
                  </td>
                ))}
                <td><ConfidenceBadge level={entity._confidence} /></td>
                <td><SourceCell url={entity.source_url} title={entity.source_title} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CellValue({ value, type, colKey, highlight }) {
  if (value == null || value === '' || value === 'null') {
    return <span className="cell-empty">—</span>;
  }

  if (type === 'url' || (typeof value === 'string' && value.startsWith('http'))) {
    const label = value.replace(/^https?:\/\//, '').replace(/\/$/, '').slice(0, 40);
    return <a className="cell-url" href={value} target="_blank" rel="noreferrer">{label}</a>;
  }

  if (type === 'tags' || Array.isArray(value)) {
    const tags = Array.isArray(value) ? value : value.split(/[,;]+/).map(t => t.trim());
    return <>{tags.map((t, i) => <span key={i} className="cell-tag">{t}</span>)}</>;
  }

  const text = String(value);
  if (colKey === 'name') return <span className="cell-name"><Highlight text={text} query={highlight} /></span>;
  if (colKey === 'description' || colKey === 'summary') return <span className="cell-desc"><Highlight text={text} query={highlight} /></span>;
  return <Highlight text={text} query={highlight} />;
}

function Highlight({ text, query }) {
  if (!query) return text;
  const parts = text.split(new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi'));
  return <>{parts.map((p, i) => parts.length > 1 && i % 2 === 1 ? <mark key={i}>{p}</mark> : p)}</>;
}

function ConfidenceBadge({ level }) {
  return (
    <span className={`confidence conf-${level || 'mid'}`}>
      <span className="conf-dot" /> {level || 'mid'}
    </span>
  );
}

function SourceCell({ url, title }) {
  if (!url) return <span className="cell-empty">—</span>;
  let host;
  try { host = new URL(url).hostname; } catch { host = url.slice(0, 30); }
  return (
    <div className="cell-source" title={url}>
      <a href={url} target="_blank" rel="noreferrer">{title || host}</a>
    </div>
  );
}