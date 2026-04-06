/* ====================================================================
   Gideon — Main JavaScript
   Linear-inspired SPA interactions
   ==================================================================== */

'use strict';

// ── State ──────────────────────────────────────────────────────────
const state = {
  jobs:          [],
  jobsTotal:     0,
  jobsPage:      0,
  jobsPageSize:  50,
  jobsSearch:    '',
  jobsStatus:    '',
  activePanel:   null,   // 'job' | 'resume' | null
  activePanelId: null,
};

// ── Utility ────────────────────────────────────────────────────────

function qs(sel, root = document) { return root.querySelector(sel); }
function qsa(sel, root = document) { return [...root.querySelectorAll(sel)]; }

function toast(msg, type = 'info', duration = 3500) {
  const container = qs('#toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icon = type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ';
  el.innerHTML = `<span style="font-size:14px;font-weight:700">${icon}</span><span>${msg}</span>`;
  container.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

function matchChipHtml(score) {
  if (score === null || score === undefined) return '<span class="match-chip match-low">—</span>';
  const cls = score >= 50 ? 'match-high' : score >= 35 ? 'match-mid' : 'match-low';
  return `<span class="match-chip ${cls}">${score.toFixed(0)}%</span>`;
}

function badgeHtml(status) {
  return `<span class="badge badge-${status}">${status}</span>`;
}

function relativeDate(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  const now = Date.now();
  const diff = Math.floor((now - d.getTime()) / 1000);
  if (diff < 60)   return 'just now';
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff/86400)}d ago`;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function truncate(str, n = 40) {
  if (!str) return '—';
  return str.length > n ? str.slice(0, n) + '…' : str;
}

// ── Panel ──────────────────────────────────────────────────────────

function openPanel() {
  const panel   = qs('#side-panel');
  const overlay = qs('#panel-overlay');
  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
  overlay.classList.add('visible');
}

function closePanel() {
  const panel   = qs('#side-panel');
  const overlay = qs('#panel-overlay');
  panel.classList.remove('open');
  panel.setAttribute('aria-hidden', 'true');
  overlay.classList.remove('visible');
  state.activePanel   = null;
  state.activePanelId = null;
}

// Close on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closePanel();
});

// ── Job panel ──────────────────────────────────────────────────────

async function openJobPanel(jobId) {
  if (state.activePanel === 'job' && state.activePanelId === jobId) {
    closePanel(); return;
  }
  state.activePanel   = 'job';
  state.activePanelId = jobId;

  const body = qs('#side-panel-body');
  body.innerHTML = '<div class="td-loading">Loading…</div>';
  openPanel();

  try {
    const res  = await fetch(`/api/jobs/${jobId}`);
    const job  = await res.json();
    if (!res.ok) {
      body.innerHTML = `<div class="panel-error">Failed to load job: ${_escHtml(job.error || res.status)}</div>`;
      return;
    }

    const skills = (job.required_skills || []).map(s =>
      `<span class="skill-tag skill-required">${_escHtml(s)}</span>`
    ).join('');
    const prefSkills = (job.preferred_skills || []).map(s =>
      `<span class="skill-tag skill-preferred">${_escHtml(s)}</span>`
    ).join('');

    let scoreHtml = '';
    if (job.tailored_resume) {
      const tr = job.tailored_resume;
      const bd = job.score_breakdown;
      scoreHtml = `
        <div class="panel-section">
          <div class="panel-section-title">Match Score ${matchChipHtml(tr.match_score)}</div>
          ${bd ? `
          <div class="score-breakdown">
            ${_scoreRow('Required Skills', bd.required_skills)}
            ${_scoreRow('Preferred Skills', bd.preferred_skills)}
            ${_scoreRow('Experience', bd.experience)}
            ${_scoreRow('Education', bd.education)}
            ${bd.bonus ? `<div class="score-bonus">+${bd.bonus.toFixed(1)} bonus pts</div>` : ''}
          </div>` : ''}
          <div class="panel-actions" style="margin-top:8px">
            <button class="btn btn-secondary btn-sm"
              onclick="downloadPDF(${tr.id})">
              ${tr.pdf_path ? 'Download PDF' : 'Export PDF'}
            </button>
          </div>
        </div>`;
    } else if (job.status === 'new') {
      scoreHtml = `
        <div class="panel-section">
          <div class="mismatch-card">
            <div class="mismatch-title">Not yet analyzed</div>
            <div class="mismatch-body">
              Run <strong>Analyze Jobs</strong> before generating a tailored resume.
              Skills must be extracted first so the resume can be properly targeted.
            </div>
          </div>
        </div>`;
    } else {
      scoreHtml = `
        <div class="panel-section">
          <button class="btn btn-primary btn-sm" onclick="generateResume(${job.id}, this)">
            Generate Tailored Resume
          </button>
        </div>`;
    }

    const statusBadge = `<span class="status-badge status-${job.status}">${job.status}</span>`;
    const archiveBtn = (job.status !== 'archived' && job.status !== 'applied')
      ? `<button class="btn btn-sm btn-ghost" style="margin-left:8px" onclick="updateJobStatus(${job.id},'archived',this)">Archive</button>`
      : '';
    const applyBtn = (job.status === 'analyzed')
      ? `<button class="btn btn-sm btn-ghost" style="margin-left:8px" onclick="updateJobStatus(${job.id},'applied',this)">Mark as Applied</button>`
      : '';

    body.innerHTML = `
      <div class="panel-job-title">${_escHtml(job.title)}</div>
      <div class="panel-company">${_escHtml(job.company)}</div>
      <div class="panel-meta-row">
        ${job.location  ? `<span class="panel-meta-item">📍 ${_escHtml(job.location)}</span>` : ''}
        ${job.salary_range ? `<span class="panel-meta-item">💰 ${_escHtml(job.salary_range)}</span>` : ''}
        ${job.date_posted ? `<span class="panel-meta-item">📅 Posted ${relativeDate(job.date_posted)}</span>` : ''}
        ${job.source    ? `<span class="panel-meta-item">${_escHtml(job.source)}</span>` : ''}
        ${job.analyzed_with_resume_name
          ? `<span class="panel-meta-item analyzed-badge">Analyzed with: <strong>${_escHtml(job.analyzed_with_resume_name)}</strong></span>`
          : ''}
      </div>

      <div class="panel-section">
        <div class="panel-section-title">Status</div>
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px">
          ${statusBadge}
          ${applyBtn}
          ${archiveBtn}
          ${job.application_url ? `<a href="${_escHtml(job.application_url)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost" style="margin-left:4px">Apply →</a>` : ''}
        </div>
      </div>

      ${scoreHtml}

      ${skills || prefSkills ? `
      <div class="panel-section">
        ${skills ? `<div class="panel-section-title">Required Skills</div><div class="panel-skills">${skills}</div>` : ''}
        ${prefSkills ? `<div class="panel-section-title" style="margin-top:8px">Preferred Skills</div><div class="panel-skills">${prefSkills}</div>` : ''}
      </div>` : ''}

      ${job.description ? `
      <div class="panel-section">
        <div class="panel-section-title">Description</div>
        <div class="panel-body-text">${_escHtml(job.description).slice(0, 1200)}${job.description.length > 1200 ? '…' : ''}</div>
      </div>` : ''}
    `;
  } catch (e) {
    body.innerHTML = `<div class="panel-error">Error: ${_escHtml(e.message)}</div>`;
  }
}

function _scoreRow(label, component) {
  if (!component) return '';
  const pct = typeof component.score === 'number' ? Math.round(component.score) : 0;
  const detail = (component.matched !== undefined && component.total !== undefined)
    ? `${component.matched}/${component.total}`
    : (component.matched ? 'Yes' : 'No');
  return `
    <div class="score-bar-wrap">
      <span class="score-bar-label">${_escHtml(label)}</span>
      <div class="score-bar-track">
        <div class="score-bar-fill" style="width:${Math.min(pct,100)}%"></div>
      </div>
      <span class="score-bar-pct">${pct}% <span class="score-bar-detail">(${detail})</span></span>
    </div>`;
}

async function generateResume(jobId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  try {
    const res  = await fetch('/api/generate-resume', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ job_id: jobId }),
    });
    const data = await res.json();
    if (res.ok) {
      const msg = data.tailoring_applied
        ? 'Resume tailored! Match score: ' + (data.match_score || 0).toFixed(0) + '%'
        : 'Resume saved — Gemini tailoring did not run. Check your API key or quota.';
      toast(msg, data.tailoring_applied ? 'success' : 'warning');
      openJobPanel(jobId);  // refresh panel
    } else if (res.status === 409 && data.error === 'resume_mismatch') {
      if (btn) { btn.disabled = false; btn.textContent = 'Generate Tailored Resume'; }
      _showMismatchCard(jobId, data, btn);
    } else if (res.status === 422 && data.error === 'not_analyzed') {
      toast('Run Analyze Jobs first — this job has not been analyzed yet.', 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Generate Tailored Resume'; }
    } else {
      toast(data.error || 'Generation failed', 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Generate Tailored Resume'; }
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Generate Tailored Resume'; }
  }
}

function _showMismatchCard(jobId, data, triggerBtn) {
  document.querySelector('.mismatch-card')?.remove();
  const card = document.createElement('div');
  card.className = 'mismatch-card';
  card.innerHTML = `
    <div class="mismatch-title">Resume mismatch</div>
    <div class="mismatch-body">
      This job was analyzed with <strong>${_escHtml(data.analyzed_with_resume_name || 'Unknown')}</strong>
      ${data.analyzed_with_domain ? '(' + _escHtml(data.analyzed_with_domain) + ')' : ''}.
      Your active resume is <strong>${_escHtml(data.active_resume_name || '')}</strong>
      ${data.active_domain ? '(' + _escHtml(data.active_domain) + ')' : ''}.
      Generating now would produce an inaccurate result.
    </div>
    <div class="mismatch-actions">
      <button class="btn btn-sm btn-warning" onclick="reanalyzeJob(${jobId}, this)">
        Re-analyze with current resume
      </button>
      <a href="/resumes" class="btn btn-sm btn-ghost">Switch resume</a>
    </div>
  `;
  // Insert before the trigger button's parent section, or prepend to panel body
  const section = triggerBtn ? triggerBtn.closest('div') : null;
  if (section) {
    section.insertAdjacentElement('beforebegin', card);
  } else {
    const body = document.getElementById('side-panel-body');
    if (body) body.prepend(card);
  }
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function reanalyzeJob(jobId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Re-analyzing…'; }
  try {
    const res  = await fetch('/api/jobs/' + jobId + '/reanalyze', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      toast('Re-analyzed — match score: ' + data.match_score + '%', 'success');
      document.querySelector('.mismatch-card')?.remove();
      openJobPanel(jobId);
    } else {
      toast(data.error || 'Re-analysis failed', 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze with current resume'; }
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze with current resume'; }
  }
}

async function updateJobStatus(jobId, status, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/api/jobs/${jobId}/status`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ status }),
    });
    if (res.ok) {
      toast('Status updated to ' + status, 'success');
      loadJobs();
      openJobPanel(jobId);
    } else {
      const d = await res.json();
      toast(d.error || 'Update failed', 'error');
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
    if (btn) btn.disabled = false;
  }
}

