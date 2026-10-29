/* Browser-owned transcript nodes survive pending/completed updates and image loads. */
(() => {
  const entries = new Map();
  const plots = new Map();
  const agentRequests = new Map();
  const idleOwners = new Map();
  const exports = new Map();
  const sourceFormats = new Map();
  let formattedView = true;
  let nextSourceId = 0;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function followBottom(container) {
    let following = true;
    let previousTop = 0;
    let previousHeight = container.scrollHeight;
    const atBottom = () => container.scrollHeight - container.scrollTop - container.clientHeight <= 8;
    container.addEventListener('scroll', () => {
      // A content resize can clamp scrollTop. Only a deliberate upward movement
      // turns following off; mutations never infer intent from the new height.
      if (container.scrollTop < previousTop - 2) following = false;
      if (atBottom()) following = true;
      previousTop = container.scrollTop;
    });
    const resize = () => {
      // Scroll events are dispatched on a later frame. Observe an upward user
      // move before a same-frame mutation can pull the viewer back down.
      const height = container.scrollHeight;
      if (container.scrollTop < previousTop - 2 && height >= previousHeight) following = false;
      previousHeight = height;
      if (!following) return;
      container.scrollTop = container.scrollHeight;
      previousTop = container.scrollTop;
    };
    new MutationObserver(resize).observe(container, {childList: true, subtree: true, characterData: true});
    new ResizeObserver(resize).observe(container.firstElementChild);
    container.addEventListener('load', resize, true);
    container.addEventListener('wheel', event => { if (event.deltaY < 0) following = false; }, {passive: true});
  }
  function renderSource(item) {
    const format = sourceFormats.get(item.formatKey) || item.format;
    const useFormatted = formattedView && format?.status === 'formatted';
    const html = useFormatted ? format.source_html : item.originalHTML;
    if (item.renderedHTML !== html) {
      // Both views come from Shiny's native Markdown component. Formatted text
      // never replaces the original source used to correlate submissions.
      const template = document.createElement('template');
      template.innerHTML = html || '';
      // A formatted result can be shared by identical submissions, but each
      // native Markdown component must keep its own DOM identity.
      if (template.content.firstElementChild) template.content.firstElementChild.id = item.sourceId;
      item.code.replaceChildren(template.content);
      item.renderedHTML = html;
    }
    item.truncation.textContent = item.sourceTruncated ? 'Source shortened by capture limit' : '';
    if (!format || item.meta.origin === 'session') {
      item.formatStatus.textContent = '';
      return;
    }
    const formatter = format.formatter || (item.meta.language === 'r' ? 'Air' : 'Ruff');
    item.formatStatus.textContent = format.status === 'formatted'
      ? (formattedView ? `Formatted with ${formatter}` : `Original source · ${formatter} formatting available`)
      : format.message || {
        pending: 'Formatting… Original source shown.',
        unavailable: 'Formatter unavailable. Original source shown.',
        unsupported: 'SQL formatting is not supported. Original source shown.',
        failed: 'Formatting failed. Original source shown.',
        truncated: 'Captured source is incomplete. Original source shown.',
      }[format.status];
  }
  function captureSource(item, meta) {
    // An admitted evaluation may retain only a source prefix. Keep a full
    // human/agent submission and its formatting even when that snapshot arrives later.
    if (meta.source_truncated && item.source !== undefined && !item.sourceTruncated) return;
    item.source = meta.source;
    item.sourceTruncated = Boolean(meta.source_truncated);
    item.originalHTML = meta.source_html || '';
    item.formatKey = meta.format_key;
    item.format = meta.format_status ? {
      status: meta.format_status, message: meta.format_message, formatter: meta.formatter,
    } : null;
    renderSource(item);
  }
  function entry(meta) {
    const key = String(meta.id);
    let existing = entries.get(key);
    const provisionalAgent = key.startsWith('agent:');
    if (!existing && meta.origin === 'mcp') {
      const match = [...entries.entries()].find(([otherKey, value]) =>
        value.meta?.origin === 'mcp' && value.meta.language === meta.language &&
        (value.source === meta.source || (meta.source_truncated && value.source.startsWith(meta.source)) ||
          (value.sourceTruncated && meta.source.startsWith(value.source))) && !value.agentMatched &&
        (provisionalAgent || value.meta.state === 'running') &&
        otherKey.startsWith('agent:') !== provisionalAgent);
      if (match) {
        existing = match[1];
        existing.agentMatched = true;
        if (provisionalAgent) {
          agentRequests.set(key, existing);
          if (existing.sourceTruncated) captureSource(existing, meta);
          return existing;
        }
        entries.delete(match[0]);
      }
    }
    if (!existing && meta.request_id) {
      existing = entries.get('request:' + meta.request_id);
      if (existing) entries.delete('request:' + meta.request_id);
    }
    if (!existing) {
      const node = element('article', 'entry');
      const submission = element('div', 'submission');
      const labels = element('div', 'labels');
      const origin = element('strong', 'origin');
      const language = element('strong', 'language');
      const state = element('small', 'evaluation-state');
      const truncation = element('small', 'source-truncation');
      labels.append(origin, language, state, truncation);
      const code = element('div', 'source');
      const formatStatus = element('small', 'format-status');
      submission.append(labels, code, formatStatus);
      const content = element('div', 'content');
      node.append(submission, content);
      document.querySelector('#transcript').append(node);
      existing = {node, origin, language, state, truncation, code, formatStatus, content,
        sourceId: `transcript-source-${++nextSourceId}`, events: new Map()};
    }
    if (existing.pendingOutcome && !provisionalAgent) {
      existing.content.replaceChildren();
      existing.pendingOutcome = false;
    }
    entries.set(key, existing);
    if (existing.meta?.state === 'complete' && meta.state === 'running') meta = {...meta, state: 'complete'};
    existing.meta = meta;
    existing.actual = !key.startsWith('agent:') && !key.startsWith('request:');
    if (provisionalAgent) agentRequests.set(key, existing);
    existing.node.dataset.evaluation = key;
    existing.node.className = `entry ${meta.origin === 'mcp' ? 'agent' : meta.origin === 'session' ? 'session' : 'direct'} ${meta.state}`;
    existing.origin.textContent = meta.origin === 'mcp' ? 'Agent' : meta.origin === 'session' ? 'Session' : 'Human';
    existing.language.textContent = meta.language === 'r' ? 'R' : meta.language.toUpperCase();
    existing.state.textContent = `${meta.execution === 'immediate' ? 'Immediate · ' : ''}${meta.state}`;
    captureSource(existing, meta);
    return existing;
  }
  function output(event) {
    if (['stdout', 'stderr', 'output', 'diagnostic', 'notice'].includes(event.type)) {
      return element('span', `stream ${event.type}`, event.text);
    }
    if (event.type === 'execution_error') {
      const error = event.error;
      return element('pre', 'execution-error', `${error.ename}: ${error.evalue}`);
    }
    if (event.type === 'image') {
      const image = element('img', 'captured-plot');
      image.src = `data:${event.mimeType};base64,${event.data}`;
      image.alt = 'Captured session plot. Click to enlarge.';
      image.tabIndex = 0;
      image.onclick = () => enlarge(image.src);
      image.onkeydown = event => { if (event.key === 'Enter') enlarge(image.src); };
      return image;
    }
    if (event.type === 'omission') return element('p', 'omission', event.message || 'Output omitted by the bounded capture.');
    return null;
  }
  function enlarge(src) {
    document.querySelector('#plot-enlarged').src = src;
    document.querySelector('#plot-dialog').showModal();
  }
  function snapshot(message) {
    if (message.reset) {
      plots.clear();
      document.querySelector('#plots').replaceChildren();
      // A new worker snapshot confirms retirement of the previous worker.
      // Its final metadata may have been coalesced out of the subscription.
      for (const item of entries.values()) {
        if (item.meta.state !== 'running') continue;
        item.meta = {...item.meta, state: 'retired'};
        item.node.classList.replace('running', 'retired');
        item.state.textContent = 'Stopped · session replaced';
      }
    }
    for (const meta of message.evaluations) entry(meta);
    let idleOwner = null;
    for (const event of message.events) {
      const eventKey = `${message.worker_id}:${event.evaluation_id}:${event.sequence}`;
      let owner = entries.get(String(event.evaluation_id));
      if (event.evaluation_id === null) {
        idleOwner = idleOwners.get(eventKey) || idleOwner || entry({id: 'idle:' + eventKey,
          origin: 'session', language: '', source: '', execution: 'serial', state: 'idle'});
        owner = idleOwner;
        idleOwners.set(eventKey, owner);
      } else idleOwner = null;
      if (!owner) continue;
      const serialized = JSON.stringify(event);
      const prior = owner.events.get(eventKey);
      if (prior?.serialized === serialized) continue;
      const node = output(event);
      if (!node) continue;
      if (prior) prior.node.replaceWith(node); else owner.content.append(node);
      owner.events.set(eventKey, {node, serialized});
      if (event.type === 'image' && !plots.has(eventKey)) {
        plots.set(eventKey, true);
        const figure = element('figure');
        figure.append(output(event), element('figcaption', '', `${owner.language.textContent} · ${owner.origin.textContent}`));
        document.querySelector('#plots').append(figure);
      }
    }
    document.querySelector('#output-omissions').textContent = message.omitted_events || message.omitted_bytes
      ? `${message.omitted_events} events and ${message.omitted_bytes} bytes omitted from the bounded server snapshot.` : '';
  }
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.history').forEach(followBottom);
    document.querySelector('#source_view').addEventListener('change', () => {
      formattedView = document.querySelector('#source_view input:checked').value === 'formatted';
      for (const item of entries.values()) renderSource(item);
    });
    document.querySelector('#plot-close').onclick = () => document.querySelector('#plot-dialog').close();
    document.addEventListener('click', event => {
      const copy = event.target.closest('[data-export-format]');
      if (copy) {
        const status = document.querySelector('#export_status');
        const label = copy.dataset.exportFormat === 'qmd' ? 'QMD' : 'Markdown';
        if (!navigator.clipboard?.write || !window.ClipboardItem) {
          status.textContent = 'Copy failed: this browser does not support clipboard writing.';
          return;
        }
        const id = crypto.randomUUID();
        const pending = {};
        const text = new Promise((resolve, reject) => Object.assign(pending, {resolve, reject}));
        exports.set(id, pending);
        document.querySelectorAll('[data-export-format]').forEach(button => { button.disabled = true; });
        status.textContent = `Copying ${label}…`;
        // Start clipboard access inside the click gesture. The promised Blob
        // lets Console's projection arrive asynchronously without a stale cache.
        navigator.clipboard.write([new ClipboardItem({
          'text/plain': text.then(value => new Blob([value], {type: 'text/plain'})),
        })]).then(() => {
          status.textContent = `Copied ${label}.`;
        }).catch(error => {
          status.textContent = `Copy failed: ${pending.error || error.message}`;
        }).finally(() => {
          exports.delete(id);
          document.querySelectorAll('[data-export-format]').forEach(button => { button.disabled = false; });
        });
        Shiny.setInputValue('export_request', {id, format: copy.dataset.exportFormat}, {priority: 'event'});
      }
      const button = event.target.closest('[data-table-language]');
      if (button) Shiny.setInputValue('open_table', {language: button.dataset.tableLanguage, name: button.dataset.tableName, nonce: Date.now()}, {priority: 'event'});
    });
    Shiny.addCustomMessageHandler('submission', entry);
    Shiny.addCustomMessageHandler('formatted-source', message => {
      sourceFormats.set(message.format_key, message);
      for (const item of entries.values()) {
        if (item.formatKey === message.format_key) renderSource(item);
      }
    });
    Shiny.addCustomMessageHandler('export-projection', message => {
      const pending = exports.get(message.id);
      if (!pending) return;
      if (message.error) {
        pending.error = message.error;
        pending.reject(new Error(message.error));
      } else pending.resolve(message.text);
    });
    Shiny.addCustomMessageHandler('output-snapshot', snapshot);
    Shiny.addCustomMessageHandler('agent-outcome', message => {
      for (const id of message.ids) {
        const item = agentRequests.get(id);
        if (item && !item.actual) {
          item.node.className = 'entry agent failed';
          item.state.textContent = 'Not executed';
          item.meta.state = 'failed';
          item.content.textContent = message.text;
          item.pendingOutcome = true;
        }
      }
    });
    Shiny.addCustomMessageHandler('notice', message => {
      const node = element('article', `notice ${message.kind}`, message.text);
      document.querySelector('#transcript').append(node);
    });
  });
})();
