/* The browser edits a viewing; the laptop owns the files and model connection. */
const $ = (id) => document.getElementById(id);
const state = { item: null, watchedEntryId: null, movies: [], template: '', dirtyNotes: false, dirtyReview: false, busy: false, polling: false, action: false, watched: [], collection: 'writing', sync: {}, syncPolling: false };
let autosaveTimer;
let saving = null;
let saveError = '';
let savedNotes = '';
let savedReview = '';

function notesInput() {
  return { title: $('title').value, watched: $('watched').value, rating: $('rating').value, notes: $('notes').value };
}

function rememberSaved() {
  savedNotes = JSON.stringify(notesInput());
  savedReview = $('review').value;
  saveError = '';
  state.dirtyNotes = false;
  state.dirtyReview = false;
}

function checkChanges() {
  // Compare actual text so opening a movie or undoing an edit is not a change.
  state.dirtyNotes = JSON.stringify(notesInput()) !== savedNotes;
  state.dirtyReview = $('review').value !== savedReview;
}

function scheduleSave() {
  checkChanges();
  saveError = '';
  clearTimeout(autosaveTimer);
  updateButtons();
  autosaveTimer = setTimeout(() => {
    // Generation owns the files until its job finishes; save edits afterwards.
    if (state.busy || state.action) return;
    if (!$('title').value.trim() || !$('watched').value) return;
    flushSave().catch((error) => status(`Auto-save failed. Your edits are still here. ${error.message}`, 'error'));
  }, 700);
}

async function flushSave() {
  clearTimeout(autosaveTimer);
  // Every caller shares one save queue, so revisions and responses stay in order.
  if (saving) await saving;
  if (!dirty()) return;
  if (state.busy) throw new Error('Wait for the draft to finish before saving your edits.');
  saving = (async () => {
    while (dirty()) {
      if (state.dirtyNotes) {
        const notes = notesInput();
        const creating = !state.item;
        if (creating) {
          // The title and date identify the file and must stay fixed during creation.
          $('title').readOnly = true;
          $('watched').readOnly = true;
        }
        try {
          state.item = await api('save-notes', { ...identity(), ...notes });
          savedNotes = JSON.stringify(notes);
          $('title').title = 'Use + to create a different movie or viewing.';
          $('page-title').textContent = state.item.title;
          $('draft-badge').textContent = state.item.has_draft ? 'DRAFT READY' : 'NOTES SAVED';
        } catch (error) {
          if (creating) { $('title').readOnly = false; $('watched').readOnly = false; }
          throw error;
        }
      }
      checkChanges();
      if (state.dirtyReview) {
        const review = $('review').value;
        state.item = await api('save-review', { ...identity(), review });
        savedReview = review;
      }
      // Keep anything typed while the request was in flight and save it next.
      checkChanges();
    }
    await refreshLibrary();
    saveError = '';
  })();
  updateButtons();
  try { await saving; }
  catch (error) { saveError = 'Could not save — edits kept here'; throw error; }
  finally { saving = null; updateButtons(); }
}

async function api(path, data) {
  const options = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Review-Desk': '1' }, body: JSON.stringify(data) };
  const response = await fetch(`/api/${path}`, options);
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || 'Something went wrong. Please try again.');
    error.status = response.status;
    throw error;
  }
  return result;
}

function status(message, kind = '') {
  $('status').textContent = message;
  $('status').className = `status ${kind}`;
}