// ── Resume panel ───────────────────────────────────────────────────

async function openResumePanel(resumeId) {
  if (state.activePanel === 'resume' && state.activePanelId === resumeId) {
    closePanel(); return;
  }
  state.activePanel   = 'resume';
  state.activePanelId = resumeId;

  const body = qs('#side-panel-body');
  body.innerHTML = '<div class="td-loading">Loading…</div>';
  openPanel();

  try {
    const res  = await fetch(`/api/resumes/tailored/${resumeId}`);
    const data = await res.json();
    if (!res.ok) {
      body.innerHTML = `<div class="panel-error">Failed to load resume: ${_escHtml(data.error || res.status)}</div>`;
      return;
    }

    const content = data.content || {};
    const summary = content.professional_summary || '';
    const skills  = (content.skills || []).slice(0, 20);
    const workExp = content.work_experience || [];

    body.innerHTML = `
      <div class="panel-job-title">${_escHtml(data.job_title || 'Tailored Resume')}</div>
      <div class="panel-company">${_escHtml(data.company || '')}</div>
      <div class="panel-meta-row">
        <span class="panel-meta-item">${matchChipHtml(data.match_score)} match</span>
        ${data.generated_at ? `<span class="panel-meta-item">Generated ${relativeDate(data.generated_at)}</span>` : ''}
      </div>
      <div class="panel-section">
        <div class="panel-actions">
          <button class="btn btn-primary btn-sm" onclick="downloadPDF(${resumeId})">
            ${data.has_pdf ? 'Download PDF' : 'Export & Download PDF'}
          </button>
        </div>
      </div>
      ${summary ? `
      <div class="panel-section">
        <div class="panel-section-title">Summary</div>
        <div class="panel-body-text">${_escHtml(summary)}</div>
      </div>` : ''}
      ${skills.length ? `
      <div class="panel-section">
        <div class="panel-section-title">Skills</div>
        <div class="panel-skills">${skills.map(s => `<span class="skill-tag">${_escHtml(s)}</span>`).join('')}</div>
      </div>` : ''}
      ${workExp.length ? `
      <div class="panel-section">
        <div class="panel-section-title">Experience</div>
        ${workExp.slice(0,3).map(w => `
          <div class="panel-exp-item">
            <div class="panel-exp-title">${_escHtml(w.title || '')} at ${_escHtml(w.company || '')}</div>
            <div class="panel-exp-dates">${_escHtml(w.start_date || '')}${w.end_date ? ' – ' + _escHtml(w.end_date) : ''}</div>
          </div>`).join('')}
      </div>` : ''}
    `;
  } catch (e) {
    body.innerHTML = `<div class="panel-error">Error: ${_escHtml(e.message)}</div>`;
  }
}

