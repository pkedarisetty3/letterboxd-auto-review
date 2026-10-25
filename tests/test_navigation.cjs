/* Run with node --test tests/test_navigation.cjs. No browser or server needed. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function editor() {
  // A small DOM substitute lets us exercise the real navigation handlers safely.
  const elements = new Map();
  const element = () => ({
    value: '', children: [], style: {}, listeners: {},
    getClientRects() { return [1]; }, scrollHeight: 410,
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; },
    scrollIntoView() {}, focus() {},
  });
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element,
    },
    window: { innerWidth: 1000, addEventListener() {} },
    setTimeout() { return 1; }, clearTimeout() {},
    confirm() { throw new Error('Unexpected discard prompt'); },
    fetch() { throw new Error('Unexpected reload'); },
  });
  const source = fs.readFileSync(path.join(__dirname, '../src/letterboxd_auto_review/static/app.js'), 'utf8');
  // Skip application startup, which normally loads real data from the server.
  vm.runInContext(source.replace(/start\(\);\s*$/, ''), context);
  return (code) => vm.runInContext(code, context);
}

test('reselecting an unsaved watched entry preserves writing and selection', async () => {
  const run = editor();
  await run(`
    const movie = { entry_id: 'first-viewing', title: 'Best of the Best', watched: '2026-10-24' };
    state.watched = [movie];
    collection('watched');
    startFromWatched(movie);
  `);
  run(`$('notes').value = 'Keep my thoughts'; $('review').value = 'Keep my review'; state.dirtyReview = true; tab('review');`);
  await run('startFromWatched({ ...movie })');
  assert.equal(run("$('notes').value"), 'Keep my thoughts');
  assert.equal(run("$('review').value"), 'Keep my review');
  assert.equal(run("$('review-panel').hidden"), false);
  assert.equal(run("$('movies').children[0]['aria-current']"), 'true');
  assert.equal(run('dirty()'), true);
});

test('browsing untouched watched entries never creates changes or prompts', async () => {
  const run = editor();
  await run(`startFromWatched({ entry_id: 'digger', title: 'Digger', watched: '2026-10-24' })`);
  assert.equal(run('dirty()'), false);
  await run(`startFromWatched({ entry_id: 'ad-astra', title: 'Ad Astra', watched: '2026-10-25' })`);
  assert.equal(run('state.watchedEntryId'), 'ad-astra');
  assert.equal(run('dirty()'), false);
});

test('reselecting a saved viewing through either collection preserves edits', async () => {
  const run = editor();
  run(`displayItem({ id: 'saved', title: 'Best of the Best', watched: '2026-10-24', notes: 'Saved' });
    $('notes').value = 'Unsaved edit'; state.dirtyNotes = true;`);
  await run(`startFromWatched({ entry_id: 'first', local_id: 'saved' })`);
  await run(`openMovie('saved')`);
  assert.equal(run("$('notes').value"), 'Unsaved edit');
  assert.equal(run('state.dirtyNotes'), true);
});

test('starting a new page clears the previous watched selection', async () => {
  const run = editor();
  await run(`startFromWatched({ entry_id: 'first', title: 'Best of the Best', watched: '2026-10-24' })`);
  await run('newMovie();');
  assert.equal(run('state.watchedEntryId'), null);
  assert.equal(run("isCurrentViewing({ entry_id: 'first' })"), false);
});

function savedEditor() {
  const run = editor();
  run(`
    const original = { id: 'digger', title: 'Digger', watched: '2026-10-24', rating: 3,
      notes: 'Original notes', review: 'Original review', has_draft: true, revision: '0' };
    displayItem(original);
    let requests = [];
    let revision = 0;
    let disk = { ...original };
    api = async (route, data) => {
      requests.push({ route, data });
      if (route === 'library') return { movies: [], template: '' };
      if (route.startsWith('movie?')) return { ...disk, id: 'ad-astra', title: 'Ad Astra' };
      if (data.revision !== disk.revision) throw Error('Stale revision');
      disk = { ...disk, ...data, revision: String(++revision) };
      return { ...disk };
    };
  `);
  return run;
}

test('switching movies saves notes and review in revision order', async () => {
  const run = savedEditor();
  run(`$('notes').value = 'New notes'; $('review').value = 'New review'; scheduleSave();`);
  await run(`openMovie('ad-astra')`);
  assert.equal(run('disk.notes'), 'New notes');
  assert.equal(run('disk.review'), 'New review');
  assert.equal(run('state.item.id'), 'ad-astra');
  assert.equal(run('dirty()'), false);
});

test('edits during saving stay in the editor and are saved next', async () => {
  const run = savedEditor();
  run(`
    let release;
    const realApi = api;
    api = async (route, data) => {
      if (route === 'save-notes' && !release) await new Promise(resolve => { release = resolve; });
      return realApi(route, data);
    };
    $('notes').value = 'First edit'; scheduleSave();
    const pending = flushSave();
    $('notes').value = 'Second edit'; scheduleSave();
    release();
  `);
  await run('pending');
  assert.equal(run('disk.notes'), 'Second edit');
  assert.equal(run("$('notes').value"), 'Second edit');
  assert.equal(run('dirty()'), false);
});

test('failed autosave blocks navigation and keeps unsaved text', async () => {
  const run = savedEditor();
  run(`$('review').value = 'Keep this review'; scheduleSave(); api = async () => { throw Error('Offline'); };`);
  await assert.rejects(run(`openMovie('ad-astra')`), /Offline/);
  assert.equal(run('state.item.id'), 'digger');
  assert.equal(run("$('review').value"), 'Keep this review');
  assert.equal(run('dirty()'), true);
  assert.match(run("$('save-state').textContent"), /Could not save/);
});

test('undoing an edit clears pending changes without a write', async () => {
  const run = savedEditor();
  run(`$('notes').value = 'Temporary'; scheduleSave(); $('notes').value = 'Original notes'; scheduleSave();`);
  await run('flushSave()');
  assert.equal(run('requests.length'), 0);
  assert.equal(run('dirty()'), false);
});

test('simultaneous save requests share a queue', async () => {
  const run = savedEditor();
  run(`$('review').value = 'New review'; scheduleSave();`);
  await run('Promise.all([flushSave(), flushSave(), flushSave()])');
  assert.equal(run("requests.filter(request => request.route === 'save-review').length"), 1);
  assert.equal(run('dirty()'), false);
});


test('input schedules autosave after a short pause', async () => {
  const run = savedEditor();
  run(`
    let callback;
    let delay;
    setTimeout = (fn, ms) => { callback = fn; delay = ms; return 1; };
    $('review').value = 'Automatically saved review';
    $('review').listeners.input();
  `);
  assert.equal(run('delay'), 700);
  assert.equal(run('requests.length'), 0);
  run('callback()');
  await run('saving');
  assert.equal(run('disk.review'), 'Automatically saved review');
  assert.equal(run('dirty()'), false);
});


test('review sizing collapses stale height before measuring text', () => {
  const run = editor();
  run(`
    const review = $('review');
    review.style.height = '2400px';
    Object.defineProperty(review, 'scrollHeight', {
      get() { return review.style.height === '0px' ? 320 : 2400; }
    });
    resizeReview();
  `);
  assert.equal(run("$('review').style.height"), '320px');
  run(`Object.defineProperty($('review'), 'getClientRects', { value: () => [] }); resizeReview();`);
  assert.equal(run("$('review').style.height"), '320px');
});