function dateLabel(value) {
  if (!value) return 'Watch date needed';
  return new Date(`${value}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function dirty() {
  return state.dirtyNotes || state.dirtyReview;
}

function updateButtons() {
  const disabled = state.busy || state.action;
  ['generate', 'feedback', 'copy'].forEach((id) => { $(id).disabled = disabled; });
  // Navigation and explicit actions may replace the editor; autosave leaves it editable.
  ['title', 'watched', 'rating', 'notes', 'review'].forEach((id) => { $(id).disabled = state.action; });
  $('generate').textContent = state.busy ? 'Generating…' : (state.item?.has_draft ? 'Regenerate review ↗' : 'Generate review ↗');
  $('save-state').textContent = saveError || (saving ? 'Saving…' : (dirty() ? 'Waiting to save…' : (state.item ? 'All changes saved' : 'Edits save automatically')));
}

function resizeReview() {
  const review = $('review');
  // Hidden tabs cannot be measured. Size the review after it becomes visible.
  if (!review.getClientRects().length) return;
  // Collapse before measuring: an automatic height can retain extra space in
  // Safari. scrollHeight then gives just the text height plus normal padding.
  review.style.height = '0px';
  review.style.height = `${review.scrollHeight}px`;
}

function tab(name) {
  const notes = name === 'notes';
  $('notes-panel').hidden = !notes;
  $('review-panel').hidden = notes;
  $('notes-tab').setAttribute('aria-selected', String(notes));
  $('review-tab').setAttribute('aria-selected', String(!notes));
  resizeReview();
}

function renderLibrary() {
  const query = $('search').value.toLocaleLowerCase();
  const list = $('movies');
  list.replaceChildren();
  const source = state.collection === 'watched' ? state.watched : state.movies;
  $('count').textContent = ` ${source.length}`;
  $('watched-hint').hidden = state.collection !== 'watched';
  source.filter((movie) => movie.title.toLocaleLowerCase().includes(query)).forEach((movie, index) => {
    const button = document.createElement('button');
    const localId = state.collection === 'watched' ? movie.local_id : movie.id;
    const selected = state.collection === 'watched' ? isCurrentViewing(movie) : Boolean(localId && state.item?.id === localId);
    button.className = `movie${selected ? ' active' : ''}`;
    button.setAttribute('aria-current', String(selected));
    const number = document.createElement('span');
    number.className = 'number';
    number.textContent = String(index + 1).padStart(2, '0');
    const words = document.createElement('span');
    const title = document.createElement('span');
    title.className = 'movie-title';
    title.textContent = `${movie.title}${state.collection === 'watched' && movie.year ? ` (${movie.year})` : ''}`;
    const meta = document.createElement('span');
    meta.className = 'movie-meta';
    meta.textContent = `${dateLabel(movie.watched)}${movie.rating == null ? '' : ` · ${movie.rating} ★`}${movie.rewatch ? ' · Rewatch' : ''}`;
    words.append(title, meta);
    const dot = document.createElement('span');
    const hasWriting = state.collection === 'watched' ? Boolean(localId) : movie.has_draft;
    dot.className = `state-dot${hasWriting ? ' drafted' : ''}`;
    dot.title = state.collection === 'watched' ? (hasWriting ? 'Writing started' : 'Start notes') : (movie.has_draft ? 'Draft ready' : 'Notes saved');
    button.append(number, words, dot);
    button.addEventListener('click', () => action(() => state.collection === 'watched' ? startFromWatched(movie) : openMovie(movie.id)));
    list.append(button);
  });
  if (!list.children.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = query ? 'No matching movies.' : (state.collection === 'watched' ? 'Sync Letterboxd to bring in recent viewings.' : 'Your first review belongs here.');
    list.append(empty);
  }
}

async function refreshLibrary() {
  const result = await api('library');
  state.movies = result.movies;
  state.watched = result.watched || [];
  state.sync = result.sync || {};
  state.template = result.template;
  renderLibrary();
  renderSync();
  if (state.sync.state === 'running') pollSync();
}

function renderSync() {
  const sync = state.sync;
  const connected = Boolean(sync.username);
  const running = sync.state === 'running';
  $('sync-account').textContent = connected ? `@${sync.username}` : 'Not connected';
  $('sync-connect').hidden = connected;
  $('sync-button').hidden = !connected;
  $('sync-button').disabled = running;
  $('sync-button').textContent = running ? 'Syncing…' : '↻ Sync Letterboxd';
  let message = sync.message || 'Connect your Letterboxd profile.';
  if (sync.last_success && !running) {
    const last = new Date(sync.last_success).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    message += ` Last synced ${last}.`;
  }
  $('sync-status').textContent = message;
  $('sync-status').className = sync.state === 'error' ? 'error' : '';
}

function collection(name) {
  state.collection = name;
  $('writing-list-tab').setAttribute('aria-selected', String(name === 'writing'));
  $('watched-list-tab').setAttribute('aria-selected', String(name === 'watched'));
  renderLibrary();
}

function isCurrentViewing(movie) {
  // Unsaved notes have no local file ID yet, so remember the diary entry too.
  return Boolean((movie.local_id && movie.local_id === state.item?.id)
    || (movie.entry_id && movie.entry_id === state.watchedEntryId));
}

async function startFromWatched(movie) {
  if (isCurrentViewing(movie)) return;
  if (movie.local_id) return openMovie(movie.local_id);
  await flushSave();
  displayItem(null);
  state.watchedEntryId = movie.entry_id;
  $('title').value = movie.title;
  $('watched').value = movie.watched;
  $('rating').value = movie.rating ?? '';
  $('page-title').textContent = movie.title;
  $('viewing-label').textContent = `Watched ${dateLabel(movie.watched)}`;
  rememberSaved();
  updateButtons();
  renderLibrary();
  tab('notes');
  status('Title, watch date, and available rating are filled in. Your edits save automatically.');
  if (window.innerWidth <= 760) $('workspace').scrollIntoView({ behavior: 'smooth' });
}

async function syncLetterboxd(username) {
  // Updating history does not replace the editor or discard unsaved writing.
  state.sync = await api('sync', username ? { username } : {});
  renderSync();
  pollSync();
}

async function pollSync() {
  if (state.syncPolling) return;
  state.syncPolling = true;
  try {
    let sync = await api('sync-status');
    while (sync.state === 'running') {
      state.sync = sync;
      renderSync();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      sync = await api('sync-status');
    }
    await refreshLibrary();
  } catch {
    state.sync = { ...state.sync, state: 'error', message: 'Could not check sync progress. Reload to reconnect; your saved history stays available.' };
    renderSync();
  } finally {
    state.syncPolling = false;
  }
}

function displayItem(item) {
  state.item = item;
  state.watchedEntryId = null;
  state.dirtyNotes = false;
  state.dirtyReview = false;
  $('title').value = item?.title || '';
  const now = new Date();
  $('watched').value = item?.watched || `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  $('rating').value = item?.rating ?? '';
  $('title').readOnly = Boolean(item);
  $('watched').readOnly = Boolean(item);
  $('title').title = item ? 'Use + to create a different movie or viewing.' : '';
  $('notes').value = item?.notes ?? state.template;
  $('review').value = item?.review || '';
  $('page-title').textContent = item?.title || 'What did you watch?';
  $('viewing-label').textContent = item ? `Watched ${dateLabel(item.watched)}` : 'A fresh page';
  $('draft-badge').textContent = item?.has_draft ? 'DRAFT READY' : (item ? 'NOTES SAVED' : 'NEW VIEWING');
  $('review').hidden = !item?.has_draft;
  $('review-actions').hidden = !item?.has_draft;
  $('no-review').hidden = Boolean(item?.has_draft);
  rememberSaved();
  resizeReview();
  updateButtons();
  renderLibrary();
}

async function openMovie(id, force = false) {
  // Selecting the open file should keep its editor contents and current tab.
  if (!force && state.item?.id === id) return;
  await flushSave();
  displayItem(await api(`movie?id=${encodeURIComponent(id)}`));
  tab(state.item.has_draft ? 'review' : 'notes');
  status('');
  if (window.innerWidth <= 760) $('workspace').scrollIntoView({ behavior: 'smooth' });
}

async function newMovie() {
  await flushSave();
  displayItem(null);
  tab('notes');
  status('Start with a title, watch date, and a few thoughts.');
  $('workspace').scrollIntoView({ behavior: 'smooth' });
  $('title').focus({ preventScroll: true });
}

function identity() {
  return { id: state.item?.id, revision: state.item?.revision };
}

async function saveNotes() {
  if (!state.item && !dirty()) state.dirtyNotes = true;
  await flushSave();
}

async function saveReview() {
  await flushSave();
  if (!state.item?.has_draft) throw new Error('Generate a review first.');
  status('Review saved. Your Letterboxd copy is up to date.');
}

async function generate() {
  const replace = Boolean(state.item?.has_draft);
  if (replace && !confirm('Generate a new draft? The current saved draft will be archived on your laptop.')) return;
  await saveNotes();
  const job = await api('generate', { ...identity(), replace });
  state.dirtyReview = false;
  state.busy = true;
  status(job.message, 'running');
  updateButtons();
  pollJob();
}

async function pollJob() {
  if (state.polling) return;
  state.polling = true;
  try {
    let job = await api('job');
    while (job.state === 'running') {
      state.busy = true;
      status(job.message, 'running');
      updateButtons();
      await new Promise((resolve) => setTimeout(resolve, 1800));
      job = await api('job');
    }
    state.busy = false;
    if (job.state === 'done') {
      await refreshLibrary();
      if (state.item?.id === job.id && dirty()) {
        // Generation changes the revision. Preserve the editor and save its edits
        // against the completed draft rather than replacing the user's text.
        state.item = await api(`movie?id=${encodeURIComponent(job.id)}`);
        if (!state.dirtyReview) { $('review').value = state.item.review; savedReview = state.item.review; }
        $('review').hidden = false;
        $('review-actions').hidden = false;
        $('no-review').hidden = true;
        await flushSave();
      }
      if (state.item?.id === job.id && !state.dirtyNotes && !state.dirtyReview) {
        displayItem(await api(`movie?id=${encodeURIComponent(job.id)}`));
        tab('review');
      } else if (state.item?.id === job.id) {
        status('A new draft is ready. Save or discard your current edits, then reopen this movie.');
        return;
      }
      status(job.message);
    } else if (job.state === 'error') status(job.message, 'error');
  } catch (error) {
    status(`Lost contact with your laptop. ${error.message} Reload this page to check the draft.`, 'error');
    state.busy = false;
  } finally {
    state.polling = false;
    updateButtons();
    if (dirty() && !saveError) scheduleSave();
  }
}

async function copyReview() {
  await saveReview();
  const text = state.item.copy_text;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      // Keep formatting separate from plain text so tags never become review text.
      if (navigator.clipboard.write && window.ClipboardItem && state.item.copy_html) {
        await navigator.clipboard.write([new ClipboardItem({
          'text/plain': new Blob([text], { type: 'text/plain' }),
          'text/html': new Blob([state.item.copy_html], { type: 'text/html' }),
        })]);
      } else {
        await navigator.clipboard.writeText(text);
      }
    } else {
      // Phones on local HTTP may not have the modern clipboard API.
      $('copy-text').value = text;
      $('copy-dialog').showModal();
      $('copy-text').focus();
      $('copy-text').select();
      $('copy-text').setSelectionRange(0, text.length);
      if (!document.execCommand('copy')) return;
      $('copy-dialog').close();
    }
    status('Copied. Editors that support formatted paste keep title italics.');
  } catch {
    $('copy-text').value = text;
    if (!$('copy-dialog').open) $('copy-dialog').showModal();
    $('copy-text').focus();
    $('copy-text').select();
  }
}

