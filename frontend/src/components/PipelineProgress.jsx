const STATUS_ICONS = { running: '⟳', done: '✓', error: '✗', pending: '○' };

export default function PipelineProgress({ steps, running }) {
  return (
    <div className="progress-section">
      <div className="progress-header">
        {running && <span className="spinner" />}
        <span>Running agent pipeline...</span>
      </div>
      <div className="steps">
        {steps.map(step => (
          <div key={step.id} className={`step ${step.status}`}>
            <span className={`step-icon ${step.status === 'running' ? 'spin' : ''}`}>
              {step.status !== 'running' ? STATUS_ICONS[step.status] : ''}
            </span>
            <div>
              <div className="step-text">{step.text}</div>
              {step.meta && <div className="step-meta">{step.meta}</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
