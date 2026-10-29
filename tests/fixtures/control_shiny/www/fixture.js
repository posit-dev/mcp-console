/* Browser-owned transcript nodes survive pending/completed updates and image loads. */
(() => {
  const sessionViews = new Map();
  const closedViews = new Set();
  const exports = new Map();
  const sourceFormats = new Map();
  let selectedView = null;
  let historyScroll;
  let nextSourceId = 0;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function followBottom(container, paused = false) {
    let following = true;
    let saved = {following: true, top: 0};
    let previousTop = 0;
    let previousHeight = container.scrollHeight;
    let previousClientHeight = container.clientHeight;
    const atBottom = () => container.scrollHeight - container.scrollTop - container.clientHeight <= 8;
    const returnedToBottom = () => container.scrollTop > previousTop + 2 &&
      container.scrollTop >= previousHeight - previousClientHeight - 8;
    const onScroll = () => {
      if (paused) return;
      // A content resize can clamp scrollTop. Only a deliberate upward movement
      // turns following off; mutations never infer intent from the new height.
      if (container.scrollTop < previousTop - 2 && container.clientHeight === previousClientHeight) following = false;
      if (returnedToBottom() || atBottom()) following = true;
      previousTop = container.scrollTop;
      previousClientHeight = container.clientHeight;
    };
    container.addEventListener('scroll', onScroll);
    const resize = () => {
      if (paused) return;
      // Scroll events are dispatched on a later frame. Observe an upward user
      // move before a same-frame mutation can pull the viewer back down.
      const height = container.scrollHeight;
      // Likewise, a user can return to the previous bottom before the scroll
      // event is dispatched and new output increases the current height.
      if (returnedToBottom()) following = true;
      if (container.scrollTop < previousTop - 2 && height >= previousHeight && container.clientHeight === previousClientHeight) following = false;
      previousHeight = height;
      previousClientHeight = container.clientHeight;
      if (!following) return;
      container.scrollTop = container.scrollHeight;
      previousTop = container.scrollTop;
    };
    const mutations = new MutationObserver(resize);
    mutations.observe(container, {childList: true, subtree: true, characterData: true});
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    for (const child of container.children) observer.observe(child);
    container.addEventListener('load', resize, true);
    const onWheel = event => {
      if (paused) return;
      if (event.deltaY < 0 && event.target.closest('.history, .output-clip') === container) following = false;
    };
    container.addEventListener('wheel', onWheel, {passive: true});
    return {
      pause() {
        if (!paused) saved = {following, top: container.scrollTop};
        paused = true;
        return {...saved};
      },
      resume(position = saved) {
        following = position.following;
        paused = false;
        container.scrollTop = following ? container.scrollHeight : position.top;
        previousTop = container.scrollTop;
        previousHeight = container.scrollHeight;
        previousClientHeight = container.clientHeight;
      },
      dispose() {
        mutations.disconnect();
        observer.disconnect();
        container.removeEventListener('scroll', onScroll);
        container.removeEventListener('load', resize, true);
        container.removeEventListener('wheel', onWheel);
      },
    };
  }
  function sessionView(message) {
    if (!message.session || !message.incarnation) throw new Error('Console session identity is required.');
    if (closedViews.has(message.incarnation)) return null;
    let view = sessionViews.get(message.incarnation);
    if (!view) {
      const transcript = element('div', 'session-transcript');
      const plotsNode = element('div', 'session-plots');
      const omissions = element('div', 'session-omissions');
      for (const node of [transcript, plotsNode, omissions]) {
        node.dataset.session = message.session;
        node.dataset.incarnation = message.incarnation;
        node.hidden = true;
      }
      document.querySelector('#transcript').append(transcript);
      document.querySelector('#plots').append(plotsNode);
      document.querySelector('#output-omissions').append(omissions);
      view = {name: message.session, incarnation: message.incarnation, transcript, plotsNode, omissions,
        entries: new Map(), plots: new Map(), agentRequests: new Map(), idleOwners: new Map(), responses: new Set(),
        scrollers: new Set(), observers: new Set(), formattedView: true, scrollPosition: null};
      sessionViews.set(message.incarnation, view);
    }
    return view;
  }
  function showInspectors() {
    for (const node of document.querySelectorAll('.session-inspector')) {
      node.hidden = node.dataset.incarnation !== selectedView?.incarnation;
    }
  }
  function selectSession(message) {
    const view = sessionView(message);
    if (!view || view === selectedView) return;
    if (selectedView) {
      selectedView.scrollPosition = historyScroll.pause();
      for (const scroller of selectedView.scrollers) scroller.pause();
      for (const node of [selectedView.transcript, selectedView.plotsNode, selectedView.omissions]) node.hidden = true;
    }
    selectedView = view;
    for (const node of [view.transcript, view.plotsNode, view.omissions]) node.hidden = false;
    showInspectors();
    const sourceView = view.formattedView ? 'formatted' : 'original';
    document.querySelector(`#source_view input[value="${sourceView}"]`).checked = true;
    for (const scroller of view.scrollers) scroller.resume();
    historyScroll.resume(view.scrollPosition || {following: true, top: 0});
  }
  function closeSession(message) {
    closedViews.add(message.incarnation);
    const view = sessionViews.get(message.incarnation);
    if (!view) return;
    if (view === selectedView) {
      historyScroll.pause();
      selectedView = null;
    }
    for (const scroller of view.scrollers) scroller.dispose();
    for (const observer of view.observers) observer.disconnect();
    for (const node of [view.transcript, view.plotsNode, view.omissions]) node.remove();
    sessionViews.delete(message.incarnation);
    showInspectors();
  }
  function renderSource(item) {
    const format = sourceFormats.get(item.formatKey) || item.format;
    const useFormatted = item.view.formattedView && format?.status === 'formatted';
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
      ? (item.view.formattedView ? `Formatted with ${formatter}` : `Original source · ${formatter} formatting available`)
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
  function updateExpansion(clip, content, button, expanded) {
    if (!button) return;
    const limit = parseFloat(getComputedStyle(clip).getPropertyValue('--output-preview-height'));
    button.hidden = content.scrollHeight <= limit;
    button.textContent = expanded ? 'Collapse' : 'Expand';
    button.setAttribute('aria-expanded', String(expanded));
    clip.classList.toggle('expanded', expanded);
  }
  function updateOutput(item) {
    const agentView = item.controls.querySelector('input:checked')?.value === 'agent';
    item.content.hidden = agentView;
    item.agentOutput.hidden = !agentView;
    item.responseStatus.textContent = item.responseIds.size ? '' :
      item.meta.origin === 'mcp' ? 'No response received from this tool call yet.' : 'Not sent to agent.';
    const retained = item.controls.querySelector('.output-retained');
    if (retained) retained.disabled = item.meta.call_id == null;
    updateExpansion(item.clip, agentView ? item.agentOutput : item.content,
      item.controls.querySelector('.output-expand'), item.expanded);
  }
  function responseBlock(block) {
    if (block.type === 'text') return element('pre', 'response-text', block.text);
    if (block.type === 'image') return output(block);
    // Preserve unfamiliar MCP block shapes instead of silently dropping them.
    return element('pre', 'response-block', JSON.stringify(block, null, 2));
  }
  function responseNode(message) {
    const node = element('section', 'agent-response');
    node.dataset.response = message.id;
    node.append(element('strong', 'response-label', `${message.label} · response to agent`));
    if (message.status) node.append(element('small', 'response-status', message.status));
    const content = element('div', 'response-content');
    for (const block of message.content) content.append(responseBlock(block));
    if (message.structuredContent != null) {
      content.append(element('pre', 'response-structured', JSON.stringify(message.structuredContent, null, 2)));
    }
    if (message.error) content.append(element('pre', 'execution-error', message.error));
    node.append(content);
    return node;
  }
  function attachResponse(item, message) {
    if (item.responseIds.has(message.id)) return;
    item.responseIds.add(message.id);
    item.agentOutput.append(responseNode(message));
    updateOutput(item);
  }
  function entry(meta, view = sessionView(meta)) {
    if (!view) return null;
    const key = String(meta.id);
    let existing = view.entries.get(key);
    const provisionalAgent = key.startsWith('agent:');
    if (!existing && meta.origin === 'mcp') {
      // The SDK response and captured output travel independently. Successful
      // response-only entries remain candidates until their snapshot arrives.
      // A failed tool call without associated capture stays separate: source
      // matching cannot distinguish it from a later retry of identical code.
      const match = [...view.entries.entries()].find(([otherKey, value]) =>
        value.meta?.origin === 'mcp' && value.meta.language === meta.language &&
        (value.source === meta.source || (meta.source_truncated && value.source.startsWith(meta.source)) ||
          (value.sourceTruncated && meta.source.startsWith(value.source))) && !value.agentMatched &&
        (provisionalAgent || (!value.toolFailed && value.meta.state !== 'retired')) &&
        otherKey.startsWith('agent:') !== provisionalAgent);
      if (match) {
        existing = match[1];
        existing.agentMatched = true;
        if (provisionalAgent) {
          view.agentRequests.set(key, existing);
          if (existing.sourceTruncated) captureSource(existing, meta);
          return existing;
        }
        view.entries.delete(match[0]);
      }
    }
    if (!existing && meta.request_id) {
      existing = view.entries.get('request:' + meta.request_id);
      if (existing) view.entries.delete('request:' + meta.request_id);
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
      const controls = element('div', 'output-toolbar');
      controls.innerHTML = meta.output_controls || '';
      const clip = element('div', 'output-clip');
      const agentOutput = element('div', 'agent-responses');
      const responseStatus = element('p', 'response-empty');
      agentOutput.append(responseStatus);
      agentOutput.hidden = true;
      clip.append(content, agentOutput);
      const outputRegion = element('div', 'output-region');
      outputRegion.append(controls, clip);
      node.append(submission, outputRegion);
      view.transcript.append(node);
      view.scrollers.add(followBottom(clip, view !== selectedView));
      existing = {view, node, origin, language, state, truncation, code, formatStatus, content,
        controls, clip, agentOutput, responseStatus, expanded: false, responseIds: new Set(),
        sourceId: `transcript-source-${++nextSourceId}`, events: new Map()};
      const item = existing;
      controls.addEventListener('change', () => updateOutput(item));
      controls.querySelector('.output-expand')?.addEventListener('click', () => {
        item.expanded = !item.expanded;
        updateOutput(item);
      });
      controls.querySelector('.output-retained')?.addEventListener('click', () => {
        Shiny.setInputValue('retained_output_request', {session: view.name, incarnation: view.incarnation,
          call_id: item.meta.call_id, nonce: crypto.randomUUID()}, {priority: 'event'});
      });
      const resize = new ResizeObserver(() => updateOutput(item));
      resize.observe(content);
      resize.observe(agentOutput);
      view.observers.add(resize);
      clip.addEventListener('load', () => updateOutput(item), true);
    }
    if (existing.pendingOutcome && !provisionalAgent) {
      existing.content.replaceChildren();
      existing.pendingOutcome = false;
    }
    view.entries.set(key, existing);
    if (existing.meta?.state === 'complete' && meta.state === 'running') meta = {...meta, state: 'complete'};
    existing.meta = meta;
    existing.actual = !key.startsWith('agent:') && !key.startsWith('request:');
    if (provisionalAgent) view.agentRequests.set(key, existing);
    existing.node.dataset.evaluation = key;
    existing.node.className = `entry ${meta.origin === 'mcp' ? 'agent' : meta.origin === 'session' ? 'session' : 'direct'} ${meta.state}`;
    existing.origin.textContent = meta.origin === 'mcp' ? 'Agent' : meta.origin === 'session' ? 'Session' : 'Human';
    existing.language.textContent = meta.language === 'r' ? 'R' : meta.language.toUpperCase();
    existing.state.textContent = `${meta.execution === 'immediate' ? 'Immediate · ' : ''}${meta.state}`;
    captureSource(existing, meta);
    updateOutput(existing);
    return existing;
  }
  function output(event) {
    if (['stdout', 'stderr', 'output', 'diagnostic', 'notice'].includes(event.type)) {
      const node = element('span', `stream ${event.type}`, event.text);
      if (event.omitted_bytes) node.append(element('span', 'omission', `\n${event.omitted_bytes} bytes omitted from this captured event.\n`));
      return node;
    }
    if (event.type === 'execution_error') {
      const error = event.error;
      const traceback = error.traceback?.length ? '\n' + error.traceback.join('\n') : '';
      return element('pre', 'execution-error', `${error.ename}: ${error.evalue}${traceback}${error.truncated ? '\nError details were truncated.' : ''}`);
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
    const view = sessionView(message);
    if (!view) return;
    if (message.reset) {
      view.plots.clear();
      view.plotsNode.replaceChildren();
      // A new worker snapshot confirms retirement of the previous worker.
      // Its final metadata may have been coalesced out of the subscription.
      for (const item of view.entries.values()) {
        if (!['running', 'awaiting_capture'].includes(item.meta.state)) continue;
        item.meta = {...item.meta, state: 'retired'};
        item.node.classList.remove('running', 'awaiting_capture');
        item.node.classList.add('retired');
        item.state.textContent = 'Stopped · session replaced';
      }
    }
    for (const meta of message.evaluations) entry(meta, view);
    let idleOwner = null;
    for (const event of message.events) {
      const eventKey = `${message.worker_id}:${event.evaluation_id}:${event.sequence}`;
      let owner = view.entries.get(String(event.evaluation_id));
      if (event.evaluation_id === null) {
        idleOwner = view.idleOwners.get(eventKey) || idleOwner || entry({id: 'idle:' + eventKey,
          origin: 'session', language: '', source: '', execution: 'serial', state: 'idle'}, view);
        owner = idleOwner;
        view.idleOwners.set(eventKey, owner);
      } else idleOwner = null;
      if (!owner) continue;
      const serialized = JSON.stringify(event);
      const prior = owner.events.get(eventKey);
      if (prior?.serialized === serialized) continue;
      const node = output(event);
      if (!node) continue;
      if (prior) prior.node.replaceWith(node); else owner.content.append(node);
      owner.events.set(eventKey, {node, serialized});
      updateOutput(owner);
      if (event.type === 'image' && !view.plots.has(eventKey)) {
        view.plots.set(eventKey, true);
        const figure = element('figure');
        figure.append(output(event), element('figcaption', '', `${owner.language.textContent} · ${owner.origin.textContent}`));
        view.plotsNode.append(figure);
      }
    }
    view.omissions.textContent = message.omitted_events || message.omitted_bytes
      ? `${message.omitted_events} events and ${message.omitted_bytes} bytes omitted from the bounded server snapshot.` : '';
  }
  document.addEventListener('DOMContentLoaded', () => {
    historyScroll = followBottom(document.querySelector('#transcript-history'), true);
    for (const id of ['session-tables', 'session-environments']) {
      new MutationObserver(showInspectors).observe(document.getElementById(id), {childList: true});
    }
    document.querySelector('#source_view').addEventListener('change', () => {
      if (!selectedView) return;
      selectedView.formattedView = document.querySelector('#source_view input:checked').value === 'formatted';
      for (const item of selectedView.entries.values()) renderSource(item);
    });
    document.querySelector('#plot-close').onclick = () => document.querySelector('#plot-dialog').close();
    document.addEventListener('click', event => {
      const copy = event.target.closest('[data-export-format]');
      if (copy) {
        const status = document.querySelector('#export_status');
        const label = copy.dataset.exportFormat === 'qmd' ? 'QMD' : 'Markdown';
        const view = selectedView;
        if (!view) {
          status.textContent = 'Copy failed: no Console session is selected.';
          return;
        }
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
        Shiny.setInputValue('export_request', {id, format: copy.dataset.exportFormat,
          session: view.name, incarnation: view.incarnation}, {priority: 'event'});
      }
      const button = event.target.closest('[data-table-language]');
      if (button) Shiny.setInputValue('open_table', {language: button.dataset.tableLanguage,
        name: button.dataset.tableName, session: button.dataset.session, incarnation: button.dataset.incarnation,
        nonce: Date.now()}, {priority: 'event'});
    });
    Shiny.addCustomMessageHandler('session-selected', selectSession);
    Shiny.addCustomMessageHandler('session-closed', closeSession);
    Shiny.addCustomMessageHandler('submission', entry);
    Shiny.addCustomMessageHandler('formatted-source', message => {
      sourceFormats.set(message.format_key, message);
      for (const view of sessionViews.values()) {
        for (const item of view.entries.values()) {
          if (item.formatKey === message.format_key) renderSource(item);
        }
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
    Shiny.addCustomMessageHandler('agent-response', message => {
      const view = sessionView(message);
      if (!view || view.responses.has(message.id)) return;
      view.responses.add(message.id);
      if (message.submission_ids.length) {
        for (const id of message.submission_ids) {
          const item = view.agentRequests.get(id);
          if (item) attachResponse(item, message);
        }
      } else {
        // Polls and controls can collect several output intervals. Keep their
        // response boundary instead of assigning them to the most recent cell.
        const node = element('article', 'response-entry');
        const response = responseNode(message);
        const content = response.querySelector('.response-content');
        const controls = element('div');
        controls.innerHTML = message.output_controls;
        const clip = element('div', 'output-clip');
        clip.append(content);
        response.append(controls, clip);
        let expanded = false;
        const expand = controls.querySelector('.output-expand');
        const resize = () => updateExpansion(clip, content, expand, expanded);
        expand.addEventListener('click', () => { expanded = !expanded; resize(); });
        const observer = new ResizeObserver(resize);
        observer.observe(content);
        view.observers.add(observer);
        node.append(response);
        view.transcript.append(node);
        view.scrollers.add(followBottom(clip, view !== selectedView));
        resize();
      }
    });
    Shiny.addCustomMessageHandler('agent-outcome', message => {
      const view = sessionView(message);
      if (!view) return;
      for (const id of message.ids) {
        const item = view.agentRequests.get(id);
        if (item && !item.actual && item.meta.state !== 'retired') {
          item.toolFailed = message.status === 'failed';
          item.meta.state = item.toolFailed ? 'failed' : 'awaiting_capture';
          item.node.className = `entry agent ${item.meta.state}`;
          item.state.textContent = item.toolFailed ? 'Tool call failed' : 'Response received · awaiting capture';
          item.content.textContent = message.text;
          item.pendingOutcome = true;
          updateOutput(item);
        }
      }
    });
    Shiny.addCustomMessageHandler('notice', message => {
      const view = sessionView(message);
      if (!view) return;
      const node = element('article', `notice ${message.kind}`, message.text);
      view.transcript.append(node);
    });
  });
})();