// ── HTML escape ────────────────────────────────────────────────────

function _escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Master resumes list ─────────────────────────────────────────────

async function loadMasterResumes() {
  const container = qs('#master-resumes-list');
  if (!container) return;

  try {
    const res  = await fetch('/api/resumes/master');
    const raw  = await res.json();
    const data = Array.isArray(raw) ? raw : (raw.resumes || []);

    // Debug: log raw API data so we can verify fields are present
    console.log('[loadMasterResumes] API data:', data.map(r => ({
      id: r.id, name: r.name, is_sample: r.is_sample, domain: r.domain
    })));

    if (!data.length) {
      container.innerHTML = '<div class="empty-state-sm">No master resumes uploaded.</div>';
      return;
    }

    const domainDisplay = {
      software_engineering: 'Software Engineering',
      ai_ml:                'AI / ML',
      product_management:   'Product Management',
      marketing:            'Marketing',
      data_analytics:       'Data & Analytics',
      design:               'Design (UX/UI)',
      finance:              'Finance',
      sales:                'Sales',
      operations:           'Operations',
      other:                'Other',
    };

    const _domainOpts = [
      ['software_engineering','Software Engineering'],['ai_ml','AI / ML'],
      ['product_management','Product Management'],['marketing','Marketing'],
      ['data_analytics','Data & Analytics'],['design','Design (UX/UI)'],
      ['finance','Finance'],['sales','Sales'],['operations','Operations'],
    ];

    // Render a checkbox list for multi-domain selection.
    // currentDomains: array of currently selected domain values.
    const _domCheckboxHtml = (resumeId, currentDomains) =>
      `<div class="domain-checkbox-list" id="domain-checkboxes-${resumeId}" onclick="event.stopPropagation()">
        ${_domainOpts.map(([v,l]) =>
          `<label class="domain-checkbox-item">
            <input type="checkbox" value="${v}"
                   ${currentDomains.includes(v) ? 'checked' : ''}
                   onchange="event.stopPropagation()">
            ${_escHtml(l)}
          </label>`
        ).join('')}
        <button class="btn btn-xs btn-primary domain-save-btn"
                onclick="event.stopPropagation(); saveDomains(${resumeId})">Save</button>
      </div>`;

    container.innerHTML = data.map(m => {
      // Explicit coercion — handles true/1/"true" from any serialiser
      const isActive = m.is_active === true || m.is_active === 1 || m.is_active === 'true';
      const isSample = m.is_sample === true || m.is_sample === 1 || m.is_sample === 'true';
      const domain   = (m.domain != null && m.domain !== '') ? String(m.domain) : '';
      // currentDomains: prefer the multi-domain array from the API, fall back to single domain
      const currentDomains = Array.isArray(m.domains) && m.domains.length
        ? m.domains.filter(d => d && d !== 'other')
        : (domain && domain !== 'other' ? [domain] : []);
      const isOtherDom = currentDomains.length === 0;

      const activeBadge = isActive
        ? '<span class="master-card-active-badge" aria-hidden="true">── ACTIVE ──</span>'
        : '';
      const activeClass = isActive ? 'is-active card-active' : '';

      // Domain display: one badge per domain; inline checkboxes when none set
      const domainHtml = isSample
        ? (domain ? `<span class="domain-badge domain-badge-sm">${_escHtml(domainDisplay[domain] || domain)}</span>` : '')
        : (isOtherDom
            ? `<span class="domain-badge domain-badge-sm domain-badge-warn" title="Set a domain to enable job scraping">No domain</span>
               ${_domCheckboxHtml(m.id, [])}`
            : `<span class="domain-badges-row">
                 ${currentDomains.map(d =>
                   `<span class="domain-badge domain-badge-sm">${_escHtml(domainDisplay[d] || d)}</span>`
                 ).join('')}
                 <button class="btn-icon domain-edit-btn" title="Edit domains"
                   onclick="event.stopPropagation(); toggleDomainEdit(${m.id}, this)"
                   data-resume-id="${m.id}">&#x270E;</button>
               </span>
               <span class="domain-edit-inline" id="domain-edit-${m.id}" style="display:none">
                 ${_domCheckboxHtml(m.id, currentDomains)}
               </span>`
          );

      // Attributes use explicit 'true'/'false' strings so dataset reads work correctly
      return `
      <div class="master-card master-resume-card ${activeClass}"
           role="button"
           tabindex="0"
           data-resume-id="${m.id}"
           data-is-sample="${isSample ? 'true' : 'false'}"
           data-domain="${domain.replace(/"/g, '&quot;')}"
           data-is-active="${isActive ? 'true' : 'false'}"
           onclick="activateResume(this)"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();activateResume(this);}">
        <div class="master-card-name">
          ${isActive ? '<span class="active-dot"></span>' : ''}
          ${_escHtml(m.name)}
          ${activeBadge}
        </div>
        <div class="master-card-domain">${domainHtml}</div>
        <div class="master-card-meta">
          Created ${m.created_at}
          <br>${(m.sections || []).join(', ')}
        </div>
        ${!isSample ? `
        <button class="master-card-delete-btn" title="Delete resume"
                onclick="event.stopPropagation(); deleteMasterResume(${m.id}, '${_escHtml(m.name).replace(/'/g, "\\'")}')">
          &#x1F5D1;
        </button>` : ''}
      </div>`;
    }).join('');

  } catch (err) {
    if (container) container.innerHTML = `<div class="td-loading">Error: ${err.message}</div>`;
  }
}

