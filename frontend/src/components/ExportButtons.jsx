export default function ExportButtons({ schema, entities }) {
  const exportJSON = () => {
    const blob = new Blob([JSON.stringify(entities, null, 2)], { type: 'application/json' });
    download(blob, 'agent-search-results.json');
  };

  const exportCSV = () => {
    const cols = schema.columns.map(c => c.key);
    const header = [...cols, 'confidence', 'source_url'].join(',');
    const rows = entities.map(e =>
      [...cols.map(k => csvCell(e[k])), csvCell(e._confidence), csvCell(e.source_url)].join(',')
    );
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' });
    download(blob, 'agent-search-results.csv');
  };

  return (
    <div className="export-row">
      <button className="export-btn" onClick={exportJSON}>export JSON</button>
      <button className="export-btn" onClick={exportCSV}>export CSV</button>
    </div>
  );
}

function csvCell(v) {
  if (v == null) return '""';
  const str = Array.isArray(v) ? v.join('; ') : String(v);
  return JSON.stringify(str);
}

function download(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}
