/**
 * Serin API client.
 *
 * The app is a *client*, not a service: it points at whatever Serin backend
 * the user configured (their self-hosted box, a LAN address, Tailscale).
 * URL + bearer token live in SecureStore; the last good portfolio snapshot
 * is cached in AsyncStorage so the app renders offline.
 *
 * All endpoints live under `/api/v1/` — the versioned contract shared with
 * the web app.
 */

import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SecureStore from "expo-secure-store";

const URL_KEY = "serin.backendUrl";
const TOKEN_KEY = "serin.bearerToken";
const BIOMETRIC_KEY = "serin.biometricLock";
const SNAPSHOT_KEY = "serin.snapshot.v1";

export async function getBackendUrl(): Promise<string | null> {
  return SecureStore.getItemAsync(URL_KEY);
}

export async function setBackendUrl(url: string): Promise<void> {
  const cleaned = url.trim().replace(/\/$/, "");
  await SecureStore.setItemAsync(URL_KEY, cleaned);
}

export async function getToken(): Promise<string | null> {
  return SecureStore.getItemAsync(TOKEN_KEY);
}

export async function setToken(token: string): Promise<void> {
  if (token) await SecureStore.setItemAsync(TOKEN_KEY, token);
  else await SecureStore.deleteItemAsync(TOKEN_KEY);
}

export async function getBiometricLock(): Promise<boolean> {
  return (await SecureStore.getItemAsync(BIOMETRIC_KEY)) === "1";
}

export async function setBiometricLock(enabled: boolean): Promise<void> {
  await SecureStore.setItemAsync(BIOMETRIC_KEY, enabled ? "1" : "0");
}