// ── Search Config functions ────────────────────────────────────────────────

async function loadSearchConfigs() {
  const container = document.getElementById('search-config-cards');
  if (!container) return;
  try {
    const res  = await fetch('/api/search-configs');
    const data = await res.json();
    const configs = data.configs || [];

    if (!configs.length) {
      container.innerHTML = '<div class="empty-state-sm">No search configs yet. Add one above.</div>';
      return;
    }

    const domainDisplay = {
      software_engineering: 'Software Engineering', ai_ml: 'AI / ML',
      product_management: 'Product Management', marketing: 'Marketing',
      data_analytics: 'Data & Analytics', design: 'Design (UX/UI)',
      finance: 'Finance', sales: 'Sales', operations: 'Operations', other: 'Other',
    };

    container.innerHTML = configs.map(cfg => `
      <div class="search-config-card" id="cfg-card-${cfg.id}">
        <div class="search-config-header">
          <span class="search-config-title">&#x1F50D; ${esc(cfg.keywords)}</span>
          <div class="search-config-actions">
            <label class="toggle-switch" title="${cfg.enabled ? 'Disable' : 'Enable'}">
              <input type="checkbox" ${cfg.enabled ? 'checked' : ''}
                     onchange="toggleSearchConfig('${cfg.id}', this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <button class="btn-icon btn-danger-ghost" onclick="deleteSearchConfig('${cfg.id}')" title="Delete">&#x2715;</button>
          </div>
        </div>
        <div class="search-config-meta">
          ${esc(cfg.location)} &middot; LinkedIn &middot; Max ${cfg.max_results}
        </div>
        <div style="margin-top:6px">
          <span class="domain-badge domain-badge-sm">${esc(domainDisplay[cfg.domain] || cfg.domain || 'Other')}</span>
        </div>
      </div>`).join('');

  } catch (err) {
    if (container) container.innerHTML = `<div class="td-loading">Error: ${err.message}</div>`;
  }
}