async function phoneSetup() {
  const result = await api('phone');
  $('phone-code').textContent = result.code;
  $('phone-addresses').replaceChildren();
  if (!result.lan) {
    $('phone-addresses').textContent = 'Restart using Open Review Desk.command to enable phone access.';
  } else if (!result.urls.length) {
    $('phone-addresses').textContent = 'Connect the laptop to Wi-Fi, then reopen Phone setup.';
  } else {
    result.urls.forEach((url) => {
      const line = document.createElement('div');
      const link = document.createElement('a');
      link.href = url;
      link.textContent = url;
      line.append(link);
      $('phone-addresses').append(line);
    });
  }
  $('phone-dialog').showModal();
}

async function action(fn) {
  if (state.action) return;
  state.action = true;
  updateButtons();
  try { await fn(); }
  catch (error) { status(error.message, 'error'); }
  finally { state.action = false; updateButtons(); if (dirty() && !saveError) scheduleSave(); }
}

async function start() {
  try {
    await refreshLibrary();
    $('pairing').hidden = true;
    $('app').hidden = false;
    // Only the laptop can display the pairing code.
    $('phone-button').hidden = !['localhost', '127.0.0.1'].includes(location.hostname);
    if (state.movies.length) await openMovie(state.movies[0].id);
    else displayItem(null);
    pollJob();
  } catch (error) {
    if (error.status === 401) {
      $('pairing').hidden = false;
      $('app').hidden = true;
      $('phone-button').hidden = true;
    } else {
      $('app').hidden = false;
      status(error.message, 'error');
    }
  }
}