export class SerinError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = await getBackendUrl();
  if (!base) {
    throw new SerinError(0, "Backend URL not configured. Open Settings.");
  }
  const token = await getToken();
  const isForm = typeof FormData !== "undefined" && init.body instanceof FormData;
  const headers: Record<string, string> = {
    ...(isForm ? {} : { "content-type": "application/json" }),
    ...(init.headers as Record<string, string> | undefined),
  };
  if (token) headers.authorization = `Bearer ${token}`;
  const response = await fetch(`${base}/api/v1${path}`, { ...init, headers });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new SerinError(response.status, extractDetail(text) || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

function extractDetail(text: string): string {
  try {
    const parsed = JSON.parse(text);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    /* raw text below */
  }
  return text;
}

// --- types -----------------------------------------------------------------

export type Version = { app: string; api_version: string; build: string };

export type Position = {
  id: number;
  symbol: string;
  name: string;
  broker: string;
  asset_type: string;
  quantity: number;
  average_cost: number;
  current_price: number;
  currency: string;
  sector: string;
  market_value: number;
  total_cost: number;
  unrealized_gain: number;
  unrealized_gain_pct: number;
  updated_at: string;
};

export type Portfolio = {
  total_value: number;
  total_cost: number;
  total_gain: number;
  total_gain_pct: number;
  cash_value: number;
  positions: Position[];
  last_refresh: string | null;
};

export type Briefing = {
  id: number;
  status: "running" | "done" | "error";
  summary: string;
  output_markdown: string;
  model: string;
  created_at: string;
  completed_at: string | null;
  error?: string;
};

export type SymbolHistory = {
  symbol: string;
  period: string;
  dates: string[];
  closes: number[];
};

export type PriceHistory = {
  history: Record<string, { dates: string[]; closes: number[] }>;
  cached: string[];
  errors: string[];
};

export type Performance = {
  today_change: number;
  today_change_pct: number;
  periods: { period: string; return_pct: number }[];
  accurate?: {
    available: boolean;
    twr_pct?: number;
    mwr_period_pct?: number | null;
    start_date?: string;
  };
};

export type ConnectorCard = {
  manifest: { id: string; name: string; kind: string; description: string };
  enabled: boolean;
  configured: boolean;
};

export type ExtractRow = {
  symbol: string;
  name: string;
  broker: string;
  asset_type: string;
  quantity: number;
  average_cost: number;
  current_price?: number;
  warnings?: string[];
};

export type ExtractResult = {
  rows: ExtractRow[];
  model: string;
  cost_usd: number;
  notes?: string;
  notice?: string;
};

export type Schedule = {
  enabled: boolean;
  time: string; // "HH:MM"
  timezone: string;
  email_enabled: boolean;
  next_run?: string | null;
};

export type NewsItem = {
  title: string;
  url: string;
  source: string;
  published: string;
  tickers?: string[];
};

export type NewsFeed = { portfolio_news: NewsItem[]; market_news: NewsItem[] };

// Whatever the X-ray connector returns is rendered as-is — the app holds no
// paid logic, mirroring the web XrayCard contract.
export type XrayReport = {
  entitled: boolean;
  message?: string;
  concentration_hhi?: number;
  effective_holdings?: number;
  holdings_count?: number;
  top5_weight?: number;
  largest?: { symbol: string; weight: number }[];
  cash_drag?: number;
  sector_mix?: Record<string, number>;
  asset_mix?: Record<string, number>;
  broker_mix?: Record<string, number>;
  currency_mix?: Record<string, number>;
  cross_broker_overlap?: { symbol: string; brokers: string[] }[];
  fee_drag?: {
    weighted_expense_ratio: number;
    annual_cost: number;
    funds: { symbol: string; expense_ratio: number; annual_cost: number }[];
    coverage: number;
  } | null;
  factor_snapshot?: {
    weighted_pe: number | null;
    weighted_pb: number | null;
    weighted_beta: number | null;
    weighted_dividend_yield: number | null;
    size_mix: Record<string, number>;
    coverage: number;
  } | null;
  benchmark?: {
    symbol: string;
    basis: string;
    periods: {
      period: string;
      portfolio_pct: number;
      benchmark_pct: number;
      delta_pct: number;
      coverage: number;
    }[];
  } | null;
  flags?: string[];
};

export type PositionInput = {
  symbol: string;
  name?: string;
  broker: string;
  asset_type: string;
  quantity: number;
  average_cost: number;
  current_price?: number;
  currency?: string;
};

// --- endpoints ----------------------------------------------------------------

export const serin = {
  version: () => request<Version>("/version"),
  portfolio: () => request<Portfolio>("/portfolio"),
  positions: () => request<Position[]>("/positions"),
  createPosition: (body: PositionInput) =>
    request<Position>("/positions", { method: "POST", body: JSON.stringify(body) }),
  updatePosition: (id: number, body: PositionInput) =>
    request<Position>(`/positions/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deletePosition: (id: number) => request<{ ok: boolean }>(`/positions/${id}`, { method: "DELETE" }),
  refreshPrices: () =>
    request<{ updated: number; errors: string[] }>("/prices/refresh", { method: "POST", body: "{}" }),
  priceHistory: (period = "3m") => request<PriceHistory>(`/price-history?period=${period}`),
  symbolHistory: (symbol: string, period = "6m", assetType = "stock") =>
    request<SymbolHistory>(`/quote/${encodeURIComponent(symbol)}/history?period=${period}&asset_type=${assetType}`),
  performance: () => request<Performance>("/performance"),
  briefings: () => request<Briefing[]>("/briefings"),
  briefing: (id: number) => request<Briefing>(`/briefings/${id}`),
  connectors: () => request<{ connectors: ConnectorCard[] }>("/connectors"),
  importExtract: (form: FormData) => request<ExtractResult>("/import/extract", { method: "POST", body: form }),
  bulkInsert: (rows: ExtractRow[], replace: boolean) =>
    request<{ inserted: number; skipped: number }>("/positions/bulk", {
      method: "POST",
      body: JSON.stringify({ rows, replace }),
    }),
  registerPush: (token: string) =>
    request<{ ok: boolean }>("/push/register", { method: "POST", body: JSON.stringify({ token }) }),
  schedule: () => request<Schedule>("/schedule"),
  setSchedule: (body: Omit<Schedule, "next_run">) =>
    request<Schedule>("/schedule", { method: "PUT", body: JSON.stringify(body) }),
  news: () => request<NewsFeed>("/news"),
  xray: () => request<XrayReport>("/connectors/xray/run", { method: "POST", body: "{}" }),
};

// --- offline snapshot -------------------------------------------------------------

export type Snapshot = {
  savedAt: string;
  portfolio: Portfolio;
  positions: Position[];
  history: PriceHistory["history"];
};

export async function saveSnapshot(snapshot: Omit<Snapshot, "savedAt">): Promise<void> {
  try {
    await AsyncStorage.setItem(SNAPSHOT_KEY, JSON.stringify({ ...snapshot, savedAt: new Date().toISOString() }));
  } catch {
    // Cache is best-effort; never break the live path over storage limits.
  }
}

export async function loadSnapshot(): Promise<Snapshot | null> {
  try {
    const raw = await AsyncStorage.getItem(SNAPSHOT_KEY);
    return raw ? (JSON.parse(raw) as Snapshot) : null;
  } catch {
    return null;
  }
}