function toggleAddConfigForm() {
  const form = document.getElementById('add-config-form');
  const btn  = document.getElementById('add-config-btn');
  if (!form) return;
  const vis = form.style.display !== 'none';
  form.style.display = vis ? 'none' : 'block';
  if (btn) btn.textContent = vis ? '+ Add New' : '✕ Cancel';
}

async function saveNewConfig(event) {
  event.preventDefault();
  const keywords = (document.getElementById('cfg-keywords') || {}).value || '';
  const location = (document.getElementById('cfg-location') || {}).value || '';
  const domain   = (document.getElementById('cfg-domain')   || {}).value || 'other';
  const maxEl    = document.getElementById('cfg-max');
  const max_results = maxEl ? parseInt(maxEl.value, 10) || 20 : 20;
  const errEl    = document.getElementById('config-form-error');

  if (errEl) errEl.style.display = 'none';

  try {
    const res  = await fetch('/api/search-configs', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ keywords, location, domain, max_results, source: 'linkedin' }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (errEl) { errEl.textContent = data.error || 'Save failed'; errEl.style.display = 'block'; }
      return;
    }
    toggleAddConfigForm();
    event.target.reset();
    loadSearchConfigs();
  } catch (e) {
    if (errEl) { errEl.textContent = 'Network error: ' + e.message; errEl.style.display = 'block'; }
  }
}

