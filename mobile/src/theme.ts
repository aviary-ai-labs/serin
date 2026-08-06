/**
 * Theme tokens — dark/light variants of the Calm Dashboard palette.
 * Consumed via useTheme() so every screen honors the system color scheme.
 */

import { useColorScheme } from "react-native";

export type Theme = {
  bg: string;
  card: string;
  inset: string;
  border: string;
  ink: string;
  sec: string;
  mut: string;
  acc: string;
  up: string;
  down: string;
};

export const DARK: Theme = {
  bg: "#0f1612",
  card: "#17231f",
  inset: "#1d2b26",
  border: "#25342e",
  ink: "#f6faf8",
  sec: "#b7c4be",
  mut: "#7b8c84",
  acc: "#6ea8ff",
  up: "#4fc98a",
  down: "#ff7a6e",
};

export const LIGHT: Theme = {
  bg: "#f3f4f7",
  card: "#ffffff",
  inset: "#f1f3f6",
  border: "#e4e7ee",
  ink: "#171b26",
  sec: "#4c5566",
  mut: "#8a93a6",
  acc: "#2f6bed",
  up: "#15a05a",
  down: "#e0453a",
};

export function useTheme(): Theme {
  return useColorScheme() === "light" ? LIGHT : DARK;
}

export function money(value: number | null | undefined, currency = "USD"): string {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(value || 0));
  } catch {
    return `$${Number(value || 0).toFixed(2)}`;
  }
}

export function signedPct(value: number | null | undefined): string {
  const n = Number(value || 0);
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

export function signedMoney(value: number | null | undefined, currency = "USD"): string {
  const n = Number(value || 0);
  return `${n >= 0 ? "+" : ""}${money(n, currency)}`;
}
