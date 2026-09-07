// Display currency is a module-level setting so every money() call site picks
// it up without threading a prop through the whole tree. App.jsx sets it from
// /api/config on load and after the user changes it.
let DISPLAY_CURRENCY = 'USD';

export const COMMON_BROKERS = [
  'manual',
  'robinhood',
  'fidelity',
  'charlesschwab',
  'etrade',
  'vanguard',
  'merrill',
  'morgan_stanley',
  'ibkr',
  'webull',
  'sofi',
  'tastytrade',
  'alpaca',
  'coinbase',
  'binance',
];

const BROKER_LABELS = {
  manual: 'Manual',
  robinhood: 'Robinhood',
  fidelity: 'Fidelity',
  charlesschwab: 'Charles Schwab',
  schwab: 'Charles Schwab',
  etrade: 'E*TRADE',
  vanguard: 'Vanguard',
  merrill: 'Merrill',
  morgan_stanley: 'Morgan Stanley',
  ibkr: 'Interactive Brokers',
  webull: 'Webull',
  sofi: 'SoFi',
  tastytrade: 'tastytrade',
  alpaca: 'Alpaca',
  coinbase: 'Coinbase',
  binance: 'Binance',
};

export function normalizeBroker(value) {
  return String(value || '').trim().toLowerCase().replace(/\s+/g, '_');
}

export function brokerOptions(existing = []) {
  return [...new Set(
    [...existing, ...COMMON_BROKERS]
      .map(normalizeBroker)
      .filter(Boolean),
  )];
}

export function setDisplayCurrency(code) {
  DISPLAY_CURRENCY = String(code || 'USD').toUpperCase();
}

export function displayCurrency() {
  return DISPLAY_CURRENCY;
}

export function money(value, currency = undefined) {
  const code = currency || DISPLAY_CURRENCY;
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: code }).format(Number(value || 0));
  } catch {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value || 0));
  }
}

export function moneyPrecise(value, digits = 4) {
  return `$${Number(value || 0).toFixed(digits)}`;
}

export function pct(value) {
  return `${Number(value || 0).toFixed(2)}%`;
}

export function signedMoney(value) {
  const n = Number(value || 0);
  return `${n >= 0 ? '+' : ''}${money(n)}`;
}

export function signedPct(value) {
  const n = Number(value || 0);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

export function quantityLabel(value) {
  const n = Number(value || 0);
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(Math.abs(n) < 1 ? 6 : 4).replace(/0+$/, '').replace(/\.$/, '');
}

export function brokerLabel(value) {
  const broker = normalizeBroker(value || 'manual');
  return BROKER_LABELS[broker] || broker.replace(/_/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

/**
 * Parse a date the way the value means it.
 *
 * `new Date("2026-08-20")` is specified to parse a bare date as UTC midnight,
 * and every toLocale* call then renders it in local time — which lands on the
 * previous day everywhere west of Greenwich. A transaction date is a calendar
 * day, not an instant, so a ledger built on the plain constructor shows every
 * trade a day early for most of the world. Timestamps that carry a time are
 * genuine instants and are left alone.
 */
function asLocalDate(value) {
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return new Date(`${value}T00:00:00`);
  }
  return new Date(value);
}

export function dateShort(value) {
  if (!value) return '';
  try {
    return asLocalDate(value).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return value;
  }
}

export function dateDay(value) {
  if (!value) return '';
  try {
    return asLocalDate(value).toLocaleDateString('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  } catch {
    return value;
  }
}

/** A ledger date: day, month and year, no time.
 *
 *  dateShort renders "Aug 12, 12:00 AM" — a midnight that is an artifact of
 *  parsing a bare date, not something the broker recorded. And a drill-in
 *  sorted by size, not by date, puts 2016 next to 2026, so the year is the
 *  part that cannot be dropped. */
export function dateLedger(value) {
  if (!value) return '';
  try {
    return asLocalDate(value).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  } catch {
    return value;
  }
}

export function timeAgo(value) {
  if (!value) return '';
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(value).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function durationLabel(startIso, endIso) {
  if (!startIso || !endIso) return '';
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return '';
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function positionKey(position) {
  return `${position.symbol}-${position.broker}-${position.asset_type}-${position.id}`;
}

export function lotKey(symbol, broker) {
  return `${symbol}::${broker}`;
}

export function groupTaxLots(lots) {
  return (lots || []).reduce((acc, lot) => {
    const key = lotKey(lot.symbol, lot.broker);
    if (!acc[key]) acc[key] = [];
    acc[key].push(lot);
    return acc;
  }, {});
}

export const ALLOC_PALETTE = [
  '#2f6f9f',
  '#0f8a5f',
  '#b98217',
  '#7b5ea7',
  '#c2413b',
  '#3f8d8f',
  '#8a6d3b',
  '#5b7d99',
  '#9a5f7d',
  '#6a8a4f',
];

export function allocColor(index) {
  return ALLOC_PALETTE[index % ALLOC_PALETTE.length];
}