async function toggleSearchConfig(id, enabled) {
  try {
    await fetch(`/api/search-configs/${id}`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ enabled }),
    });
    loadSearchConfigs();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function deleteSearchConfig(id) {
  try {
    const res = await fetch(`/api/search-configs/${id}`, { method: 'DELETE' });
    if (res.ok) {
      const card = document.getElementById('cfg-card-' + id);
      if (card) card.remove();
      loadSearchConfigs();
    } else {
      toast('Delete failed', 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ── Domain Resume Assignment ───────────────────────────────────────────────

async function loadDomainResumes() {
  const container = document.getElementById('domain-resume-rows');
  if (!container) return;
  try {
    const [dmRes, mrRes] = await Promise.all([
      fetch('/api/domain-resumes'),
      fetch('/api/resumes/master'),
    ]);
    const dmData = await dmRes.json();
    const mrRaw  = await mrRes.json();
    const mappings  = dmData.mappings || {};
    const resumes   = Array.isArray(mrRaw) ? mrRaw : (mrRaw.resumes || []);
    const ownResumes = resumes.filter(r => !r.is_sample);

    const resumeOptions = `<option value="">— none —</option>` +
      ownResumes.map(r => `<option value="${r.id}">${_escHtml(r.name)}</option>`).join('');

    const domainDisplay = {
      software_engineering: 'Software Engineering', ai_ml: 'AI / ML',
      product_management: 'Product Management', marketing: 'Marketing',
      data_analytics: 'Data & Analytics', design: 'Design (UX/UI)',
      finance: 'Finance', sales: 'Sales', operations: 'Operations',
    };

    container.innerHTML = Object.entries(domainDisplay).map(([domain, label]) => {
      const mapping = mappings[domain] || {};
      const currentId = mapping.resume_id || '';
      const opts = `<option value="">— none —</option>` +
        ownResumes.map(r =>
          `<option value="${r.id}"${r.id == currentId ? ' selected' : ''}>${_escHtml(r.name)}</option>`
        ).join('');
      return `
        <div class="domain-resume-row">
          <span class="domain-resume-label">${_escHtml(label)}</span>
          <select class="domain-resume-select"
                  onchange="setDomainResume('${domain}', this.value)">
            ${opts}
          </select>
        </div>`;
    }).join('');

  } catch (err) {
    if (container) container.innerHTML = `<div class="td-loading">Error: ${err.message}</div>`;
  }
}

async function setDomainResume(domain, resumeId) {
  try {
    const res = await fetch(`/api/domain-resumes/${domain}`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ resume_id: resumeId ? parseInt(resumeId, 10) : null }),
    });
    if (res.ok) {
      toast('Resume assigned for ' + domain.replace(/_/g, ' '), 'success');
    } else {
      const d = await res.json();
      toast(d.error || 'Assignment failed', 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ── Tailored resumes table ─────────────────────────────────────────────────

async function loadTailoredResumes() {
  const tbody = document.getElementById('tailored-tbody');
  if (!tbody) return;
  try {
    const res  = await fetch('/api/resumes/tailored');
    const data = await res.json();
    const resumes = data.resumes || (Array.isArray(data) ? data : []);

    if (!resumes.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="td-empty">No tailored resumes yet — generate one from a job.</td></tr>';
      return;
    }

    tbody.innerHTML = resumes.map(r => `
      <tr>
        <td class="td-title">${_escHtml(r.job_title || '—')}</td>
        <td class="td-secondary">${_escHtml(r.company || '—')}</td>
        <td>${matchChipHtml(r.match_score)}</td>
        <td class="td-secondary td-date">${_escHtml(r.generated_at || '—')}</td>
        <td>${r.has_pdf ? '<span class="pdf-chip">Ready</span>' : '<span class="td-secondary">—</span>'}</td>
        <td>
          <button class="btn btn-sm btn-ghost" onclick="downloadPDF(${r.id})">
            ${r.has_pdf ? 'Download' : 'Export PDF'}
          </button>
          <button class="btn btn-sm btn-ghost" onclick="openResumePanel(${r.id})" style="margin-left:4px">
            View
          </button>
        </td>
      </tr>`).join('');

  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="6" class="td-loading">Error: ${err.message}</td></tr>`;
  }
}

// ── PDF download ───────────────────────────────────────────────────────────

async function downloadPDF(resumeId) {
  // First export (generates the PDF), then download
  try {
    toast('Generating PDF…', 'info');
    const expRes = await fetch('/api/export-pdf', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ resume_id: resumeId }),
    });
    if (!expRes.ok) {
      const d = await expRes.json().catch(() => ({}));
      toast(d.error || 'PDF export failed', 'error');
      return;
    }
    // Now download the file as a blob (no page navigation)
    const dlRes = await fetch(`/api/download-pdf/${resumeId}`);
    if (!dlRes.ok) {
      const d = await dlRes.json().catch(() => ({}));
      toast(d.error || 'Download failed', 'error');
      return;
    }
    const blob = await dlRes.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `resume_${resumeId}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('PDF downloaded', 'success');
    loadTailoredResumes();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ── Jobs page ──────────────────────────────────────────────────────────────

async function loadJobs() {
  const tbody     = qs('#jobs-tbody');
  const countText = qs('#jobs-count-text');
  if (!tbody) return;

  const params = new URLSearchParams({
    limit:  state.jobsPageSize,
    offset: state.jobsPage * state.jobsPageSize,
  });
  if (state.jobsSearch) params.set('search', state.jobsSearch);
  if (state.jobsStatus) params.set('status', state.jobsStatus);

  tbody.innerHTML = `<tr><td colspan="7" class="td-loading">Loading…</td></tr>`;

  try {
    const res  = await fetch('/api/jobs?' + params);
    const data = await res.json();
    const jobs = data.jobs || [];
    state.jobsTotal = data.total || 0;

    if (countText) {
      countText.textContent = `${state.jobsTotal} job${state.jobsTotal !== 1 ? 's' : ''}`;
    }

    // Update nav badge
    const navBadge = qs('#nav-badge-jobs');
    if (navBadge) {
      navBadge.textContent = state.jobsTotal > 0 ? state.jobsTotal : '';
    }

    if (!jobs.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="td-empty">No jobs found — run a scrape or adjust your filters.</td></tr>`;
      renderPagination();
      return;
    }

    tbody.innerHTML = jobs.map(j => `
      <tr class="clickable-row" onclick="openJobPanel(${j.id})">
        <td class="td-title">${_escHtml(j.title)}</td>
        <td class="td-secondary">${_escHtml(j.company)}</td>
        <td class="td-secondary">${_escHtml(j.location || '—')}</td>
        <td class="td-secondary">${j.skills_count ? j.skills_count + ' skills' : '—'}</td>
        <td>${matchChipHtml(j.match_score)}</td>
        <td>${badgeHtml(j.status)}</td>
        <td class="td-secondary td-date">${_escHtml(j.date_scraped || '—')}</td>
      </tr>`).join('');

    renderPagination();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="td-loading">Error: ${e.message}</td></tr>`;
  }
}

function renderPagination() {
  const pg = qs('#jobs-pagination');
  if (!pg) return;
  const totalPages = Math.ceil(state.jobsTotal / state.jobsPageSize);
  if (totalPages <= 1) { pg.innerHTML = ''; return; }

  let html = '';
  if (state.jobsPage > 0) {
    html += `<button class="btn btn-sm btn-ghost" onclick="goPage(${state.jobsPage - 1})">← Prev</button>`;
  }
  html += `<span style="font-size:13px;color:var(--text-secondary);padding:0 8px">Page ${state.jobsPage + 1} / ${totalPages}</span>`;
  if (state.jobsPage < totalPages - 1) {
    html += `<button class="btn btn-sm btn-ghost" onclick="goPage(${state.jobsPage + 1})">Next →</button>`;
  }
  pg.innerHTML = html;
}

function goPage(page) {
  state.jobsPage = page;
  loadJobs();
}

function setupJobFilters() {
  const searchInput  = qs('#search-input');
  const statusFilter = qs('#status-filter');
  let debounceTimer;

  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        state.jobsSearch = searchInput.value.trim();
        state.jobsPage   = 0;
        loadJobs();
      }, 350);
    });
  }

  if (statusFilter) {
    statusFilter.addEventListener('change', () => {
      state.jobsStatus = statusFilter.value;
      state.jobsPage   = 0;
      loadJobs();
    });
  }
}

// Triggered from jobs page "+ Scrape" button
async function runTask(key) {
  try {
    const res  = await fetch(`/api/run/${key}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      toast('Task started — refresh in a moment', 'info');
    } else if (res.status === 409) {
      toast(data.error || 'Already running', 'info');
    } else {
      toast(data.error || 'Start failed', 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ── Helper used in search config card HTML (esc for inline HTML) ───────────

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── activateResume — defined here so loadMasterResumes' onclick can resolve it ──
//
// Called when a .master-resume-card is clicked.  Sends PATCH /api/resume/mode
// with the card's data attributes, then refreshes the mode/card state.
// Exposed on window so inline onclick="activateResume(this)" works from any page.

window.activateResume = async function activateResume(card) {
  if (!card) return;
  if (card.dataset.isActive === 'true') return;   // already active

  const resumeId = card.dataset.resumeId;
  const isSample = card.dataset.isSample === 'true';
  const domain   = card.dataset.domain || '';

  let body;
  if (isSample) {
    if (!domain) {
      if (typeof window.showModeToast === 'function')
        window.showModeToast('✗ This sample has no domain set', 'error');
      else toast('✗ This sample has no domain set', 'error');
      return;
    }
    body = { mode: 'sample', domain: domain };
  } else {
    const rid = parseInt(resumeId, 10);
    if (!rid) {
      if (typeof window.showModeToast === 'function')
        window.showModeToast('✗ Invalid resume id', 'error');
      else toast('✗ Invalid resume id', 'error');
      return;
    }
    body = { mode: 'own', resume_id: rid };
  }

  card.classList.add('loading');
  try {
    const resp = await fetch('/api/resume/mode', {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await resp.json();
    const notify = typeof window.showModeToast === 'function'
      ? window.showModeToast.bind(window)
      : (m, t) => toast(m, t === 'error' ? 'error' : 'success');

    if (resp.ok) {
      if (typeof window._resumePageRefresh === 'function') {
        await window._resumePageRefresh();
      }
      loadMasterResumes();
      notify('✓ Now using: ' + (data.active_resume || data.name || 'resume'));
    } else {
      const msg = typeof data.error === 'string'
        ? data.error
        : JSON.stringify(data);
      notify('✗ ' + msg, 'error');
    }
  } catch (e) {
    const notify = typeof window.showModeToast === 'function'
      ? window.showModeToast.bind(window)
      : (m, t) => toast(m, t === 'error' ? 'error' : 'success');
    notify('✗ Network error', 'error');
  } finally {
    card.classList.remove('loading');
  }
};

// ── Domain override helpers ────────────────────────────────────────────────

// Save selected domains from the checkbox list on a master resume card.
window.saveDomains = async function saveDomains(resumeId) {
  const container = document.getElementById('domain-checkboxes-' + resumeId);
  if (!container) return;
  const checked = [...container.querySelectorAll('input[type=checkbox]:checked')].map(el => el.value);
  if (!checked.length) {
    toast('Select at least one domain.', 'error');
    return;
  }
  try {
    const res  = await fetch(`/api/resume/${resumeId}/domain`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ domains: checked }),
    });
    const data = await res.json();
    const notify = typeof window.showModeToast === 'function'
      ? window.showModeToast.bind(window)
      : (m, t) => toast(m, t === 'error' ? 'error' : 'success');
    if (res.ok) {
      notify('✓ Domains updated');
      loadMasterResumes();
      if (typeof loadModeState === 'function') loadModeState();
    } else {
      notify('✗ ' + (data.error || 'Failed to update domains'), 'error');
    }
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
  }
};

// Legacy single-domain helper (still used by resumes.html banner save).
window.changeDomain = async function changeDomain(resumeId, selectEl) {
  const domain = selectEl.value;
  if (!domain) return;
  try {
    const res  = await fetch(`/api/resume/${resumeId}/domain`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ domain }),
    });
    const data = await res.json();
    const notify = typeof window.showModeToast === 'function'
      ? window.showModeToast.bind(window)
      : (m, t) => toast(m, t === 'error' ? 'error' : 'success');
    if (res.ok) {
      notify('✓ Domain set to ' + data.display_name);
      loadMasterResumes();
      if (typeof loadModeState === 'function') loadModeState();
    } else {
      notify('✗ ' + (data.error || 'Failed to update domain'), 'error');
    }
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
  }
};

// Delete an uploaded master resume (sample resumes are blocked server-side).
window.deleteMasterResume = async function deleteMasterResume(resumeId, name) {
  if (!confirm(`Delete "${name}"?\n\nThis will also remove all tailored resumes generated from it. This cannot be undone.`)) return;
  try {
    const res  = await fetch(`/api/resumes/master/${resumeId}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok) {
      const notify = typeof window.showModeToast === 'function'
        ? window.showModeToast.bind(window)
        : (m, t) => toast(m, t === 'error' ? 'error' : 'success');
      notify('Resume deleted');
      loadMasterResumes();
      if (typeof loadModeState === 'function') loadModeState();
    } else {
      toast(data.error || 'Delete failed', 'error');
    }
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
  }
};

// Toggle the inline domain-edit <select> visible/hidden on the card ✎ button.
window.toggleDomainEdit = function toggleDomainEdit(resumeId, btn) {
  const span = document.getElementById('domain-edit-' + resumeId);
  if (!span) return;
  const visible = span.style.display !== 'none';
  span.style.display = visible ? 'none' : 'inline-flex';
  btn.textContent = visible ? '\u270E' : '\u2715';
};