$('notes-tab').addEventListener('click', () => tab('notes'));
$('review-tab').addEventListener('click', () => tab('review'));
$('back-to-notes').addEventListener('click', () => tab('notes'));
$('new-button').addEventListener('click', () => action(newMovie));
$('search').addEventListener('input', renderLibrary);
$('writing-list-tab').addEventListener('click', () => collection('writing'));
$('watched-list-tab').addEventListener('click', () => collection('watched'));
$('sync-button').addEventListener('click', () => action(() => syncLetterboxd()));
$('sync-connect').addEventListener('submit', (event) => { event.preventDefault(); action(() => syncLetterboxd($('sync-username').value.trim())); });
['title', 'watched', 'rating', 'notes'].forEach((id) => $(id).addEventListener('input', scheduleSave));
$('review').addEventListener('input', () => { resizeReview(); scheduleSave(); });
$('generate').addEventListener('click', () => action(generate));
$('copy').addEventListener('click', () => action(copyReview));
$('feedback').addEventListener('click', () => action(async () => { await saveReview(); const result = await api('feedback', identity()); status(result.message); }));
$('phone-button').addEventListener('click', () => action(phoneSetup));
$('close-phone').addEventListener('click', () => $('phone-dialog').close());
$('close-copy').addEventListener('click', () => $('copy-dialog').close());
$('select-copy').addEventListener('click', () => { $('copy-text').focus(); $('copy-text').select(); $('copy-text').setSelectionRange(0, $('copy-text').value.length); });
$('pair-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await api('pair', { code: $('pair-code').value }); $('pair-error').textContent = ''; await start(); }
  catch (error) { $('pair-error').textContent = error.message; }
});
// Reflow the text when the window width or phone orientation changes.
window.addEventListener('resize', resizeReview);
window.addEventListener('beforeunload', (event) => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
start();
