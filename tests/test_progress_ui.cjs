const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../src/dj_dashboard.html'), 'utf8');
const model = html.match(/\/\/ Progress model:[\s\S]*?\/\/ End progress model\./)[0];
const api = vm.createContext({});
vm.runInContext(model, api);
const state = (changes = {}) => ({ playback_progress: {
  sample_id: 1, uri: 'spotify:track:one', progress_ms: 42000, duration_ms: 200000,
  is_playing: true, age_ms: 0, ...changes,
}});

test('progress advances without API calls and repeated broadcasts do not reset its clock', () => {
  const sample = api.progressSampleFromState(state(), null, 1000);
  const repeated = api.progressSampleFromState(state({ age_ms: 500 }), sample, 1500);
  assert.equal(repeated, sample);
  assert.equal(api.progressAt(repeated, 3000).position, 44000);
});

test('pause freezes and resume uses the new Spotify position', () => {
  const paused = api.progressSampleFromState(state({ is_playing: false }), null, 1000);
  assert.equal(api.progressAt(paused, 50000).position, 42000);
  assert.equal(api.progressAt(paused, 50000).stale, false);
  const resumed = api.progressSampleFromState(state({ sample_id: 2, progress_ms: 45000 }), paused, 50000);
  assert.equal(api.progressAt(resumed, 51000).position, 46000);
});

test('backward/forward seeks and new songs immediately replace the old timeline', () => {
  let sample = api.progressSampleFromState(state(), null, 0);
  for (const position of [10000, 190000, 0]) {
    sample = api.progressSampleFromState(state({ sample_id: sample.id + 1, progress_ms: position }), sample, 5000);
    assert.equal(api.progressAt(sample, 5000).position, position);
  }
});

test('missing data clears progress and progress never exceeds duration', () => {
  assert.equal(api.progressSampleFromState({ playback_progress: null }, null, 0), null);
  assert.equal(api.progressAt(null, 1000), null);
  const sample = api.progressSampleFromState(state({ progress_ms: 199000 }), null, 0);
  assert.equal(api.progressAt(sample, 5000).position, 200000);
});

test('stale samples stop extrapolating, including after page reload', () => {
  const sample = api.progressSampleFromState(state({ age_ms: 5000 }), null, 1000);
  assert.equal(api.progressAt(sample, 2000).position, 48000);
  const stale = api.progressAt(sample, 30000);
  assert.equal(stale.stale, true);
  assert.equal(stale.position, 60000);
  assert.equal(api.progressAt(sample, 90000).position, stale.position);
});

test('time labels use minutes and padded seconds', () => {
  assert.equal(api.formatProgressTime(0), '0:00');
  assert.equal(api.formatProgressTime(65000), '1:05');
  assert.equal(api.formatProgressTime(200999), '3:20');
});

test('dashboard updates bar and accessibility labels, clears on disconnect', () => {
  const elements = new Map();
  const handlers = {};
  const intervals = [];
  let now = 1000;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      style: {}, attributes: {}, textContent: '', children: [],
      classList: { toggle() {}, add() {}, remove() {} },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      addEventListener() {},
      appendChild(child) { this.children.push(child); },
    });
    return elements.get(id);
  };
  const browser = vm.createContext({
    io: () => ({ on: (name, handler) => { handlers[name] = handler; }, emit() {} }),
    document: { getElementById: element, querySelectorAll: () => [], createElement: () => ({}) },
    performance: { now: () => now },
    setInterval: callback => intervals.push(callback),
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script, browser);
  handlers.state({ ...state({ progress_ms: 50000 }), current_artist: 'Artist', current_track: 'Song', is_playing: true });
  assert.equal(element('progress-fill').style.transform, 'scaleX(0.25)');
  assert.equal(element('progress-elapsed').textContent, '0:50');
  assert.equal(element('progress-duration').textContent, '3:20');
  assert.equal(element('progress-rail').attributes['aria-valuenow'], '25');
  now += 2000;
  intervals[0]();
  assert.equal(element('progress-elapsed').textContent, '0:52');
  handlers.disconnect();
  assert.equal(element('progress-elapsed').textContent, '--:--');
  assert.equal(element('dj-status').textContent, 'Verbindung zum DJ unterbrochen');
  assert.equal(element('progress-fill').style.transform, 'scaleX(0)');
  assert.equal(element('progress-rail').attributes['aria-valuenow'], undefined);
  handlers.dj_log({ lines: ['--- Modus: German Trap ---', '--- Neuer Song ---',
                            'DJ -> Sierra Kidd', 'Sierra Kidd: fetched=68, eligible=51'] });
  const log = element('dj-log').children;
  assert.equal(log[0].className, 'log-line section');
  assert.equal(log[0].textContent, 'Modus: German Trap');
  assert.equal(log[1].textContent, 'Neuer Song');
  assert.equal(log[2].className, 'log-line dj-pick');
  assert.equal(log[3].className, 'log-line filter');
  handlers.state({ shutting_down: true, status: 'DJ und Dashboard werden beendet' });
  handlers.disconnect();
  assert.match(element('dj-status').textContent, /DJ und Dashboard beendet/);
});
