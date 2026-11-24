const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../src/dj_dashboard.html'), 'utf8');
const code = html.slice(html.indexOf('  function appendLines('), html.indexOf('  function classifyVoice('));

function setup() {
  const entries = [];
  const classes = new Set();
  const log = { children: entries, scrollHeight: 100, clientHeight: 100, scrollTop: 0,
    appendChild: item => entries.push(item), removeChild: item => entries.splice(entries.indexOf(item), 1),
    get firstChild() { return entries[0]; },
    classList: { toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name) } };
  const context = { MAX_LINES: 300, document: { getElementById: () => log, createElement: () => ({}) } };
  vm.createContext(context);
  vm.runInContext(code, context);
  return { context, entries, classes };
}

test('new Standard messages have distinct semantic colors', () => {
  const { context: c } = setup();
  for (const [line, expected] of [
    ['NF — HOPE', 'dj-pick'],
    ['--- Standard: Deine Favoriten ---', 'section block'],
    ['Standard: nächster Abschnitt vorbereitet — Entdeckungen: KSI (1 Titel).', 'ready'],
    ['Pausiert; auf Fortsetzen in Spotify warten', 'state'],
    ['Spotify Rate-Limit (HTTP 429): 30s warten', 'warning'],
    ['Playback-Daten vorübergehend unbekannt', 'warning'],
    ['Manueller oder unklarer Wechsel; DJ freigegeben bis zum nächsten DJ-Befehl', 'warning'],
    ['Standard: Blockplanung Kein passender Titel; vorhandener Mix läuft weiter.', 'warning'],
    ['Start nicht bestätigt nach 4 Prüfungen', 'error'],
    ['Startdiagnose: angefordert=Ado — 新時代; Spotify=Ado — New Genesis', 'error'],
  ]) assert.equal(c.classifyDJ(line), expected, line);
});

test('routine detail remains available and does not obscure songs', () => {
  const { context: c } = setup();
  for (const line of ['--- Neuer Song ---', 'Standard DJ -> NF', 'Track near end -- prepared DJ transition',
    'AI artist (found on Spotify): KSI', 'AI artist: no unambiguous matching profile — skipping',
    'Standard: KI plant den nächsten Musikabschnitt im Hintergrund.',
    'AI: Persönlicher DJ: nächster Musikblock angefragt; Netzwerk-Zeitlimit 60s.',
    'Your application has reached a rate/request limit. Retry will occur after: 0 s', 'Max Retries reached']) {
    assert.equal(c.classifyDJ(line), 'detail', line);
  }
});

test('details toggle preserves raw lines and section formatting; text remains inert', () => {
  const { context: c, entries, classes } = setup();
  const raw = 'AI artist (known): <img src=x onerror=alert(1)>';
  c.appendLines('dj-log', ['--- Standard: Deine Favoriten ---', raw, 'NF — HOPE'], c.classifyDJ);
  assert.equal(entries[0].textContent, 'Standard: Deine Favoriten');
  assert.equal(entries[1].textContent, raw);
  assert.equal(entries[1].innerHTML, undefined);
  assert.equal(entries[2].className, 'log-line dj-pick');
  c.toggleLogDetails(true);
  assert.ok(classes.has('show-details'));
  c.toggleLogDetails(false);
  assert.equal(classes.size, 0);
  assert.equal(entries.length, 3);
  assert.ok(html.includes('#dj-log:not(.show-details) .detail { display: none; }'));
});

test('cost display shows USD and flags unknown charges instead of pretending zero', () => {
  const elements = {};
  const context = { document: { getElementById: id => elements[id] ||= {} } };
  vm.createContext(context);
  vm.runInContext(code, context);
  context.renderCosts({ available: true, active: true, session_usd: .0105, total_usd: .1234, unknown: 0 });
  assert.equal(elements['cost-session'].textContent, '0,01 USD');
  assert.equal(elements['cost-total'].textContent, '0,12 USD');
  context.renderCosts({ available: true, active: true, session_usd: .001, total_usd: .001, remaining_usd: 23.4999, unknown: 0 });
  assert.equal(elements['cost-session'].textContent, '0,00 USD');
  assert.equal(elements['cost-remaining'].textContent, '23,50 USD');
  context.renderCosts({ available: true, active: true, remaining_usd: -.014, unknown: 0 });
  assert.equal(elements['cost-remaining'].textContent, '-0,01 USD');
  context.renderCosts({ available: true, active: true, session_usd: .01, total_usd: .12, unknown: 2 });
  assert.match(elements['cost-note'].textContent, /Unvollständig.*2/);
  context.renderCosts({ available: false });
  assert.equal(elements['cost-total'].textContent, '—');
  context.renderCosts({ available: true, active: false });
  assert.match(elements['cost-note'].textContent, /DJ-Neustart/);
});
